"""appendMetadata owns a transaction, so it has to end one on every path.

upsertTrack and insertPlay both document that they do NOT commit - callers
compose them into a single transaction and "call commit()/rollback()
themselves". appendMetadata called commit() on success and nothing at all on
failure, which on a thread-local, long-lived connection (Database/db.py's
ConnectionManager) left the staged writes pending: they held the WAL write lock
until this thread's next commit, and then that unrelated commit PERSISTED them.

The listener is what makes that reachable rather than theoretical - it catches
per item and moves to the next one (see _addToDatabaseFromListener), so item
N's half-written catalog rows were committed by item N+1.
"""
import sqlite3
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest


def _meta(trackId, playedAt):
    """A full formatTrack-shaped item: entry fields plus enough track metadata
    to satisfy Repository.upsertTrack (mirrors tests/test_import_commit.py)."""
    track = normalizeTrackForTest({"id": trackId, "name": f"Song {trackId}", "artists": []})
    track["playedAt"] = playedAt
    track["timePlayed"] = 60000   #< a full listen (> the 5s skip floor) -> real play
    track["playedFrom"] = None
    return track


class TestAppendMetadataTransaction(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.db = self._makeDb({}, [])

    def test_a_failed_play_insert_leaves_nothing_staged(self):
        """The failure appendMetadata is actually exposed to: upsertTrack has
        already written, and insertPlay raises on a locked database."""
        with patch.object(self.db.repo, "insertPlay", side_effect=RuntimeError("database is locked")):
            with self.assertRaises(RuntimeError):
                self.db.appendMetadata(_meta("t-doomed", 500))

        self.assertFalse(self.db.repo.connection().in_transaction,
                         "a failed appendMetadata left its transaction open, holding the write lock")

    def test_a_failed_play_insert_is_not_committed_by_the_next_play(self):
        """The consequence, not just the state: the listener's next item calls
        commit(), and that commit must not carry the failed item's track with
        it. Read back through a query rather than by asserting on the rollback
        call, so it fails for the discarded-track reason and not the how."""
        with patch.object(self.db.repo, "insertPlay", side_effect=RuntimeError("database is locked")):
            with self.assertRaises(RuntimeError):
                self.db.appendMetadata(_meta("t-doomed", 500))

        self.db.appendMetadata(_meta("t-good", 600))

        self.assertIsNone(self.db.repo.getTrack("t-doomed"),
                          "the next play's commit persisted the failed play's track")
        self.assertIsNotNone(self.db.repo.getTrack("t-good"))

    def test_a_failure_inside_the_track_upsert_discards_its_partial_writes(self):
        """The corruption case, and the reason this is not just about a stale
        lock. upsertTrack runs five write statements - albums, artists, tracks,
        then a DELETE of track_artists followed by re-INSERTing them - so a
        failure partway through leaves REAL rows staged, including a track
        whose artist links have been deleted and not yet restored.

        The side effect writes a real row and then raises, which is what a
        mid-statement failure inside upsertTrack does; mocking the method
        alone would stage nothing and pass either way.
        """
        def partialWriteThenFail(track, created_reason=None):
            self.db.repo.connection().execute(
                "INSERT INTO albums (id, name, url, total_tracks, release_date, image_id, image_url)"
                " VALUES ('half-written', 'Half Written', '', 1, 0.0, 'half-written', '')"
            )
            raise RuntimeError("failed after the album insert")

        with patch.object(self.db.repo, "upsertTrack", side_effect=partialWriteThenFail):
            with self.assertRaises(RuntimeError):
                self.db.appendMetadata(_meta("t-doomed", 500))

        self.assertFalse(self.db.repo.connection().in_transaction)

        self.db.appendMetadata(_meta("t-good", 600))

        staged = self.db.repo.connection().execute(
            "SELECT 1 FROM albums WHERE id='half-written'"
        ).fetchone()
        self.assertIsNone(staged,
                          "the next play's commit persisted a partial catalog write")

    def test_a_successful_append_still_commits(self):
        """The guard must not turn the success path into a rollback."""
        self.assertTrue(self.db.appendMetadata(_meta("t-good", 600)))
        self.assertFalse(self.db.repo.connection().in_transaction)
        self.assertIsNotNone(self.db.repo.getTrack("t-good"))

    def test_a_failing_rollback_does_not_replace_the_error_that_caused_it(self):
        """The masking case. parseError reads only the exception handed to it,
        never __context__, so whatever reaches the listener's log line and
        listener_last_error IS the whole report. A rollback that raised put
        itself there instead of the insert failure - and a rollback is most
        likely to fail exactly when the database is in the trouble you need the
        original error to describe."""
        insertFailed = patch.object(self.db.repo, "insertPlay",
                                    side_effect=RuntimeError("database is locked"))
        rollbackFailed = patch.object(self.db.repo, "rollback",
                                      side_effect=sqlite3.ProgrammingError("closed database"))
        with insertFailed, rollbackFailed:
            with self.assertRaises(RuntimeError) as caught:
                self.db.appendMetadata(_meta("t-doomed", 500))

        self.assertIn("database is locked", str(caught.exception))
        self.assertNotIsInstance(caught.exception, sqlite3.ProgrammingError)
