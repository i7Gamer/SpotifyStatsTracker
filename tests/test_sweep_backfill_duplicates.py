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
        """This pairing anchors on created_at, which only means "play end" for
        a real listener play: a skip's created_at is when the user skipped
        AWAY. The skip-side duplicates have their own pairing (see
        TestSweepSkipDuplicates), which matches played_at instead."""
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

    def test_the_reported_listener_times_come_from_one_row(self):
        """The report prints a "listener start -> end" pair per duplicate. Two
        independent MIN() aggregates took each end of that pair from whichever
        row minimised it separately, so a backfill copy pairing with several
        listener rows was described by a start and an end that never belonged
        to the same listen - and the operator reads exactly this to decide
        whether --apply is safe.

        Here the EARLIER-starting row is not the one whose end pairs: MIN()
        reports its start (START - 300) beside the other row's end."""
        self._insertPlay("t1", START, createdAt=END, created_reason=LISTENER_REASON)
        self._insertPlay("t1", START - 300, createdAt=END + 4, created_reason=LISTENER_REASON)
        self._insertPlay("t1", END + 1, created_reason=BACKFILL_REASON)

        found = self._find()

        self.assertEqual(len(found), 1)
        row = found[0]
        pairs = {(START, END), (START - 300, END + 4)}
        self.assertIn((row["listener_played_at"], row["listener_created_at"]), pairs,
                      "the reported start and end must be one row's, not two rows' minima")
        #< and specifically the closest pairing, which is the one being acted on
        self.assertEqual(row["listener_created_at"], END)

    def test_more_duplicates_than_sqlites_parameter_ceiling_are_deleted(self):
        """deleteBackfillDuplicates binds one parameter per id. SQLite's
        SQLITE_MAX_VARIABLE_NUMBER is a COMPILE-TIME maximum (32766 here) that
        sqlite3_limit cannot raise, so a single statement over every duplicate
        raises "too many SQL variables" on a library with enough of them -
        after the report has already told the operator how many there are.

        Exercised against a lowered chunk size rather than by writing 32k rows:
        the batching is what is under test, not SQLite's constant."""
        for index in range(7):
            playedAt = START + index * 10_000
            self._insertPlay("t1", playedAt, createdAt=playedAt + 100,
                             created_reason=LISTENER_REASON)
            self._insertPlay("t1", playedAt + 101, created_reason=BACKFILL_REASON)

        with patch.object(sweep, "DELETE_CHUNK_SIZE", 2):
            exitCode = sweep.main(["--db", str(self.dbPath), "--apply"])

        self.assertEqual(exitCode, 0)
        self.assertEqual(len(self._playedAts()), 7, "every backfill copy should be gone")


