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
import Database.Migrators.migrate1_17_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_17_0(unittest.TestCase):
    """1.17.0 -> 1.18.0 adds users.is_admin and promotes the earliest-created
    user to admin (whoever set the instance up) when no admin exists."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.18.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.17.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _preColumnSchema(self):
        """SCHEMA with is_admin stripped out, simulating a pre-1.18.0 database -
        without this, a fresh Repository() connection would create the column
        via SCHEMA's own CREATE TABLE before the migration's ALTER TABLE ever
        runs, making the test pass even if the migration's own DDL were broken."""
        line = "    is_admin              INTEGER NOT NULL DEFAULT 0,\n"
        self.assertIn(line, dbModule.SCHEMA)   #< the line we're about to strip really exists
        return dbModule.SCHEMA.replace(line, "")

    def _seedUsers(self):
        repo = Repository(self.dbPath)
        repo.upsertUser("newer", "newer@example.com", createdAt=200.0)
        repo.upsertUser("older", "older@example.com", createdAt=100.0)
        repo.connectionManager.close()

    def _columnNames(self):
        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        return {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}

    def test_adds_the_column_and_promotes_the_earliest_user(self):
        preSchema = self._preColumnSchema()
        with patch.object(dbModule, "SCHEMA", preSchema):
            self._seedUsers()
            self.assertNotIn("is_admin", self._columnNames())

        migrateModule.Migrator("1.17.0", "1.18.0").migrate()

        self.assertIn("is_admin", self._columnNames())
        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        self.assertTrue(repo.isAdmin("older"))
        self.assertFalse(repo.isAdmin("newer"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.18.0")

    def test_migration_is_idempotent_and_keeps_the_existing_admin(self):
        preSchema = self._preColumnSchema()
        with patch.object(dbModule, "SCHEMA", preSchema):
            self._seedUsers()

        migrateModule.Migrator("1.17.0", "1.18.0").migrate()

        # The instance owner reassigned admin before a retried upgrade - the
        # re-run must not promote a second admin or flip it back.
        repo = Repository(self.dbPath)
        repo.setUserAdmin("older", False)
        repo.setUserAdmin("newer", True)
        repo.connectionManager.close()

        (self.dataDir / "VERSION").write_text("1.17.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.17.0")   #< the in-db marker is authoritative now too
        migrateModule.Migrator("1.17.0", "1.18.0").migrate()   #< must not raise

        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        self.assertEqual(repo.getAdminUsernames(), ["newer"])
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.18.0")

    def test_empty_database_migrates_without_promoting_anyone(self):
        preSchema = self._preColumnSchema()
        with patch.object(dbModule, "SCHEMA", preSchema):
            Repository(self.dbPath).connectionManager.close()   #< creates the schema, no users

        migrateModule.Migrator("1.17.0", "1.18.0").migrate()   #< must not raise

        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        self.assertEqual(repo.getAdminUsernames(), [])


if __name__ == "__main__":
    unittest.main()
