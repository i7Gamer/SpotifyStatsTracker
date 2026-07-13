import sys
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_8_0 as migrateModule
from Database.repository import Repository


class MigratorTestCase(unittest.TestCase):
    """Builds a temp directory mirroring the real repo-root/Database/{Migrators,Data}/
    layout (resolved relative to base.py's own __file__, which is what
    BaseMigrator.baseDir resolves against) already at 1.8.0, since this
    migrator only ever runs against a database in that shape."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.9.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.8.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _seedPreMigrationDb(self):
        """A users table shaped like it was before password_hash existed -
        this migrator's whole job is adding that column to databases in
        exactly this shape."""
        conn = sqlite3.connect(self.dbPath)
        conn.execute(
            "CREATE TABLE users (username TEXT PRIMARY KEY, email TEXT UNIQUE, "
            "cookies_json TEXT, created_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO users (username, email, created_at) VALUES (?, ?, ?)",
            ("alice", "alice@example.com", 0.0),
        )
        conn.commit()
        conn.close()

    def _repo(self):
        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        return repo


class TestMigrate1_8_0(MigratorTestCase):
    def test_adds_password_hash_column_to_existing_users_table(self):
        self._seedPreMigrationDb()

        migrateModule.Migrator("1.8.0", "1.9.0").migrate()

        conn = sqlite3.connect(self.dbPath)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        conn.close()
        self.assertIn("password_hash", columns)

    def test_existing_rows_survive_with_null_password(self):
        self._seedPreMigrationDb()

        migrateModule.Migrator("1.8.0", "1.9.0").migrate()

        repo = self._repo()
        self.assertIsNone(repo.getUserPasswordHash("alice"))
        self.assertEqual(repo.getEmailForUsername("alice"), "alice@example.com")

    def test_advances_version_marker(self):
        self._seedPreMigrationDb()

        migrateModule.Migrator("1.8.0", "1.9.0").migrate()

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.9.0")

    def test_noop_when_column_already_present(self):
        """Re-running against an already-migrated database (e.g. a retried
        migration step) must not raise."""
        seed = self._repo()
        seed.upsertUser("alice", "alice@example.com")
        seed.commit()
        seed.connectionManager.close()

        migrateModule.Migrator("1.8.0", "1.9.0").migrate()

        repo = self._repo()
        self.assertIsNone(repo.getUserPasswordHash("alice"))


if __name__ == "__main__":
    unittest.main()
