import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import sqlite3

import Database.db as dbModule
import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_21_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_21_0(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.22.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.21.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _repo(self):
        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        return repo

    def test_creates_share_links_table_and_bumps_version(self):
        # A pre-1.22.0 DB has users but no share_links table yet.
        conn = self._repo()._conn()
        with conn:
            conn.execute("INSERT INTO users (username, created_at) VALUES ('alice', 0)")
        self._repo().connectionManager.close()

        migrateModule.Migrator("1.21.0", "1.22.0").migrate()

        repo = self._repo()
        conn = repo._conn()

        tableNames = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("share_links", tableNames)

        # The new table is actually usable, not just present.
        with conn:
            conn.execute(
                "INSERT INTO share_links (token, username, kind, year, created_at) "
                "VALUES ('tok123', 'alice', 'wrapped', 2026, 0)"
            )
        row = conn.execute("SELECT username, year FROM share_links WHERE token='tok123'").fetchone()
        self.assertEqual(row["username"], "alice")
        self.assertEqual(row["year"], 2026)

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.22.0")

    def test_migration_ddl_creates_the_table_without_schema_help(self):
        """The plain creates-the-table test above is vacuous for the
        migration's own DDL: every Repository connection runs
        executescript(SCHEMA), which already contains share_links, so the
        table exists before the migrator's CREATE TABLE runs. Here SCHEMA is
        patched back to its pre-share_links form (everything before the
        table's section comment) so only the migration's own DDL can create
        it - a broken/renamed statement in migrate1_21_0.py fails this test."""
        preShareLinksSchema = dbModule.SCHEMA.split("-- Public, tokenized read-only links")[0]
        self.assertNotIn("share_links", preShareLinksSchema)   #< the split really removed it

        with patch.object(dbModule, "SCHEMA", preShareLinksSchema):
            migrateModule.Migrator("1.21.0", "1.22.0").migrate()

        # Inspect via raw sqlite3 so no Repository/SCHEMA runs on connect.
        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master").fetchall()}
        self.assertIn("share_links", names)
        self.assertIn("idx_share_links_username", names)

    def test_migration_is_idempotent_via_create_if_not_exists(self):
        """Running the migration twice (e.g. a retried/interrupted upgrade)
        must not error - the table creation has to tolerate already existing,
        same as every other CREATE TABLE IF NOT EXISTS in this schema."""
        self._repo().connectionManager.close()

        migrateModule.Migrator("1.21.0", "1.22.0").migrate()
        (self.dataDir / "VERSION").write_text("1.21.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.21.0")   #< the in-db marker is authoritative now too
        migrateModule.Migrator("1.21.0", "1.22.0").migrate()   #< must not raise

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.22.0")


if __name__ == "__main__":
    unittest.main()
