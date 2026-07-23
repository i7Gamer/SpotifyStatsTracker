"""1.35.0 -> 1.36.0: recalculate user_milestones.achieved_at from play history.

The 1.34.0 seeding pass stamped every already-achieved milestone with the
seeding moment, so existing accounts show their whole backlog as achieved on
migration day. This migration rewrites those dates to what the plays table
says (see services/milestones.py recalculateMilestoneDates and
tests/test_milestone_recalc.py for the per-kind rules); here the migrator
plumbing is under test - version gating, per-user application, and leaving
seen flags / unsupported rows alone.
"""
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_35_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository
from services.milestones import MILESTONE_KIND_PLAYS

# The wrong date the 1.34.0 seeding pass stamped: any value newer than the
# real play history stands in for it here.
SEEDED_AT = 9_999_999_999.0
PLAY_TIMESTAMPS = (100.0, 200.0, 300.0)


def _track(trackId, artistId, albumId):
    return {
        "id": trackId,
        "name": f"Track {trackId}",
        "url": f"http://example.com/track/{trackId}",
        "artists": [{"id": artistId, "name": f"Artist {artistId}",
                     "url": f"http://example.com/artist/{artistId}", "imageUrl": "", "imageId": artistId}],
        "album": {
            "id": albumId, "name": f"Album {albumId}", "url": f"http://example.com/album/{albumId}",
            "imageId": albumId, "imageUrl": "", "totalTracks": 10, "releaseDate": 0.0,
        },
        "imageUrl": "", "imageId": albumId, "duration": 200000, "explicit": False,
        "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0.0,
    }


class TestMigrate1_35_0(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.36.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.35.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _seedDatabaseAt(self, version, withPlays=True):
        """A user whose 2-plays milestone was seeded with the wrong (seeding-
        moment) date; the real second play happened at PLAY_TIMESTAMPS[1]."""
        repo = Repository(self.dbPath)
        repo.upsertUser("someone", "someone@example.com", createdAt=100.0)
        if withPlays:
            repo.upsertTrack(_track("t1", "a1", "al1"))
            for ts in PLAY_TIMESTAMPS:
                repo.insertPlay("someone", "t1", ts, 60000)
        repo.recordMilestone("someone", MILESTONE_KIND_PLAYS, 2, None, SEEDED_AT, seen=True)
        repo.recordMilestone("someone", MILESTONE_KIND_PLAYS, 5, None, SEEDED_AT, seen=False)
        repo.setMilestoneBaselineAt("someone", SEEDED_AT)
        repo.commit()
        repo.connectionManager.close()
        dbversion.writeDbVersion(self.dbPath, version)

    def _milestones(self):
        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        return {r["threshold"]: r for r in repo.getMilestonesForUser("someone")}

    def test_recalculates_seeded_dates_and_bumps_the_markers(self):
        self._seedDatabaseAt("1.35.0")

        migrateModule.Migrator("1.35.0", "1.36.0").migrate()

        rows = self._milestones()
        self.assertEqual(rows[2]["achieved_at"], PLAY_TIMESTAMPS[1])
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.36.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.36.0")

    def test_unsupported_threshold_and_seen_flags_untouched(self):
        self._seedDatabaseAt("1.35.0")

        migrateModule.Migrator("1.35.0", "1.36.0").migrate()

        rows = self._milestones()
        self.assertEqual(rows[5]["achieved_at"], SEEDED_AT)   #< only 3 plays exist - no real date to claim
        self.assertEqual(rows[2]["seen"], 1)
        self.assertEqual(rows[5]["seen"], 0)

    def test_user_without_plays_keeps_seeded_dates(self):
        self._seedDatabaseAt("1.35.0", withPlays=False)

        migrateModule.Migrator("1.35.0", "1.36.0").migrate()

        rows = self._milestones()
        self.assertEqual(rows[2]["achieved_at"], SEEDED_AT)
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.36.0")

    def test_rejects_a_database_not_on_the_from_version(self):
        self._seedDatabaseAt("1.34.0")   #< not 1.35.0
        with self.assertRaises(Exception):
            migrateModule.Migrator("1.35.0", "1.36.0").migrate()

    def test_migration_is_idempotent_on_retry(self):
        self._seedDatabaseAt("1.35.0")
        migrateModule.Migrator("1.35.0", "1.36.0").migrate()

        (self.dataDir / "VERSION").write_text("1.35.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.35.0")
        migrateModule.Migrator("1.35.0", "1.36.0").migrate()   #< must not raise

        rows = self._milestones()
        self.assertEqual(rows[2]["achieved_at"], PLAY_TIMESTAMPS[1])
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.36.0")

    def test_empty_database_migrates_cleanly(self):
        Repository(self.dbPath).connectionManager.close()   #< schema only, no users
        dbversion.writeDbVersion(self.dbPath, "1.35.0")

        migrateModule.Migrator("1.35.0", "1.36.0").migrate()   #< must not raise

        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.36.0")


if __name__ == "__main__":
    unittest.main()
