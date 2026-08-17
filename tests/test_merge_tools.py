"""The two merge-measurement tools under tools/.

They are dev utilities, not shipped code, but they are pointed by hand at a
database path - and in this repo the dev checkout's Database/Data IS the live
data. That makes "what does this do to the file I named" the thing worth
pinning:

  * measure_track_merge_candidates.py documents itself as read-only, and was
    not: a plain sqlite3.connect() CREATES the file, so a typo left a 0-byte
    database behind and reported zero of everything about it.
  * benchmark_merge_read_path.py rewrites canonical_id across the whole tracks
    table and inserts synthetic plays. Aimed at the wrong path, that is not a
    measurement, it is a merge nobody asked for on real listening history.

Plus the two ways each of them fell over before doing anything useful: a
module-level sys.argv[1] that made `--help` an IndexError, and an empty
database that reached a fetchone()[0].
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'tools')))

import benchmark_merge_read_path as benchmark
import measure_track_merge_candidates as measure

DURATION_TOLERANCE_MS = measure.DURATION_TOLERANCE_MS


class _TempDbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.dbPath = Path(self.tmpdir) / "stats.db"

    def _cleanup(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _makeDb(self, tracks=(), plays=()):
        conn = sqlite3.connect(self.dbPath)
        with conn:
            conn.execute("CREATE TABLE tracks (id TEXT PRIMARY KEY, name TEXT, duration_ms INTEGER, "
                         "isrc TEXT, canonical_id TEXT)")
            conn.execute("CREATE TABLE track_artists (track_id TEXT, artist_id TEXT, position INTEGER)")
            conn.execute("CREATE TABLE plays (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, "
                         "track_id TEXT, played_at REAL, time_played INTEGER, is_skip INTEGER DEFAULT 0, "
                         "UNIQUE(username, track_id, played_at))")
            for trackId, name, duration, isrc, artist in tracks:
                conn.execute("INSERT INTO tracks (id, name, duration_ms, isrc) VALUES (?,?,?,?)",
                             (trackId, name, duration, isrc))
                conn.execute("INSERT INTO track_artists VALUES (?,?,0)", (trackId, artist))
            for trackId, playedAt in plays:
                conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                             "VALUES ('alice',?,?,1000)", (trackId, playedAt))
        conn.close()
        return self.dbPath


class TestMeasureToolIsActuallyReadOnly(_TempDbTestCase):
    def test_a_mistyped_path_fails_instead_of_creating_a_database(self):
        """The failure mode this replaces is silent: connect() makes the file,
        every query answers 0 rows, and the tool prints a confident report
        about a database that does not exist."""
        missing = Path(self.tmpdir) / "typo.db"

        with self.assertRaises(sqlite3.OperationalError):
            measure.load(missing)

        self.assertFalse(missing.exists(), "a read-only tool must not create its input")

    def test_it_cannot_write_to_the_database_it_was_given(self):
        self._makeDb(tracks=[("t1", "Song", 200000, "ISRC1", "art1")])

        conn = measure.openReadOnly(self.dbPath)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE tracks SET name='rewritten'")
        finally:
            conn.close()

    def test_it_still_reads_a_real_database(self):
        self._makeDb(tracks=[("t1", "Song", 200000, "ISRC1", "art1")],
                     plays=[("t1", 1000.0)])

        tracks, plays = measure.load(self.dbPath)

        self.assertEqual([t["id"] for t in tracks], ["t1"])
        self.assertEqual(plays, {"t1": 1})


class TestMeasureToolDurationGrouping(unittest.TestCase):
    """`duration_ms or 0` turned a NULL into a zero-length track, so one
    unstamped release dragged the group's spread past the tolerance and the
    WHOLE group - every valid member with it - was discarded. The tool's
    numbers are what the merge feature's design was argued from, so a silent
    undercount there is not cosmetic."""

    @staticmethod
    def _track(trackId, duration):
        return {"id": trackId, "name": "Song", "duration_ms": duration,
                "isrc": None, "artist": "art1"}

    def _groups(self, tracks):
        return measure.groupsBy(lambda t: (t["name"], t["artist"]), tracks)

    def test_a_null_duration_does_not_discard_its_whole_group(self):
        groups = self._groups([
            self._track("t1", 200000),
            self._track("t2", 200000),
            self._track("t3", None),
        ])

        self.assertEqual(len(groups), 1)

    def test_the_group_still_splits_when_the_known_durations_disagree(self):
        """Ignoring NULLs must not become ignoring the rule."""
        groups = self._groups([
            self._track("t1", 200000),
            self._track("t2", 200000 + DURATION_TOLERANCE_MS * 10),
            self._track("t3", None),
        ])

        self.assertEqual(groups, {})

    def test_a_group_of_only_null_durations_is_not_asserted_to_agree(self):
        """With nothing to compare, "same duration" is unproven - and this
        tool's job is to measure the rule, not to be generous about it."""
        groups = self._groups([self._track("t1", None), self._track("t2", None)])

        self.assertEqual(groups, {})

    def test_a_zero_duration_is_treated_as_unknown_too(self):
        """0 ms is not a length any real track has; it is the other spelling
        of "not stamped"."""
        groups = self._groups([
            self._track("t1", 200000),
            self._track("t2", 200000),
            self._track("t3", 0),
        ])

        self.assertEqual(len(groups), 1)