class TestSweepSkipDuplicates(SweepTestCase):
    """The second pairing: a backfill row sitting on a same-track SKIP.

    A backfill row is is_skip=0 by construction (no ms_played from the Web API,
    so the source stamps the whole track duration), so the end-time pairing -
    which requires both sides real plays - can never reach one. The gap is
    matched played_at-to-played_at and kept tight: at 95s+ "skip, then a genuine
    replay the listener missed" is the likelier reading."""

    def _findSkips(self, tolerance=20):
        return sweep.findBackfillSkipDuplicates(self._sweepConn(), tolerance)

    def test_a_backfill_row_sitting_on_a_skip_is_found(self):
        self._insertPlay("t1", START, createdAt=START + 25, created_reason=LISTENER_REASON, is_skip=1)
        self._insertPlay("t1", START - 15, created_reason=BACKFILL_REASON)

        found = self._findSkips()

        self.assertEqual([(r["username"], r["track_id"], r["played_at"]) for r in found],
                         [("alice", "t1", START - 15)])

    def test_a_skip_outside_the_tight_tolerance_pairs_nothing(self):
        self._insertPlay("t1", START, createdAt=START + 25, created_reason=LISTENER_REASON, is_skip=1)
        self._insertPlay("t1", START + 95, created_reason=BACKFILL_REASON)

        self.assertEqual(self._findSkips(), [])

    def test_a_skips_created_at_never_anchors_the_pairing(self):
        """Only played_at counts on the skip side - its created_at is when the
        user skipped away, not an end the feed still owes us."""
        self._insertPlay("t1", START, createdAt=END, created_reason=LISTENER_REASON, is_skip=1)
        self._insertPlay("t1", END + 1, created_reason=BACKFILL_REASON)

        self.assertEqual(self._findSkips(), [])

    def test_a_real_play_never_pairs_here(self):
        """The end-time pairing owns real plays; matching them by played_at too
        would delete a genuine replay recorded seconds after a short track."""
        self._insertPlay("t1", START, createdAt=START + 25, created_reason=LISTENER_REASON)
        self._insertPlay("t1", START - 15, created_reason=BACKFILL_REASON)

        self.assertEqual(self._findSkips(), [])

    def test_a_skipped_backfill_row_is_never_deleted(self):
        """Only the backfill copy of a listen goes, and a skipped backfill row
        is not one - it is also impossible today, so this pins the filter
        rather than a live shape."""
        self._insertPlay("t1", START, createdAt=START + 25, created_reason=LISTENER_REASON, is_skip=1)
        self._insertPlay("t1", START - 15, created_reason=BACKFILL_REASON, is_skip=1)

        self.assertEqual(self._findSkips(), [])

    def test_pairing_never_crosses_users_or_tracks(self):
        self._insertPlay("t1", START, createdAt=START + 25, created_reason=LISTENER_REASON, is_skip=1)
        self._insertPlay("t2", START - 15, created_reason=BACKFILL_REASON)
        self._insertPlay("t1", START - 15, created_reason=BACKFILL_REASON, username="bob")

        self.assertEqual(self._findSkips(), [])

    def test_a_copy_pairing_with_two_skips_is_one_deletion(self):
        """Live shape (7kevinegger, 2026-08-12): one backfill row between two
        skips of the same track, 9s and 110s away."""
        self._insertPlay("t1", START, createdAt=START + 25, created_reason=LISTENER_REASON, is_skip=1)
        self._insertPlay("t1", START + 20, createdAt=START + 30, created_reason=LISTENER_REASON, is_skip=1)
        self._insertPlay("t1", START + 6, created_reason=BACKFILL_REASON)

        found = self._findSkips()

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["skip_played_at"], START, "the report must describe the closest pairing")

    def test_dry_run_reports_both_pairings_and_deletes_nothing(self):
        self._insertPlay("t1", START, createdAt=END, created_reason=LISTENER_REASON)
        self._insertPlay("t1", END + 1, created_reason=BACKFILL_REASON)          #< end-time pairing
        self._insertPlay("t2", START, createdAt=START + 25, created_reason=LISTENER_REASON, is_skip=1)
        self._insertPlay("t2", START - 15, created_reason=BACKFILL_REASON)       #< skip pairing

        exitCode = sweep.main(["--db", str(self.dbPath)])

        self.assertEqual(exitCode, 0)
        self.assertEqual(len(self._playedAts()), 4)

    def test_apply_deletes_the_skip_shadowed_copy_too(self):
        self._insertPlay("t2", START, createdAt=START + 25, created_reason=LISTENER_REASON, is_skip=1)
        self._insertPlay("t2", START - 15, created_reason=BACKFILL_REASON)
        self._insertPlay("t1", START, created_reason=BACKFILL_REASON)  #< genuine, no sibling

        exitCode = sweep.main(["--db", str(self.dbPath), "--apply"])

        self.assertEqual(exitCode, 0)
        self.assertEqual(self._playedAts(), [START, START])  #< the skip + the genuine backfill

    def test_a_row_caught_by_both_pairings_is_deleted_once(self):
        """deleteBackfillDuplicates binds ids; the same id reaching it twice
        would report a deletion count the operator cannot reconcile with the
        row count above it."""
        self._insertPlay("t1", START, createdAt=START + 5, created_reason=LISTENER_REASON, is_skip=1)
        self._insertPlay("t1", START + 3, createdAt=START + 9, created_reason=LISTENER_REASON)
        self._insertPlay("t1", START + 10, created_reason=BACKFILL_REASON)

        self.assertEqual(len(self._findSkips()), 1)
        self.assertEqual(len(self._find()), 1)

        exitCode = sweep.main(["--db", str(self.dbPath), "--apply"])

        self.assertEqual(exitCode, 0)
        self.assertEqual(self._playedAts(), [START, START + 3])


