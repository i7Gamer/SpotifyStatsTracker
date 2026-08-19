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
import Database.Migrators.migrate1_50_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_50_0(unittest.TestCase):
    """1.50.0 -> 1.51.0 adds users.session_version: the counter a signed
    session cookie carries a copy of, so a password reset (or "Sign out
    everywhere") can end sessions on devices the browser doing it has no
    reach over.

    It arrives at 0 for everyone, which is deliberately the SAME value a
    session minted before the column existed reads as - so the upgrade logs
    nobody out. The first bump is what invalidates those older cookies, and by
    then their owner has asked for exactly that."""

    COLUMN_SQL = "session_version       INTEGER NOT NULL DEFAULT 0"
    COLUMN_COMMENT_START = "-- Bumped to end every session this account has anywhere"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.51.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.50.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _preMigrationSchema(self):
        """SCHEMA with the new column stripped, standing in for a pre-1.51.0
        database - without it a fresh Repository() connection would create the
        users table WITH the column and the migration would have nothing to
        prove.

        session_version is the LAST column of users, so removing it also has to
        take the comma that ended the line before it, or what is left is not
        valid SQL and the fixture fails in a way that looks like a schema bug.
        Its comment block goes with it, so the fixture reads as a believable
        older file rather than one documenting a column it does not have."""
        schema = dbModule.SCHEMA
        self.assertIn(self.COLUMN_SQL, schema)

        start = schema.index(self.COLUMN_COMMENT_START)
        head = schema[:start].rstrip()
        self.assertTrue(head.endswith(","), "expected session_version to follow another column")
        return head[:-1] + "\n" + schema[schema.index(self.COLUMN_SQL) + len(self.COLUMN_SQL):]

    #< closed before returning, not via addCleanup: an inspection connection
    #  left open across the migration blocks its database snapshot on Windows,
    #  which reads like a migrator bug and is not one
    def _columnNames(self, table):
        conn = sqlite3.connect(self.dbPath)
        try:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        finally:
            conn.close()

    def _seedOldDatabase(self):
        with patch.object(dbModule, "SCHEMA", self._preMigrationSchema()):
            repo = Repository(self.dbPath)
            repo._conn()   #< Repository connects lazily, and connecting is what stamps SCHEMA
            repo.upsertUser("alice", "alice@example.com")
            repo.commit()
            repo.connectionManager.close()

    def test_adds_the_column_and_bumps_the_version(self):
        self._seedOldDatabase()
        self.assertNotIn("session_version", self._columnNames("users"))

        migrateModule.Migrator("1.50.0", "1.51.0").migrate()

        self.assertIn("session_version", self._columnNames("users"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.51.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.51.0")

    def test_an_existing_user_arrives_at_version_zero(self):
        """Not NULL and not 1: every session cookie already out there carries
        no version at all, and those have to keep working. Zero is what a
        missing one is read as."""
        self._seedOldDatabase()

        migrateModule.Migrator("1.50.0", "1.51.0").migrate()

        conn = sqlite3.connect(self.dbPath)
        try:
            row = conn.execute("SELECT username, session_version FROM users").fetchone()
            self.assertEqual(row[0], "alice")
            self.assertEqual(row[1], 0)
        finally:
            conn.close()

    def test_a_fresh_database_needs_no_migration(self):
        """SCHEMA carries the column, so a new install is born at 1.51.0's
        shape - the two DDL sites have to agree or an upgraded database and a
        fresh one end up different."""
        repo = Repository(self.dbPath)
        repo._conn()   #< see _seedOldDatabase: connecting is what stamps SCHEMA
        repo.connectionManager.close()

        self.assertIn("session_version", self._columnNames("users"))

    def test_migration_is_idempotent(self):
        self._seedOldDatabase()
        migrateModule.Migrator("1.50.0", "1.51.0").migrate()

        (self.dataDir / "VERSION").write_text("1.50.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.50.0")
        migrateModule.Migrator("1.50.0", "1.51.0").migrate()   #< must not raise

        self.assertIn("session_version", self._columnNames("users"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.51.0")

    def test_a_rerun_does_not_reset_a_version_already_bumped(self):
        """A retry must not hand back sessions the user has already ended."""
        self._seedOldDatabase()
        migrateModule.Migrator("1.50.0", "1.51.0").migrate()
        conn = sqlite3.connect(self.dbPath)
        with conn:
            conn.execute("UPDATE users SET session_version = 3 WHERE username = 'alice'")
        conn.close()

        (self.dataDir / "VERSION").write_text("1.50.0", encoding="utf-8")
        dbversion.writeDbVersion(self.dbPath, "1.50.0")
        migrateModule.Migrator("1.50.0", "1.51.0").migrate()

        conn = sqlite3.connect(self.dbPath)
        try:
            self.assertEqual(
                conn.execute("SELECT session_version FROM users WHERE username='alice'").fetchone()[0],
                3)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
