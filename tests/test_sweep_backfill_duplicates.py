"""Tests for the one-off pause-stretched backfill duplicate sweep.

The sweep applies the reconciler's end-time pairing rule to the whole plays
table: a web_api_backfill row whose played_at sits within tolerance of a
same-user same-track listener row's created_at (the observed play end) is the
same physical listen recorded twice, and only the backfill copy goes. The
guarantees mirror the live reconciler's: listener rows only as anchors, real
plays only, never delete without a pairing.
"""
import sqlite3
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.repository import Repository
import sweep_backfill_duplicates as sweep


def _track(trackId):
    return {
        "id": trackId,
        "name": f"Track {trackId}",
        "url": f"http://example.com/track/{trackId}",
        "artists": [
            {"id": "a1", "name": "Artist a1", "url": "http://example.com/artist/a1",
             "imageUrl": "", "imageId": "a1"},
        ],
        "album": {
            "id": "al1", "name": "Album al1", "url": "http://example.com/album/al1",
            "imageId": "al1", "imageUrl": "", "totalTracks": 10, "releaseDate": 0.0,
        },
        "imageUrl": "", "imageId": "al1", "duration": 287000, "explicit": False,
        "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0.0,
    }


LISTENER_REASON = "listener_play (user: alice)"
BACKFILL_REASON = "web_api_backfill_play (user: alice)"
IMPORT_REASON = "history_import (user: alice)"

#< the incident's shape: start, then end = start + duration + pause
START = 1_700_000_000.0
END = START + 474.0


class SweepTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.dbPath = Path(self._tmpdir.name) / "test.db"
        self.repo = Repository(self.dbPath)
        self.addCleanup(self.repo.connectionManager.close)

        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")
        for trackId in ("t1", "t2"):
            self.repo.upsertTrack(_track(trackId))
        self.repo.commit()

    def _insertPlay(self, trackId, playedAt, createdAt=None, created_reason=None,
                    username="alice", is_skip=0):
        """created_at is stamped with time.time() inside insertPlay - pin the
        clock so the test controls the row's insert-time stamp."""
        with patch("Database.queries.plays.time") as mockTime:
            mockTime.time.return_value = createdAt
            self.repo.insertPlay(username, trackId, playedAt, 60000,
                                 created_reason=created_reason, is_skip=is_skip)
        self.repo.commit()

    def _sweepConn(self):
        conn = sqlite3.connect(self.dbPath)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def _find(self, tolerance=10):
        return sweep.findBackfillEndTimeDuplicates(self._sweepConn(), tolerance)

    def _playedAts(self, username="alice"):
        rows = self.repo._conn().execute(
            "SELECT played_at FROM plays WHERE username=? ORDER BY played_at", (username,)
        ).fetchall()
        return [r["played_at"] for r in rows]

    def test_pause_stretched_backfill_copy_is_found(self):
        self._insertPlay("t1", START, createdAt=END, created_reason=LISTENER_REASON)
        self._insertPlay("t1", END + 1, created_reason=BACKFILL_REASON)

        found = self._find()

        self.assertEqual([(r["username"], r["track_id"], r["played_at"]) for r in found],
                         [("alice", "t1", END + 1)])

    def test_a_backfill_row_without_a_listener_sibling_is_kept(self):
        """The whole point of the backfill: a play nothing else recorded."""
        self._insertPlay("t1", START, created_reason=BACKFILL_REASON)

        self.assertEqual(self._find(), [])

    def test_a_listener_end_outside_the_tolerance_pairs_nothing(self):
        self._insertPlay("t1", START, createdAt=END, created_reason=LISTENER_REASON)
        self._insertPlay("t1", END + 11, created_reason=BACKFILL_REASON)

        self.assertEqual(self._find(), [])

    def test_an_import_rows_created_at_never_anchors_a_pairing(self):
        """An import row's created_at is the import moment, not a play end."""
        self._insertPlay("t2", START, createdAt=END, created_reason=IMPORT_REASON)
        self._insertPlay("t2", END + 1, created_reason=BACKFILL_REASON)

        self.assertEqual(self._find(), [])

    def test_a_legacy_listener_row_without_created_at_pairs_nothing(self):
        self._insertPlay("t1", START, created_reason=LISTENER_REASON)
        #< insertPlay stamps created_at whenever created_reason is set - blank
        #  it to model a legacy row
        self.repo._conn().execute("UPDATE plays SET created_at=NULL")
        self.repo.commit()
        self._insertPlay("t1", END + 1, created_reason=BACKFILL_REASON)

        self.assertEqual(self._find(), [])

    def test_skip_rows_are_ignored_on_both_sides(self):
        """The reconciler's own filter: a backfill row must never dedup
        against a merged skip row - and a skipped backfill row is not a
        double-recorded LISTEN."""
        self._insertPlay("t1", START, createdAt=END, created_reason=LISTENER_REASON, is_skip=1)
        self._insertPlay("t1", END + 1, created_reason=BACKFILL_REASON)
        self._insertPlay("t2", START, createdAt=END, created_reason=LISTENER_REASON)
        self._insertPlay("t2", END + 1, created_reason=BACKFILL_REASON, is_skip=1)

        self.assertEqual(self._find(), [])

    def test_pairing_never_crosses_users_or_tracks(self):
        self._insertPlay("t1", START, createdAt=END, created_reason=LISTENER_REASON)
        self._insertPlay("t2", END + 1, created_reason=BACKFILL_REASON)          #< other track
        self._insertPlay("t1", END + 1, created_reason=BACKFILL_REASON, username="bob")  #< other user

        self.assertEqual(self._find(), [])

    def test_a_copy_pairing_with_two_listener_rows_is_one_deletion(self):
        self._insertPlay("t1", START, createdAt=END, created_reason=LISTENER_REASON)
        self._insertPlay("t1", START - 300, createdAt=END - 2, created_reason=LISTENER_REASON)
        self._insertPlay("t1", END + 1, created_reason=BACKFILL_REASON)

        found = self._find()

        self.assertEqual(len(found), 1)

    def test_dry_run_deletes_nothing(self):
        self._insertPlay("t1", START, createdAt=END, created_reason=LISTENER_REASON)
        self._insertPlay("t1", END + 1, created_reason=BACKFILL_REASON)

        exitCode = sweep.main(["--db", str(self.dbPath)])

        self.assertEqual(exitCode, 0)
        self.assertEqual(self._playedAts(), [START, END + 1])

    def test_apply_deletes_only_the_backfill_copy(self):
        self._insertPlay("t1", START, createdAt=END, created_reason=LISTENER_REASON)
        self._insertPlay("t1", END + 1, created_reason=BACKFILL_REASON)
        self._insertPlay("t2", START, created_reason=BACKFILL_REASON)  #< genuine, no sibling

        exitCode = sweep.main(["--db", str(self.dbPath), "--apply"])

        self.assertEqual(exitCode, 0)
        self.assertEqual(self._playedAts(), [START, START])  #< listener row + genuine backfill


if __name__ == "__main__":
    unittest.main()
