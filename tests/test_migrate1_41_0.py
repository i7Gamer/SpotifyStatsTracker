"""1.41.0 -> 1.42.0: rewrite plays.is_skip so short tracks played in full stop
counting as skips.

The classifier fix (Repository.computeIsSkip, seconds mode) only governs rows
written after it lands - rows already on disk keep whatever the duration-blind
comparison stored. This migration re-runs recomputeSkipFlags, the same op the
admin threshold form triggers, so existing databases self-correct.

The real case: a 22.174s intro under a 30s threshold had all 17 of its plays
flagged as skips, while Spotify's export recorded every one as
skipped=false / trackdone.
"""
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_41_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository, SKIP_MODE_SECONDS

INTRO_MS = 22_174          #< shorter than the 30s threshold: can never reach it
NORMAL_MS = 200_000
THRESHOLD_SECONDS = 30


def _track(trackId, durationMs):
    return {
        "id": trackId,
        "name": f"Track {trackId}",
        "url": f"http://example.com/track/{trackId}",
        "artists": [{"id": "a1", "name": "Artist", "url": "http://example.com/artist/a1",
                     "imageUrl": "", "imageId": "a1"}],
        "album": {
            "id": "al1", "name": "Album", "url": "http://example.com/album/al1",
            "imageId": "al1", "imageUrl": "", "totalTracks": 10, "releaseDate": 0.0,
        },
        "imageUrl": "", "imageId": "al1", "duration": durationMs, "explicit": False,
        "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0.0,
    }


class TestMigrate1_41_0(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.42.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.41.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _seedDatabaseAt(self, version):
        """A complete play of a sub-threshold intro, wrongly stored as a skip,
        plus a genuinely abandoned play of a normal-length track."""
        repo = Repository(self.dbPath)
        repo.upsertUser("someone", "someone@example.com", createdAt=100.0)
        repo.upsertTrack(_track("intro", INTRO_MS))
        repo.upsertTrack(_track("normal", NORMAL_MS))
        repo.setSkipThreshold(SKIP_MODE_SECONDS, THRESHOLD_SECONDS)
        repo.insertPlay("someone", "intro", 1000.0, INTRO_MS)
        repo.insertPlay("someone", "normal", 2000.0, 25_000)
        # Force the pre-fix (duration-blind) state regardless of what insertPlay
        # classifies them as today - this is what the old code left on disk.
        repo._conn().execute("UPDATE plays SET is_skip = 1")
        repo.commit()
        repo.connectionManager.close()
        dbversion.writeDbVersion(self.dbPath, version)

    def _skipFlags(self):
        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        rows = repo._conn().execute("SELECT track_id, is_skip FROM plays").fetchall()
        return {r["track_id"]: r["is_skip"] for r in rows}

    def test_completed_short_track_stops_being_a_skip(self):
        self._seedDatabaseAt("1.41.0")

        migrateModule.Migrator("1.41.0", "1.42.0").migrate()

        self.assertEqual(self._skipFlags()["intro"], 0)

    def test_genuinely_abandoned_play_stays_a_skip(self):
        """The migration must not blanket-clear the column - a real skip of a
        normal-length track is still a skip afterwards."""
        self._seedDatabaseAt("1.41.0")

        migrateModule.Migrator("1.41.0", "1.42.0").migrate()

        self.assertEqual(self._skipFlags()["normal"], 1)

    def test_bumps_both_version_markers(self):
        self._seedDatabaseAt("1.41.0")

        migrateModule.Migrator("1.41.0", "1.42.0").migrate()

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.42.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.42.0")

    def test_rejects_a_database_not_on_the_from_version(self):
        self._seedDatabaseAt("1.40.0")
        with self.assertRaisesRegex(Exception, "does not match migrator's expected from-version"):
            migrateModule.Migrator("1.41.0", "1.42.0").migrate()

    def test_migration_is_idempotent_on_retry(self):
        self._seedDatabaseAt("1.41.0")
        migrateModule.Migrator("1.41.0", "1.42.0").migrate()

        (self.dataDir / "VERSION").write_text("1.41.0", encoding="utf-8")
        dbversion.writeDbVersion(self.dbPath, "1.41.0")
        migrateModule.Migrator("1.41.0", "1.42.0").migrate()   #< must not raise

        self.assertEqual(self._skipFlags(), {"intro": 0, "normal": 1})


if __name__ == "__main__":
    unittest.main()
