import sys
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _migrator_case import MigratorHelpersMixin
import Database.db as dbModule
import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_46_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository
from config import TOP_LIST_DEFAULT_WINDOW


class TestMigrate1_46_0(MigratorHelpersMixin, unittest.TestCase):
    """1.46.0 -> 1.47.0 adds users.default_top_list_window: the Top pages' own
    default time window, split off from default_dashboard_window so a "last
    week" dashboard no longer implies a last-week career ranking.

    Nothing to backfill, and that is load-bearing rather than incidental: the
    Top pages were hardcoded to All Time, so the column's DEFAULT is exactly
    what every existing account was already seeing."""

    COLUMN_LINE = "    default_top_list_window TEXT DEFAULT 'all time',\n"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.47.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.46.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _preColumnSchema(self):
        """SCHEMA with the new column stripped out, simulating a pre-1.47.0
        database - without this, a fresh Repository() connection would create
        the column via SCHEMA's own CREATE TABLE before the ALTER ever runs."""
        self.assertIn(self.COLUMN_LINE, dbModule.SCHEMA)
        return dbModule.SCHEMA.replace(self.COLUMN_LINE, "")

    def _seedOldDatabase(self):
        preSchema = self._preColumnSchema()
        with patch.object(dbModule, "SCHEMA", preSchema):
            repo = Repository(self.dbPath)
            repo.upsertUser("someone", "someone@example.com", createdAt=100.0)
            repo.connectionManager.close()
        #< raw SQL, not updateUserSettings: that method now reads and writes the
        #  column this database is deliberately missing. A pre-version fixture
        #  can only use what existed at that version.
        conn = sqlite3.connect(self.dbPath)
        with conn:
            conn.execute("UPDATE users SET default_dashboard_window='week' WHERE username='someone'")
        conn.close()

    def test_adds_the_column_and_bumps_the_version(self):
        self._seedOldDatabase()
        self.assertNotIn("default_top_list_window", self._columnNames("users"))

        migrateModule.Migrator("1.46.0", "1.47.0").migrate()

        self.assertIn("default_top_list_window", self._columnNames("users"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.47.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.47.0")

    def test_an_existing_account_keeps_its_all_time_top_pages(self):
        """The no-backfill contract. This account has "week" stored as its
        DASHBOARD window; its Top pages were still all-time, so the new column
        must arrive at All Time rather than inheriting the neighbour."""
        self._seedOldDatabase()

        migrateModule.Migrator("1.46.0", "1.47.0").migrate()

        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        settings = repo.getUserSettings("someone")
        self.assertEqual(settings["default_top_list_window"], TOP_LIST_DEFAULT_WINDOW)
        self.assertEqual(settings["default_dashboard_window"], "week")

    def test_migration_is_idempotent(self):
        self._seedOldDatabase()
        migrateModule.Migrator("1.46.0", "1.47.0").migrate()

        (self.dataDir / "VERSION").write_text("1.46.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.46.0")
        migrateModule.Migrator("1.46.0", "1.47.0").migrate()   #< must not raise

        self.assertIn("default_top_list_window", self._columnNames("users"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.47.0")

    def test_a_window_set_before_a_rerun_survives_it(self):
        """A retry must not clobber data the first run made reachable - the
        ALTER is guarded, so the second pass has to leave the column alone."""
        self._seedOldDatabase()
        migrateModule.Migrator("1.46.0", "1.47.0").migrate()
        repo = Repository(self.dbPath)
        repo.updateUserSettings("someone", "week", None, default_top_list_window="year")
        repo.connectionManager.close()

        (self.dataDir / "VERSION").write_text("1.46.0", encoding="utf-8")
        dbversion.writeDbVersion(self.dbPath, "1.46.0")
        migrateModule.Migrator("1.46.0", "1.47.0").migrate()

        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        self.assertEqual(repo.getUserSettings("someone")["default_top_list_window"], "year")

    def test_empty_database_migrates_cleanly(self):
        preSchema = self._preColumnSchema()
        with patch.object(dbModule, "SCHEMA", preSchema):
            Repository(self.dbPath).connectionManager.close()   #< schema only, no users

        migrateModule.Migrator("1.46.0", "1.47.0").migrate()   #< must not raise

        self.assertIn("default_top_list_window", self._columnNames("users"))


if __name__ == "__main__":
    unittest.main()
