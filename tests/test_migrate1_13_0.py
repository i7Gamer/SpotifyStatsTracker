import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_13_0 as migrateModule
from Database.repository import Repository


class TestMigrate1_13_0(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.14.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.13.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _repo(self):
        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        return repo

    def test_adds_columns_and_clears_cached_wrapped(self):
        # Seed a cached Wrapped year (pre-1.14.0 payloads lack the badge fields)
        conn = self._repo()._conn()
        with conn:
            conn.execute("INSERT INTO users (username, created_at) VALUES ('u1', 0)")
            conn.execute(
                """
                INSERT INTO user_wrapped (
                    username, year, calculated_at, max_played_at, total_plays, total_ms,
                    longest_streak, unique_songs, unique_artists, discovered_songs, discovered_artists,
                    time_series_day, time_series_week, time_series_month,
                    top_songs, top_artists, top_albums,
                    discovered_songs_list, discovered_artists_list, discovered_albums_list
                ) VALUES ('u1', 2024, 0, 0, 1, 1, 1, 1, 1, 0, 0,
                          '[]', '[]', '[]', '[]', '[]', '[]', '[]', '[]', '[]')
                """
            )
        self._repo().connectionManager.close()

        migrateModule.Migrator("1.13.0", "1.14.0").migrate()

        repo = self._repo()
        conn = repo._conn()

        track_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}
        self.assertIn("availability_reason", track_cols)
        album_cols = {row["name"] for row in conn.execute("PRAGMA table_info(albums)").fetchall()}
        self.assertIn("backfill_attempted_at", album_cols)

        # Cached Wrapped years must be gone so they recalculate with badge fields
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_wrapped").fetchone()[0], 0)

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.14.0")


if __name__ == "__main__":
    unittest.main()
