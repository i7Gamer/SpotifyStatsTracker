import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_25_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_25_0(unittest.TestCase):
    """1.25.0 -> 1.26.0 adds artists.bio and artists.bio_attempted_at for the
    artist-bio feature."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.26.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.25.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def test_adds_bio_columns_and_bumps_the_version(self):
        Repository(self.dbPath).connectionManager.close()   #< schema only

        migrateModule.Migrator("1.25.0", "1.26.0").migrate()

        import sqlite3
        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(artists)").fetchall()}
        self.assertIn("bio", columns)
        self.assertIn("bio_attempted_at", columns)

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.26.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.26.0")

    def test_migration_is_idempotent(self):
        Repository(self.dbPath).connectionManager.close()
        migrateModule.Migrator("1.25.0", "1.26.0").migrate()

        (self.dataDir / "VERSION").write_text("1.25.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.25.0")
        migrateModule.Migrator("1.25.0", "1.26.0").migrate()   #< must not raise

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.26.0")


if __name__ == "__main__":
    unittest.main()
