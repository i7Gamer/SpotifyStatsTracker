import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_7_0 as migrateModule
from Database.repository import Repository


def _track(trackId):
    return {
        "id": trackId,
        "name": f"Song {trackId}",
        "url": f"http://example.com/track/{trackId}",
        "artists": [],
        "album": {
            "id": "alb1", "name": "Album", "url": "http://example.com/album",
            "imageId": "alb1", "imageUrl": "", "totalTracks": 1, "releaseDate": 12345.0,
        },
        "imageUrl": "", "imageId": "alb1", "duration": 200000, "explicit": False,
        "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 12345.0,
    }


class MigratorTestCase(unittest.TestCase):
    """Builds a temp directory mirroring the real repo-root/Database/{Migrators,Data}/
    layout (resolved relative to base.py's own __file__, which is what
    BaseMigrator.baseDir resolves against) already at 1.7.0/post-SQLite-migration,
    since this migrator only ever runs against a database in that shape."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.8.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.7.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _repo(self):
        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        return repo


class TestMigrate1_7_0(MigratorTestCase):
    def test_removes_zero_and_negative_duration_plays_across_users(self):
        seed = self._repo()
        seed.upsertUser("alice", "alice@example.com")
        seed.upsertUser("bob", "bob@example.com")
        seed.upsertTrack(_track("t1"))
        seed.insertPlay("alice", "t1", 100.0, 0)
        seed.insertPlay("alice", "t1", 200.0, 5000)
        seed.insertPlay("bob", "t1", 300.0, -1)
        seed.commit()
        seed.connectionManager.close()

        migrateModule.Migrator("1.7.0", "1.8.0").migrate()

        repo = self._repo()
        self.assertEqual(repo.getPlaysCount("alice"), 1)
        self.assertEqual(repo.getPlaysCount("bob"), 0)
        self.assertEqual(repo.getPlaysNewestFirst("alice")[0]["timePlayed"], 5000)

    def test_advances_version_marker(self):
        seed = self._repo()
        seed.upsertUser("alice", "alice@example.com")
        seed.commit()
        seed.connectionManager.close()

        migrateModule.Migrator("1.7.0", "1.8.0").migrate()

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.8.0")

    def test_noop_when_no_zero_duration_plays_exist(self):
        seed = self._repo()
        seed.upsertUser("alice", "alice@example.com")
        seed.upsertTrack(_track("t1"))
        seed.insertPlay("alice", "t1", 100.0, 5000)
        seed.commit()
        seed.connectionManager.close()

        migrateModule.Migrator("1.7.0", "1.8.0").migrate()

        repo = self._repo()
        self.assertEqual(repo.getPlaysCount("alice"), 1)
        self.assertEqual(repo.getPlaysNewestFirst("alice")[0]["timePlayed"], 5000)


if __name__ == "__main__":
    unittest.main()
