import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_10_0 as migrateModule
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

        (self.root / "Database" / "VERSION").write_text("1.11.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.10.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _repo(self):
        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        return repo


class TestMigrate1_10_0(MigratorTestCase):
    def test_adds_track_metadata_and_user_api_columns(self):
        # Initialize db with old schema (without created_at/reason and without spotify_* columns)
        conn = self._repo()._conn()
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                "  username TEXT PRIMARY KEY, email TEXT, cookies_json TEXT, password_hash TEXT, created_at REAL"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tracks ("
                "  id TEXT PRIMARY KEY, name TEXT, url TEXT, album_id TEXT, image_id TEXT, duration_ms INTEGER"
                ")"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS plays ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, track_id TEXT, "
                "  played_at REAL, time_played INTEGER, played_from TEXT"
                ")"
            )
        self._repo().connectionManager.close()

        # Run migration
        migrateModule.Migrator("1.10.0", "1.11.0").migrate()

        # Verify columns exist now
        repo = self._repo()
        conn = repo._conn()

        user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        self.assertIn("spotify_client_id", user_cols)
        self.assertIn("spotify_client_secret", user_cols)
        self.assertIn("spotify_refresh_token", user_cols)

        track_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}
        self.assertIn("created_at", track_cols)
        self.assertIn("created_reason", track_cols)

        play_cols = {row["name"] for row in conn.execute("PRAGMA table_info(plays)").fetchall()}
        self.assertIn("created_at", play_cols)
        self.assertIn("created_reason", play_cols)

        # Verify version file is bumped
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.11.0")


if __name__ == "__main__":
    unittest.main()
