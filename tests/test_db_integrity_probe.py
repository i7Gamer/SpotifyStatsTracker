"""Repository.checkIntegrity() - the startup probe that names database
corruption out loud instead of letting it surface as unrelated errors scattered
across subsystems.

On 2026-07-15 a corrupt database produced 16 "disk image is malformed" errors
and 22 UNIQUE-constraint failures on track_artists, in three different modules,
within the same minute - and the constraint failures were misdiagnosable as a
concurrency race (they aren't: ConnectionManager gives one connection per
thread, so two writers can't interleave inside a transaction). One unambiguous
line at boot is what that morning was missing.
"""
import sqlite3
import sys
import os
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import Repository


class IntegrityProbeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.dbPath = Path(self._tmpdir.name) / "test.db"
        self.repo = Repository(self.dbPath)
        self.addCleanup(self.repo.connectionManager.close)


class TestCheckIntegrityOnHealthyDatabase(IntegrityProbeTestCase):
    def test_fresh_database_reports_healthy(self):
        result = self.repo.checkIntegrity()

        self.assertTrue(result["ok"])
        self.assertEqual(result["corruption"], [])
        self.assertEqual(result["foreignKeyViolations"], {})

    def test_populated_database_reports_healthy(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertTrack({
            "id": "t1", "name": "Track", "url": "", "duration": 1000, "explicit": False,
            "isrc": "", "discNumber": 1, "trackNumber": 1,
            "artists": [{"id": "a1", "name": "Artist", "url": "", "imageId": "a1"}],
            "album": {"id": "al1", "name": "Album", "url": "", "totalTracks": 1,
                      "releaseDate": 0.0, "imageUrl": ""},
            "imageId": "al1",
        })
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.commit()

        self.assertTrue(self.repo.checkIntegrity()["ok"])


class TestCheckIntegrityFindsForeignKeyViolations(IntegrityProbeTestCase):
    def _insertOrphanedTrackArtist(self, trackId="ghost"):
        """Write a row pointing at a track that doesn't exist. Only reachable
        with foreign_keys=OFF - which is exactly how the real ones got in
        (a table rebuild, or a corrupt-index era) and why nothing since has
        noticed them."""
        conn = self.repo._conn()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with conn:
                conn.execute("INSERT OR IGNORE INTO artists (id, name, url) VALUES ('a1', 'Artist', '')")
                conn.execute(
                    "INSERT INTO track_artists (track_id, artist_id, position) VALUES (?, 'a1', 0)",
                    (trackId,))
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    def test_counts_orphaned_rows_by_table(self):
        self._insertOrphanedTrackArtist()

        result = self.repo.checkIntegrity()

        self.assertEqual(result["foreignKeyViolations"], {"track_artists": 1})

    def test_orphaned_rows_do_not_read_as_corruption(self):
        """A dangling reference is a data-consistency problem, not a damaged
        file - conflating them would either cry corruption over 195 harmless
        legacy rows or hide a genuinely broken database behind them."""
        self._insertOrphanedTrackArtist()

        result = self.repo.checkIntegrity()

        self.assertEqual(result["corruption"], [])

    def test_violations_make_the_result_not_ok(self):
        self._insertOrphanedTrackArtist()

        self.assertFalse(self.repo.checkIntegrity()["ok"])

    def test_aggregates_multiple_violations_in_one_table(self):
        self._insertOrphanedTrackArtist("ghost1")
        self._insertOrphanedTrackArtist("ghost2")

        result = self.repo.checkIntegrity()

        self.assertEqual(result["foreignKeyViolations"], {"track_artists": 2})


class TestCheckIntegrityFindsCorruption(unittest.TestCase):
    """The branch that matters most, exercised against a genuinely damaged
    file rather than a stubbed pragma."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.dbPath = Path(self._tmpdir.name) / "corrupt.db"

    def _buildAndCorrupt(self):
        """Fill enough pages to be worth damaging, close cleanly, then
        overwrite the interior of a data page - the shape of the on-disk damage
        SQLite reports as "database disk image is malformed"."""
        repo = Repository(self.dbPath)
        repo.upsertUser("alice", "alice@example.com")
        for i in range(500):
            repo._conn().execute(
                "INSERT INTO artists (id, name, url) VALUES (?, ?, '')",
                (f"a{i}", f"Artist {i} {'x' * 200}"))
        repo.commit()
        repo.connectionManager.close()

        raw = bytearray(self.dbPath.read_bytes())
        pageSize = int.from_bytes(raw[16:18], "big") or 4096
        # Leave page 1 (the header/schema page) intact so the file still opens;
        # shred the middle of a later page so the damage is found on a scan.
        start = pageSize * 3
        raw[start:start + pageSize] = b"\xa5" * pageSize
        self.dbPath.write_bytes(bytes(raw))

    def test_reports_corruption_without_raising(self):
        self._buildAndCorrupt()

        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        result = repo.checkIntegrity()

        self.assertFalse(result["ok"])
        self.assertTrue(result["corruption"],
                        "quick_check found nothing in a deliberately damaged file")
        self.assertNotEqual(result["corruption"], ["ok"])

    def test_unreadable_database_reports_rather_than_raising(self):
        """The probe runs at startup, before anything else has had a chance to
        report a problem. It has to survive a database too broken to query."""
        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)

        class _ExplodingConn:
            def execute(self, *args, **kwargs):
                raise sqlite3.DatabaseError("database disk image is malformed")

        original = repo._conn
        repo._conn = lambda: _ExplodingConn()
        try:
            result = repo.checkIntegrity()
        finally:
            repo._conn = original

        self.assertFalse(result["ok"])
        self.assertTrue(any("malformed" in str(entry) for entry in result["corruption"]))


if __name__ == "__main__":
    unittest.main()
