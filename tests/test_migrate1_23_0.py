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
import Database.Migrators.migrate1_23_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_23_0(unittest.TestCase):
    """Relaxes share_links.year from NOT NULL to nullable (NULL = an
    "all years" share link). SQLite can't ALTER a NOT NULL constraint away,
    so migrate1_23_0.py rebuilds the table - these tests focus on the things
    a naive rebuild gets wrong: autocommit-per-statement leaving the table
    dropped-but-not-renamed on a crash, and sqlite_sequence regressing so a
    deleted row's id gets reused.

    Since Database/db.py's real SCHEMA already has the post-migration
    (nullable) column, every test here must first force share_links into
    existence with the *old* NOT NULL constraint (_createLegacyTable) -
    otherwise the migrator's own idempotency guard would see an
    already-nullable column and skip the rebuild entirely, making the test
    vacuous."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.24.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.23.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _repo(self):
        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        return repo

    def _rawConn(self):
        conn = sqlite3.connect(self.dbPath)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def _yearIsNotNull(self):
        conn = self._rawConn()
        column = next(r for r in conn.execute("PRAGMA table_info(share_links)").fetchall() if r["name"] == "year")
        return bool(column["notnull"])

    def _legacySchema(self):
        preNullableSchema = dbModule.SCHEMA.replace(
            "    year        INTEGER,\n", "    year        INTEGER NOT NULL,\n"
        )
        self.assertNotEqual(preNullableSchema, dbModule.SCHEMA)   #< the replace really matched something
        return preNullableSchema

    def _createLegacyTable(self):
        """Forces share_links into existence with the pre-1.24.0 NOT NULL
        constraint. A later Repository(self.dbPath) opened against today's
        real (already-nullable) SCHEMA leaves this alone - CREATE TABLE IF
        NOT EXISTS never alters a table that already exists."""
        with patch.object(dbModule, "SCHEMA", self._legacySchema()):
            repo = Repository(self.dbPath)
            repo._conn()   #< forces executescript(patched SCHEMA) to actually run now - constructing Repository alone does not
            repo.connectionManager.close()
        self.assertTrue(self._yearIsNotNull())   #< sanity: the legacy table really is NOT NULL

    def _migrate(self):
        migrateModule.Migrator("1.23.0", "1.24.0").migrate()

    def test_relaxes_year_to_nullable_and_bumps_version(self):
        self._createLegacyTable()

        self._migrate()

        self.assertFalse(self._yearIsNotNull())
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.24.0")

    def test_migration_ddl_relaxes_the_constraint_without_schema_help(self):
        """Vacuous otherwise: every Repository connection runs
        executescript(SCHEMA). If SCHEMA itself already had the relaxed
        column, the table would already be nullable before the migrator's
        own rebuild runs. _createLegacyTable already patches SCHEMA back to
        the pre-relax form for exactly this reason - this test just makes
        the intent explicit and re-asserts the starting condition."""
        self._createLegacyTable()

        self._migrate()

        self.assertFalse(self._yearIsNotNull())

    def test_existing_row_survives_the_rebuild_unchanged(self):
        self._createLegacyTable()
        conn = self._rawConn()
        with conn:
            conn.execute("INSERT INTO users (username, created_at) VALUES ('alice', 0)")
            conn.execute(
                "INSERT INTO share_links (token, username, kind, year, created_at, expires_at) "
                "VALUES ('tok-a', 'alice', 'wrapped', 2025, 111.5, NULL)"
            )
            conn.execute(
                "INSERT INTO share_links (token, username, kind, year, created_at, expires_at) "
                "VALUES ('tok-b', 'alice', 'wrapped', 2026, 222.5, 999.0)"
            )
        conn.close()

        self._migrate()

        conn = self._rawConn()
        rowA = conn.execute("SELECT * FROM share_links WHERE token='tok-a'").fetchone()
        rowB = conn.execute("SELECT * FROM share_links WHERE token='tok-b'").fetchone()
        self.assertEqual(rowA["id"], 1)
        self.assertEqual(rowA["username"], "alice")
        self.assertEqual(rowA["kind"], "wrapped")
        self.assertEqual(rowA["year"], 2025)
        self.assertEqual(rowA["created_at"], 111.5)
        self.assertIsNone(rowA["expires_at"])
        self.assertEqual(rowB["id"], 2)
        self.assertEqual(rowB["expires_at"], 999.0)

    def test_autoincrement_id_does_not_reuse_a_deleted_rows_id_after_rebuild(self):
        self._createLegacyTable()
        conn = self._rawConn()
        with conn:
            conn.execute("INSERT INTO users (username, created_at) VALUES ('alice', 0)")
            conn.execute(
                "INSERT INTO share_links (token, username, kind, year, created_at) "
                "VALUES ('tok-a', 'alice', 'wrapped', 2025, 0)"
            )
            conn.execute(
                "INSERT INTO share_links (token, username, kind, year, created_at) "
                "VALUES ('tok-b', 'alice', 'wrapped', 2026, 0)"
            )
            conn.execute("DELETE FROM share_links WHERE token='tok-b'")   #< id 2 now free
        conn.close()

        self._migrate()

        repo2 = self._repo()
        conn2 = repo2._conn()
        with conn2:
            conn2.execute(
                "INSERT INTO share_links (token, username, kind, year, created_at) "
                "VALUES ('tok-c', 'alice', 'wrapped', NULL, 0)"
            )
        row = conn2.execute("SELECT id FROM share_links WHERE token='tok-c'").fetchone()
        self.assertEqual(row["id"], 3)   #< not 2 - the deleted row's id must not be recycled

    def test_all_years_row_can_be_inserted_after_migration(self):
        self._createLegacyTable()
        conn = self._rawConn()
        with conn:
            conn.execute("INSERT INTO users (username, created_at) VALUES ('alice', 0)")
        conn.close()

        self._migrate()

        repo2 = self._repo()
        token = repo2.createShareLink("alice", "wrapped", None, None)
        link = repo2.getShareLink(token)
        self.assertIsNone(link["year"])

    def test_migration_is_idempotent_when_rerun_against_an_already_migrated_db(self):
        self._createLegacyTable()

        self._migrate()
        (self.dataDir / "VERSION").write_text("1.23.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.23.0")
        self._migrate()   #< must not raise

        self.assertFalse(self._yearIsNotNull())
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.24.0")

    def test_check_constraint_on_kind_still_rejects_invalid_values_after_rebuild(self):
        self._createLegacyTable()
        conn = self._rawConn()
        with conn:
            conn.execute("INSERT INTO users (username, created_at) VALUES ('alice', 0)")
        conn.close()

        self._migrate()

        conn = self._rawConn()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO share_links (token, username, kind, year, created_at) "
                "VALUES ('bad', 'alice', 'not-wrapped', 2026, 0)"
            )

    def test_unique_token_constraint_still_enforced_after_rebuild(self):
        self._createLegacyTable()
        conn = self._rawConn()
        with conn:
            conn.execute("INSERT INTO users (username, created_at) VALUES ('alice', 0)")
            conn.execute(
                "INSERT INTO share_links (token, username, kind, year, created_at) "
                "VALUES ('dup', 'alice', 'wrapped', 2025, 0)"
            )
        conn.close()

        self._migrate()

        conn = self._rawConn()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO share_links (token, username, kind, year, created_at) "
                "VALUES ('dup', 'alice', 'wrapped', 2026, 0)"
            )

    def test_index_on_username_still_exists_after_rebuild(self):
        self._createLegacyTable()

        self._migrate()

        conn = self._rawConn()
        names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        self.assertIn("idx_share_links_username", names)


if __name__ == "__main__":
    unittest.main()
