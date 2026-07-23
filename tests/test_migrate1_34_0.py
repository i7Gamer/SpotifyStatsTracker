import sys
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_34_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_34_0(unittest.TestCase):
    """1.34.0 -> 1.35.0 is a version-only (no-op) migration: the 1.35.0 features
    (streak calendar, the /history split + Next-milestones panel, Insights/
    Account nav grouping, deferred charts/genres AJAX, profile "show more"
    milestones) are all UI/route-only. The migration chain still needs a step
    for every consecutive minor version, so this one exists purely to advance
    the marker - it must NOT touch the schema, and it must reject a database
    that isn't actually on 1.34.0."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.35.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.34.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _columnNames(self, table):
        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _seedDatabaseAt(self, version):
        repo = Repository(self.dbPath)
        repo.upsertUser("someone", "someone@example.com", createdAt=100.0)
        repo.connectionManager.close()
        dbversion.writeDbVersion(self.dbPath, version)

    def test_bumps_the_version_marker(self):
        self._seedDatabaseAt("1.34.0")

        migrateModule.Migrator("1.34.0", "1.35.0").migrate()

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.35.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.35.0")

    def test_leaves_the_schema_untouched(self):
        self._seedDatabaseAt("1.34.0")
        before = self._columnNames("users")

        migrateModule.Migrator("1.34.0", "1.35.0").migrate()

        self.assertEqual(self._columnNames("users"), before)

    def test_rejects_a_database_not_on_the_from_version(self):
        self._seedDatabaseAt("1.33.0")   #< not 1.34.0
        with self.assertRaises(Exception):
            migrateModule.Migrator("1.34.0", "1.35.0").migrate()

    def test_migration_is_idempotent_on_retry(self):
        self._seedDatabaseAt("1.34.0")
        migrateModule.Migrator("1.34.0", "1.35.0").migrate()

        (self.dataDir / "VERSION").write_text("1.34.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.34.0")
        migrateModule.Migrator("1.34.0", "1.35.0").migrate()   #< must not raise

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.35.0")

    def test_empty_database_migrates_cleanly(self):
        Repository(self.dbPath).connectionManager.close()   #< schema only, no users
        dbversion.writeDbVersion(self.dbPath, "1.34.0")

        migrateModule.Migrator("1.34.0", "1.35.0").migrate()   #< must not raise

        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.35.0")


if __name__ == "__main__":
    unittest.main()
