import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_11_0 as migrateModule
from Database.repository import Repository


class MigratorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.12.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.11.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _repo(self):
        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        return repo


class TestMigrate1_11_0(MigratorTestCase):
    def test_adds_user_settings_columns(self):
        # Initialize db with old schema (without default_dashboard_window and timezone)
        conn = self._repo()._conn()
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                "  username TEXT PRIMARY KEY, email TEXT, cookies_json TEXT, password_hash TEXT, created_at REAL"
                ")"
            )
        self._repo().connectionManager.close()

        # Run migration
        migrateModule.Migrator("1.11.0", "1.12.0").migrate()

        # Verify columns exist now
        repo = self._repo()
        conn = repo._conn()

        user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        self.assertIn("default_dashboard_window", user_cols)
        self.assertIn("timezone", user_cols)

        # Verify version file is bumped
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.12.0")


if __name__ == "__main__":
    unittest.main()
