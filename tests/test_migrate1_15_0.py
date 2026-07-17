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
import Database.Migrators.migrate1_15_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_15_0(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.16.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.15.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _preColumnSchema(self):
        """SCHEMA with requester_seen_accepted stripped out, simulating a
        pre-1.16.0 database - without this, a fresh Repository() connection
        would create the column via SCHEMA's own CREATE TABLE before the
        migration's ALTER TABLE ever runs, making the test pass even if the
        migration's own DDL were broken."""
        line = "    requester_seen_accepted INTEGER NOT NULL DEFAULT 0,\n"
        self.assertIn(line, dbModule.SCHEMA)   #< the line we're about to strip really exists
        return dbModule.SCHEMA.replace(line, "")

    def test_adds_the_column_and_bumps_version(self):
        preSchema = self._preColumnSchema()
        with patch.object(dbModule, "SCHEMA", preSchema):
            repo = Repository(self.dbPath)
            repo.upsertUser("alice", "alice@example.com")
            repo.upsertUser("bob", "bob@example.com")
            repo.createShareRequest("alice", "bob")
            repo.commit()
            repo.connectionManager.close()

            conn = sqlite3.connect(self.dbPath)
            columnsBefore = {row[1] for row in conn.execute("PRAGMA table_info(user_shares)").fetchall()}
            conn.close()
            self.assertNotIn("requester_seen_accepted", columnsBefore)

        migrateModule.Migrator("1.15.0", "1.16.0").migrate()

        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        columnsAfter = {row[1] for row in conn.execute("PRAGMA table_info(user_shares)").fetchall()}
        self.assertIn("requester_seen_accepted", columnsAfter)

        # Existing rows default to 0/unseen rather than NULL or erroring.
        value = conn.execute(
            "SELECT requester_seen_accepted FROM user_shares WHERE requester_username='alice'"
        ).fetchone()[0]
        self.assertEqual(value, 0)

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.16.0")

    def test_migration_is_idempotent(self):
        """Running the migration twice (e.g. a retried/interrupted upgrade)
        must not error - addRequesterSeenAcceptedColumnIfMissing is guarded
        the same way every other addXIfMissing helper in this file is."""
        preSchema = self._preColumnSchema()
        with patch.object(dbModule, "SCHEMA", preSchema):
            Repository(self.dbPath).connectionManager.close()

        migrateModule.Migrator("1.15.0", "1.16.0").migrate()
        (self.dataDir / "VERSION").write_text("1.15.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.15.0")   #< the in-db marker is authoritative now too
        migrateModule.Migrator("1.15.0", "1.16.0").migrate()   #< must not raise

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.16.0")


if __name__ == "__main__":
    unittest.main()
