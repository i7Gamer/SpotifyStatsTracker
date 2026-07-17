import sys
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_20_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import IMAGE_KIND_ARTIST, IMAGE_KIND_TRACK, IMAGE_STATUS_FAILED, IMAGE_STATUS_OK, Repository


class TestMigrate1_20_0(unittest.TestCase):
    """1.20.0 -> 1.21.0 clears every artist image marked 'failed', since all of
    them were marked that way by the now-dead og:image scrape rather than a real
    "no image" signal from Spotify - see migrate1_20_0's docstring. Track images
    and successful ('ok') artist images are untouched."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.21.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.20.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _seedDatabase(self):
        repo = Repository(self.dbPath)
        conn = repo._conn()
        with conn:
            conn.execute("INSERT INTO images (id, kind, status) VALUES (?, ?, ?)",
                         ("artFailed1", IMAGE_KIND_ARTIST, IMAGE_STATUS_FAILED))
            conn.execute("INSERT INTO images (id, kind, status) VALUES (?, ?, ?)",
                         ("artFailed2", IMAGE_KIND_ARTIST, IMAGE_STATUS_FAILED))
            conn.execute("INSERT INTO images (id, kind, status) VALUES (?, ?, ?)",
                         ("artOk", IMAGE_KIND_ARTIST, IMAGE_STATUS_OK))
            conn.execute("INSERT INTO images (id, kind, status) VALUES (?, ?, ?)",
                         ("trackFailed", IMAGE_KIND_TRACK, IMAGE_STATUS_FAILED))
        repo.commit()
        repo.connectionManager.close()

    def _statuses(self):
        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        return {row[0]: (row[1], row[2]) for row in
                conn.execute("SELECT id, kind, status FROM images").fetchall()}

    def test_clears_only_failed_artist_images_and_bumps_the_version(self):
        self._seedDatabase()

        migrateModule.Migrator("1.20.0", "1.21.0").migrate()

        statuses = self._statuses()
        self.assertNotIn("artFailed1", statuses)   #< cleared, retryable under the new fetch path
        self.assertNotIn("artFailed2", statuses)
        self.assertEqual(statuses["artOk"], (IMAGE_KIND_ARTIST, IMAGE_STATUS_OK))         #< untouched
        self.assertEqual(statuses["trackFailed"], (IMAGE_KIND_TRACK, IMAGE_STATUS_FAILED))  #< untouched

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.21.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.21.0")

    def test_migration_is_idempotent(self):
        self._seedDatabase()
        migrateModule.Migrator("1.20.0", "1.21.0").migrate()

        (self.dataDir / "VERSION").write_text("1.20.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.20.0")
        migrateModule.Migrator("1.20.0", "1.21.0").migrate()   #< must not raise

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.21.0")

    def test_empty_database_migrates_cleanly(self):
        Repository(self.dbPath).connectionManager.close()   #< schema only, no rows

        migrateModule.Migrator("1.20.0", "1.21.0").migrate()   #< must not raise

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.21.0")


if __name__ == "__main__":
    unittest.main()
