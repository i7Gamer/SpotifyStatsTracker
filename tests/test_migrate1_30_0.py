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
import Database.Migrators.migrate1_30_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_30_0(unittest.TestCase):
    """1.30.0 -> 1.31.0 adds users.spotify_needs_reauth: the flag Web API
    backfill sets when the stored refresh token lacks the
    user-read-recently-played scope, so Profile can surface a
    re-authorization prompt instead of the listener silently failing every
    poll forever."""

    NEEDS_REAUTH_LINE = "    spotify_needs_reauth  INTEGER NOT NULL DEFAULT 0,\n"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.31.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.30.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _preColumnSchema(self):
        """SCHEMA with the spotify_needs_reauth line stripped out, simulating a
        pre-1.31.0 database - without this, a fresh Repository() connection
        would create the column via SCHEMA's own CREATE TABLE before the
        migration's ALTER TABLE ever runs. Removing the whole line (its
        trailing comma included) keeps the users definition valid now that
        later columns follow it."""
        self.assertIn(self.NEEDS_REAUTH_LINE, dbModule.SCHEMA)
        return dbModule.SCHEMA.replace(self.NEEDS_REAUTH_LINE, "")

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

    def test_adds_the_column_and_bumps_the_version(self):
        self._seedOldDatabase()
        self.assertNotIn("spotify_needs_reauth", self._columnNames("users"))

        migrateModule.Migrator("1.30.0", "1.31.0").migrate()

        self.assertIn("spotify_needs_reauth", self._columnNames("users"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.31.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.31.0")

    def test_new_column_defaults_to_not_needing_reauth(self):
        self._seedOldDatabase()
        migrateModule.Migrator("1.30.0", "1.31.0").migrate()

        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        row = conn.execute("SELECT spotify_needs_reauth FROM users WHERE username='someone'").fetchone()
        self.assertEqual(row[0], 0)

    def test_migration_is_idempotent(self):
        self._seedOldDatabase()
        migrateModule.Migrator("1.30.0", "1.31.0").migrate()

        (self.dataDir / "VERSION").write_text("1.30.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.30.0")
        migrateModule.Migrator("1.30.0", "1.31.0").migrate()   #< must not raise

        self.assertIn("spotify_needs_reauth", self._columnNames("users"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.31.0")

    def test_empty_database_migrates_cleanly(self):
        preSchema = self._preColumnSchema()
        with patch.object(dbModule, "SCHEMA", preSchema):
            Repository(self.dbPath).connectionManager.close()   #< schema only, no users

        migrateModule.Migrator("1.30.0", "1.31.0").migrate()   #< must not raise

        self.assertIn("spotify_needs_reauth", self._columnNames("users"))


if __name__ == "__main__":
    unittest.main()