class TestBenchmarkNeverTouchesTheDatabaseItIsGiven(_TempDbTestCase):
    def test_the_simulated_merge_leaves_the_source_file_alone(self):
        """simulateMerge nulls every canonical_id and rewrites the column from
        a title rule. Run against a real database that is the live merge state
        being destroyed - so it runs against a copy, and this is the assertion
        that says so."""
        self._makeDb(tracks=[("t1", "Song", 200000, "ISRC1", "art1"),
                             ("t2", "Song", 200000, "ISRC1", "art1")],
                     plays=[("t1", 1000.0), ("t2", 2000.0)])
        conn = sqlite3.connect(self.dbPath)
        with conn:
            conn.execute("UPDATE tracks SET canonical_id = 't1' WHERE id = 't2'")
        conn.close()

        benchmark.main([str(self.dbPath)])

        conn = sqlite3.connect(self.dbPath)
        try:
            canonical = dict(conn.execute("SELECT id, canonical_id FROM tracks"))
            playCount = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
            columns = {r[1] for r in conn.execute("PRAGMA table_info(plays)")}
        finally:
            conn.close()

        self.assertEqual(canonical, {"t1": None, "t2": "t1"}, "the real merge state survived")
        self.assertEqual(playCount, 2, "no synthetic plays were left behind")
        self.assertNotIn("canonical_track_id", columns, "no column was added to the real table")

    def test_an_empty_database_is_reported_rather_than_crashing(self):
        """`SELECT username FROM plays ... LIMIT 1` returns None on an empty
        table, and the next character was `[0]`."""
        self._makeDb()

        exitCode = benchmark.main([str(self.dbPath)])

        self.assertNotEqual(exitCode, 0)

    def test_a_missing_database_is_reported_rather_than_created(self):
        missing = Path(self.tmpdir) / "typo.db"

        exitCode = benchmark.main([str(missing)])

        self.assertNotEqual(exitCode, 0)
        self.assertFalse(missing.exists())


class TestBenchmarkCli(unittest.TestCase):
    def test_importing_it_without_arguments_does_not_raise(self):
        """`DB = sys.argv[1]` ran at import, so `--help`, an editor's import,
        and this test file all died on an IndexError before main() existed."""
        self.assertTrue(callable(benchmark.main))

    def test_no_argument_is_reported_rather_than_an_indexerror(self):
        exitCode = benchmark.main([])

        self.assertNotEqual(exitCode, 0)

    def test_the_speedup_column_survives_a_zero_timing(self):
        """A 0.0ms baseline is not reachable on a real clock, but the verdict
        line divides by it and a crash at the last print would throw away the
        whole run's measurements."""
        self.assertEqual(benchmark.speedup(0.0, 5.0), benchmark.SPEEDUP_UNAVAILABLE)
        self.assertEqual(benchmark.speedup(2.0, 5.0), "2.5x")


if __name__ == "__main__":
    unittest.main()
