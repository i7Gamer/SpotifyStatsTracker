import sys
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_26_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_26_0(unittest.TestCase):
    """1.26.0 -> 1.27.0 requeues corrupted artist biographies: bios fetched
    before the bio.content + sentence-boundary-truncation fix are stuck
    mid-sentence forever (bio IS NOT NULL, so on-demand/backfill fetches
    never revisit them). bio and bio_attempted_at are cleared so the
    corrected extraction re-runs immediately instead of after the 30-day
    retry window."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.27.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.26.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _seedDatabase(self):
        repo = Repository(self.dbPath)
        repo.upsertUser("testuser", "test@example.com")
        conn = repo._conn()
        with conn:
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('arCorrupted', 'Corrupted', '')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('arClean', 'Clean', '')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('arNoBio', 'NoBio', '')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('arNeverAttempted', 'Never', '')")
        repo.setArtistBio("arCorrupted", "This bio was cut off mid-sen")
        repo.setArtistBio("arClean", "This bio ends properly.")
        repo.setArtistBio("arNoBio", None)
        repo.commit()
        repo.connectionManager.close()

    def _row(self, artistId):
        conn = sqlite3.connect(self.dbPath)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn.execute("SELECT bio, bio_attempted_at FROM artists WHERE id=?",
                            (artistId,)).fetchone()

    def test_requeues_corrupted_bios_and_bumps_the_version(self):
        self._seedDatabase()

        migrateModule.Migrator("1.26.0", "1.27.0").migrate()

        corrupted = self._row("arCorrupted")
        self.assertIsNone(corrupted["bio"])
        self.assertIsNone(corrupted["bio_attempted_at"])

        clean = self._row("arClean")
        self.assertEqual(clean["bio"], "This bio ends properly.")
        self.assertIsNotNone(clean["bio_attempted_at"])

        noBio = self._row("arNoBio")
        self.assertIsNone(noBio["bio"])
        self.assertIsNotNone(noBio["bio_attempted_at"])   #< untouched, not a corrupted bio

        neverAttempted = self._row("arNeverAttempted")
        self.assertIsNone(neverAttempted["bio"])
        self.assertIsNone(neverAttempted["bio_attempted_at"])

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.27.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.27.0")

    def test_migration_is_idempotent(self):
        self._seedDatabase()
        migrateModule.Migrator("1.26.0", "1.27.0").migrate()

        (self.dataDir / "VERSION").write_text("1.26.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.26.0")
        migrateModule.Migrator("1.26.0", "1.27.0").migrate()   #< must not raise

        self.assertEqual(self._row("arClean")["bio"], "This bio ends properly.")
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.27.0")

    def test_empty_database_migrates_cleanly(self):
        Repository(self.dbPath).connectionManager.close()   #< schema only, no rows

        migrateModule.Migrator("1.26.0", "1.27.0").migrate()   #< must not raise

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.27.0")


if __name__ == "__main__":
    unittest.main()