class TestSweepCrossReleaseDuplicates(SweepTestCase):
    """The third pairing: the two copies do not even share a track id.

    Spotify hands the connect player_state and the recently-played endpoint
    different release ids for one recording, so the listener stores the play
    under an id the Web API never mentions and the first two pairings - both of
    which join on l.track_id = b.track_id - reach none of it. Found live
    2026-08-17: 45 rows, 9.6% of every backfill-sourced play on the instance.

    Sameness is the same three signals the live dedup uses
    (PlayQueries._sameRecordingTrackIds), each meaning "same master" rather
    than "probably related" - deleting a play is unrecoverable."""

    LISTENER_ID = "listener_release"
    WEB_API_ID = "web_api_release"
    DURATION_MS = 287000
    ISRC = "DEU601606324"

    def _variant(self, trackId, *, name="Same Song", durationMs=DURATION_MS, isrc="", artistId="a1"):
        track = _track(trackId)
        track["name"] = name
        track["duration"] = durationMs
        track["isrc"] = isrc
        track["artists"] = [{"id": artistId, "name": f"Artist {artistId}",
                             "url": f"http://example.com/artist/{artistId}",
                             "imageUrl": "", "imageId": artistId}]
        self.repo.upsertTrack(track)
        self.repo.commit()

    def _findCrossRelease(self, tolerance=10):
        return sweep.findBackfillCrossReleaseDuplicates(self._sweepConn(), tolerance)

    def _recordBothCopies(self):
        """The live shape: the listener's row under one release id, the Web
        API's copy of the same listen under the other, its played_at landing on
        the listener row's observed end."""
        self._insertPlay(self.LISTENER_ID, START, createdAt=END, created_reason=LISTENER_REASON)
        self._insertPlay(self.WEB_API_ID, END + 1, created_reason=BACKFILL_REASON)

    def test_a_copy_under_a_release_sharing_the_isrc_is_found(self):
        self._variant(self.LISTENER_ID, isrc=self.ISRC, name="Album Version")
        self._variant(self.WEB_API_ID, isrc=self.ISRC, name="Single Version")
        self._recordBothCopies()

        found = self._findCrossRelease()

        self.assertEqual([(r["track_id"], r["listener_track_id"], r["played_at"]) for r in found],
                         [(self.WEB_API_ID, self.LISTENER_ID, END + 1)])

    def test_a_copy_under_a_merged_sibling_is_found(self):
        """By the time a sweep runs, the daily ISRC matcher has usually already
        merged the phantom id - the shape most of the live rows were found in."""
        self._variant(self.LISTENER_ID, name="Same Song (Radio Edit)")
        self._variant(self.WEB_API_ID)
        self.repo.mergeTrackManually(self.LISTENER_ID, self.WEB_API_ID, "tester")
        self.repo.commit()
        self._recordBothCopies()

        self.assertEqual(len(self._findCrossRelease()), 1)

    def test_a_copy_under_the_same_title_duration_and_artist_is_found(self):
        """No ISRC and no merge: an id minted minutes ago, which is what the
        listener keeps inventing."""
        self._variant(self.LISTENER_ID, isrc="")
        self._variant(self.WEB_API_ID, isrc=self.ISRC)
        self._recordBothCopies()

        self.assertEqual(len(self._findCrossRelease()), 1)

    def test_a_different_duration_pairs_nothing(self):
        self._variant(self.LISTENER_ID)
        self._variant(self.WEB_API_ID, durationMs=self.DURATION_MS + 1)
        self._recordBothCopies()

        self.assertEqual(self._findCrossRelease(), [])

    def test_a_different_primary_artist_pairs_nothing(self):
        self._variant(self.LISTENER_ID, artistId="a1")
        self._variant(self.WEB_API_ID, artistId="a2")
        self._recordBothCopies()

        self.assertEqual(self._findCrossRelease(), [])

    def test_a_different_title_pairs_nothing(self):
        self._variant(self.LISTENER_ID, name="One Song")
        self._variant(self.WEB_API_ID, name="Another Song")
        self._recordBothCopies()

        self.assertEqual(self._findCrossRelease(), [])

    def test_an_empty_isrc_pairs_nothing_on_its_own(self):
        """Most rows carry isrc = '' until the catalog lookup lands. Reading
        that as a shared master would pair the whole library with itself."""
        self._variant(self.LISTENER_ID, name="One Song", isrc="")
        self._variant(self.WEB_API_ID, name="Another Song", isrc="")
        self._recordBothCopies()

        self.assertEqual(self._findCrossRelease(), [])

    def test_a_listener_end_outside_every_tolerance_pairs_nothing(self):
        """Same recording, but a play that ended minutes away is a DIFFERENT
        listen - the tolerances carry the same meaning they do on the
        same-track pairing."""
        self._variant(self.LISTENER_ID, isrc=self.ISRC)
        self._variant(self.WEB_API_ID, isrc=self.ISRC)
        self._insertPlay(self.LISTENER_ID, START, createdAt=END, created_reason=LISTENER_REASON)
        self._insertPlay(self.WEB_API_ID, END + 11, created_reason=BACKFILL_REASON)

        self.assertEqual(self._findCrossRelease(), [])

    def test_a_copy_at_the_unpaused_end_of_a_paused_listen_is_found(self):
        """Live play 123777 (7kevinegger, 2026-08-16). The listener saw a ~60s
        pause, so its created_at sits 59s after the Web API's played_at - well
        outside the end-time tolerance - while that played_at is start +
        duration to the second. Spotify reported the play's end as though
        nothing had been paused, which is exactly the reading the announce
        dedup's second arm carries and which this pairing therefore needs too:
        without it the sweep silently under-reports."""
        self._variant(self.LISTENER_ID, isrc=self.ISRC)
        self._variant(self.WEB_API_ID, isrc=self.ISRC)
        pauseSeconds = 60
        durationSeconds = self.DURATION_MS // 1000
        self._insertPlay(self.LISTENER_ID, START,
                         createdAt=START + durationSeconds + pauseSeconds,
                         created_reason=LISTENER_REASON)
        self._insertPlay(self.WEB_API_ID, START + durationSeconds, created_reason=BACKFILL_REASON)

        found = self._findCrossRelease()

        self.assertEqual([r["play_id"] is not None for r in found], [True])
        self.assertEqual(len(found), 1)

    def test_a_copy_sharing_the_listener_start_is_found(self):
        """The Web API's played_at read as the START of the play - the announce
        dedup's first arm."""
        self._variant(self.LISTENER_ID, isrc=self.ISRC)
        self._variant(self.WEB_API_ID, isrc=self.ISRC)
        self._insertPlay(self.LISTENER_ID, START, createdAt=END, created_reason=LISTENER_REASON)
        self._insertPlay(self.WEB_API_ID, START + 1, created_reason=BACKFILL_REASON)

        self.assertEqual(len(self._findCrossRelease()), 1)

    def test_a_copy_a_whole_track_away_from_an_unpaused_listen_pairs_nothing(self):
        """The unpaused-end arm must not reach a NEXT listen: gapless playback
        puts a genuinely missing play's start exactly one track-length after
        its predecessor, which is the false positive the same-track pairing was
        already burned by."""
        self._variant(self.LISTENER_ID, isrc=self.ISRC)
        self._variant(self.WEB_API_ID, isrc=self.ISRC)
        durationSeconds = self.DURATION_MS // 1000
        self._insertPlay(self.LISTENER_ID, START, createdAt=START + durationSeconds,
                         created_reason=LISTENER_REASON)
        #< two track-lengths on: neither a start, nor an unpaused end, nor the
        #  recorded end of THIS listen
        self._insertPlay(self.WEB_API_ID, START + 3 * durationSeconds,
                         created_reason=BACKFILL_REASON)

        self.assertEqual(self._findCrossRelease(), [])

    def test_pairing_never_crosses_users(self):
        self._variant(self.LISTENER_ID, isrc=self.ISRC)
        self._variant(self.WEB_API_ID, isrc=self.ISRC)
        self._insertPlay(self.LISTENER_ID, START, createdAt=END, created_reason=LISTENER_REASON)
        self._insertPlay(self.WEB_API_ID, END + 1, created_reason=BACKFILL_REASON, username="bob")

        self.assertEqual(self._findCrossRelease(), [])

    def test_a_same_track_copy_belongs_to_the_end_time_pairing_alone(self):
        """The three pairings partition rather than overlap, so the report's
        per-user counts stay readable."""
        self._insertPlay("t1", START, createdAt=END, created_reason=LISTENER_REASON)
        self._insertPlay("t1", END + 1, created_reason=BACKFILL_REASON)

        self.assertEqual(self._findCrossRelease(), [])
        self.assertEqual(len(self._find()), 1)

    def test_apply_deletes_the_cross_release_copy(self):
        self._variant(self.LISTENER_ID, isrc=self.ISRC)
        self._variant(self.WEB_API_ID, isrc=self.ISRC)
        self._recordBothCopies()
        self._insertPlay("t1", START, created_reason=BACKFILL_REASON)  #< genuine, no sibling

        exitCode = sweep.main(["--db", str(self.dbPath), "--apply"])

        self.assertEqual(exitCode, 0)
        self.assertEqual(self._playedAts(), [START, START])  #< the listener row + the genuine backfill


