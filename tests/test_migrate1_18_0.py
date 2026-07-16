import sys
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.db as dbModule
import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_18_0 as migrateModule
from Database.repository import Repository


class TestMigrate1_18_0(unittest.TestCase):
    """1.18.0 -> 1.19.0 adds the Last.fm genre-backfill columns:
    users.lastfm_api_key and lastfm_attempted_at on artists/albums/tracks.
    (The genre join tables and app_settings are plain CREATE TABLE IF NOT
    EXISTS in SCHEMA, so they need no migration.)"""

    API_KEY_LINE = "    lastfm_api_key        TEXT,\n"
    ATTEMPTED_LINE = "    lastfm_attempted_at REAL,\n"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.19.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.18.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _preColumnSchema(self):
        """SCHEMA with the Last.fm columns stripped out, simulating a
        pre-1.19.0 database - without this, a fresh Repository() connection
        would create the columns via SCHEMA's own CREATE TABLE before the
        migration's ALTER TABLE ever runs."""
        self.assertIn(self.API_KEY_LINE, dbModule.SCHEMA)
        self.assertEqual(dbModule.SCHEMA.count(self.ATTEMPTED_LINE), 3)   #< artists, albums, tracks
        return dbModule.SCHEMA.replace(self.API_KEY_LINE, "").replace(self.ATTEMPTED_LINE, "")

    def _columnNames(self, table):
        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _seedOldDatabase(self):
        preSchema = self._preColumnSchema()
        with patch.object(dbModule, "SCHEMA", preSchema):
            repo = Repository(self.dbPath)
            repo.upsertUser("someone", "someone@example.com", createdAt=100.0)
            repo.connectionManager.close()

    def test_adds_all_four_columns_and_bumps_the_version(self):
        self._seedOldDatabase()
        self.assertNotIn("lastfm_api_key", self._columnNames("users"))
        for table in ("artists", "albums", "tracks"):
            self.assertNotIn("lastfm_attempted_at", self._columnNames(table))

        migrateModule.Migrator("1.18.0", "1.19.0").migrate()

        self.assertIn("lastfm_api_key", self._columnNames("users"))
        for table in ("artists", "albums", "tracks"):
            self.assertIn("lastfm_attempted_at", self._columnNames(table))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.19.0")

    def test_migration_is_idempotent(self):
        self._seedOldDatabase()
        migrateModule.Migrator("1.18.0", "1.19.0").migrate()

        (self.dataDir / "VERSION").write_text("1.18.0", encoding="utf-8")   #< simulate a retry
        migrateModule.Migrator("1.18.0", "1.19.0").migrate()   #< must not raise

        self.assertIn("lastfm_api_key", self._columnNames("users"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.19.0")

    def test_empty_database_migrates_cleanly(self):
        preSchema = self._preColumnSchema()
        with patch.object(dbModule, "SCHEMA", preSchema):
            Repository(self.dbPath).connectionManager.close()   #< schema only, no users

        migrateModule.Migrator("1.18.0", "1.19.0").migrate()   #< must not raise

        self.assertIn("lastfm_api_key", self._columnNames("users"))


if __name__ == "__main__":
    unittest.main()
