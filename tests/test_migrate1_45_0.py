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
import Database.Migrators.migrate1_45_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_45_0(unittest.TestCase):
    """1.45.0 -> 1.46.0 adds users.display_name: the editable label that stands
    in for the immutable username key wherever a person is named. NULL means
    "display as the username", so there is nothing to backfill - the ALTER's own
    default is already correct for every existing account."""

    DISPLAY_NAME_LINE = "    display_name          TEXT,\n"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.46.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.45.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _preColumnSchema(self):
        """SCHEMA with the display_name line stripped out, simulating a
        pre-1.46.0 database - without this, a fresh Repository() connection
        would create the column via SCHEMA's own CREATE TABLE before the
        migration's ALTER TABLE ever runs."""
        self.assertIn(self.DISPLAY_NAME_LINE, dbModule.SCHEMA)
        return dbModule.SCHEMA.replace(self.DISPLAY_NAME_LINE, "")

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
        self.assertNotIn("display_name", self._columnNames("users"))

        migrateModule.Migrator("1.45.0", "1.46.0").migrate()

        self.assertIn("display_name", self._columnNames("users"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.46.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.46.0")

    def test_an_existing_account_keeps_displaying_as_its_username(self):
        """The no-backfill contract: the column arrives NULL and the read path
        resolves that to the username, so nobody's name changes on upgrade."""
        self._seedOldDatabase()

        migrateModule.Migrator("1.45.0", "1.46.0").migrate()

        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        row = conn.execute("SELECT display_name FROM users WHERE username='someone'").fetchone()
        self.assertIsNone(row[0])

        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        self.assertEqual(repo.getDisplayName("someone"), "someone")

    def test_migration_is_idempotent(self):
        self._seedOldDatabase()
        migrateModule.Migrator("1.45.0", "1.46.0").migrate()

        (self.dataDir / "VERSION").write_text("1.45.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.45.0")
        migrateModule.Migrator("1.45.0", "1.46.0").migrate()   #< must not raise

        self.assertIn("display_name", self._columnNames("users"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.46.0")

    def test_a_name_set_before_a_rerun_survives_it(self):
        """A retry must not clobber data the first run already made reachable -
        the ALTER is guarded, so the second pass has to leave the column alone."""
        self._seedOldDatabase()
        migrateModule.Migrator("1.45.0", "1.46.0").migrate()
        repo = Repository(self.dbPath)
        repo.setDisplayName("someone", "Someone Else")
        repo.connectionManager.close()

        (self.dataDir / "VERSION").write_text("1.45.0", encoding="utf-8")
        dbversion.writeDbVersion(self.dbPath, "1.45.0")
        migrateModule.Migrator("1.45.0", "1.46.0").migrate()

        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        self.assertEqual(repo.getDisplayName("someone"), "Someone Else")

    def test_empty_database_migrates_cleanly(self):
        preSchema = self._preColumnSchema()
        with patch.object(dbModule, "SCHEMA", preSchema):
            Repository(self.dbPath).connectionManager.close()   #< schema only, no users

        migrateModule.Migrator("1.45.0", "1.46.0").migrate()   #< must not raise

        self.assertIn("display_name", self._columnNames("users"))


if __name__ == "__main__":
    unittest.main()