class TestSweepAgainstAPre149Database(unittest.TestCase):
    """tracks.canonical_id arrived in 1.49, and the cross-release query reads
    it. This tool is pointed at BACKUPS - that is most of what it is for - and
    an older one answered with a bare "no such column" traceback, which reads
    like the tool is broken rather than like the file predates the query.

    Built by hand rather than by dropping the column from a current schema:
    SQLite's DROP COLUMN re-parses the stored schema text and refuses this one,
    which is itself a good reason not to pretend a migration is available here.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.dbPath = Path(self._tmpdir.name) / "legacy.db"
        conn = sqlite3.connect(self.dbPath)
        try:
            with conn:
                conn.execute("CREATE TABLE tracks (id TEXT PRIMARY KEY, name TEXT, "
                             "duration_ms INTEGER, isrc TEXT)")   #< no canonical_id
                conn.execute("CREATE TABLE track_artists (track_id TEXT, artist_id TEXT, position INTEGER)")
                conn.execute("CREATE TABLE plays (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                             "username TEXT, track_id TEXT, played_at REAL, ms_played INTEGER, "
                             "time_played INTEGER, is_skip INTEGER DEFAULT 0, created_at REAL, "
                             "created_reason TEXT)")
                conn.execute("INSERT INTO tracks VALUES ('t1', 'Song', 287000, 'ISRC1')")
                conn.execute("INSERT INTO track_artists VALUES ('t1', 'a1', 0)")
                #< the end-time pairing the sweep was actually written for
                conn.execute("INSERT INTO plays (username, track_id, played_at, time_played, "
                             "created_at, created_reason) VALUES ('alice','t1',?,60000,?,?)",
                             (START, START + 200, LISTENER_REASON))
                conn.execute("INSERT INTO plays (username, track_id, played_at, time_played, "
                             "created_reason) VALUES ('alice','t1',?,60000,?)",
                             (START + 200, BACKFILL_REASON))
        finally:
            conn.close()

    def _playedAts(self):
        conn = sqlite3.connect(self.dbPath)
        try:
            return [r[0] for r in conn.execute("SELECT played_at FROM plays ORDER BY played_at")]
        finally:
            conn.close()

    def test_the_run_completes_instead_of_raising_no_such_column(self):
        exitCode = sweep.main(["--db", str(self.dbPath)])

        self.assertEqual(exitCode, 0)

    def test_the_other_pairings_still_do_their_work(self):
        """Skipping the whole run would be worse than the crash it replaces: a
        pre-1.49 database is exactly where the end-time duplicates are."""
        exitCode = sweep.main(["--db", str(self.dbPath), "--apply"])

        self.assertEqual(exitCode, 0)
        self.assertEqual(self._playedAts(), [START])


if __name__ == "__main__":
    unittest.main()
