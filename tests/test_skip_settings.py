"""Instance-wide skip threshold + numeric tunables, stored in app_settings.

The skip threshold is the single admin-tunable boundary between a skip and a
real listen (it replaced the old play_skips split and the 30s completion line).
computeIsSkip is the one classifier; recomputeSkipFlags re-materializes
plays.is_skip when the threshold changes. getIntSetting backs the migrated
numeric constants (Discover count, worker pool sizes).
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
from Database.db import SKIP_THRESHOLD_MS
from Database.repository import (
    SKIP_THRESHOLD_MODE_KEY, SKIP_THRESHOLD_VALUE_KEY,
    SKIP_MODE_SECONDS, SKIP_MODE_PERCENT,
    SKIP_SECONDS_MIN, SKIP_SECONDS_MAX, SKIP_PERCENT_MIN, SKIP_PERCENT_MAX,
    SKIP_THRESHOLD_DEFAULT_MODE, SKIP_THRESHOLD_DEFAULT_VALUE,
    DISCOVER_ARTIST_LIMIT_KEY, DISCOVER_ARTIST_LIMIT_MIN, DISCOVER_ARTIST_LIMIT_MAX,
)


class SkipThresholdSettingsTestCase(DatabaseTestCase):
    def test_defaults_to_seconds_5_when_unset(self):
        db = self._makeDb({}, [])
        mode, value = db.repo.getSkipThreshold()
        self.assertEqual(mode, SKIP_MODE_SECONDS)
        self.assertEqual(value, 5)
        self.assertEqual((SKIP_THRESHOLD_DEFAULT_MODE, SKIP_THRESHOLD_DEFAULT_VALUE), (SKIP_MODE_SECONDS, 5))
        self.assertIsNone(db.repo.getAppSetting(SKIP_THRESHOLD_MODE_KEY))

    def test_round_trips_seconds_and_percent(self):
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, 30)
        self.assertEqual(db.repo.getSkipThreshold(), (SKIP_MODE_SECONDS, 30))
        db.repo.setSkipThreshold(SKIP_MODE_PERCENT, 20)
        self.assertEqual(db.repo.getSkipThreshold(), (SKIP_MODE_PERCENT, 20))

    def test_clamps_seconds_to_bounds(self):
        db = self._makeDb({}, [])
        self.assertEqual(db.repo.setSkipThreshold(SKIP_MODE_SECONDS, 1), (SKIP_MODE_SECONDS, SKIP_SECONDS_MIN))
        self.assertEqual(db.repo.setSkipThreshold(SKIP_MODE_SECONDS, 999), (SKIP_MODE_SECONDS, SKIP_SECONDS_MAX))

    def test_clamps_percent_to_bounds(self):
        db = self._makeDb({}, [])
        self.assertEqual(db.repo.setSkipThreshold(SKIP_MODE_PERCENT, 0), (SKIP_MODE_PERCENT, SKIP_PERCENT_MIN))
        self.assertEqual(db.repo.setSkipThreshold(SKIP_MODE_PERCENT, 100), (SKIP_MODE_PERCENT, SKIP_PERCENT_MAX))

    def test_unknown_mode_rejected(self):
        db = self._makeDb({}, [])
        with self.assertRaises(ValueError):
            db.repo.setSkipThreshold("half", 10)

    def test_corrupt_stored_value_falls_back_to_default(self):
        db = self._makeDb({}, [])
        db.repo.setAppSetting(SKIP_THRESHOLD_MODE_KEY, SKIP_MODE_SECONDS)
        db.repo.setAppSetting(SKIP_THRESHOLD_VALUE_KEY, "not-a-number")
        self.assertEqual(db.repo.getSkipThreshold(), (SKIP_MODE_SECONDS, SKIP_THRESHOLD_DEFAULT_VALUE))


class ComputeIsSkipTestCase(DatabaseTestCase):
    def test_seconds_mode_boundary(self):
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, 30)
        self.assertEqual(db.repo.computeIsSkip(29_999), 1)   #< just under 30s
        self.assertEqual(db.repo.computeIsSkip(30_000), 0)   #< exactly 30s is a real play
        self.assertEqual(db.repo.computeIsSkip(60_000), 0)

    def test_percent_mode_uses_duration(self):
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_PERCENT, 20)
        # 20% of a 200s track = 40s
        self.assertEqual(db.repo.computeIsSkip(39_000, durationMs=200_000), 1)
        self.assertEqual(db.repo.computeIsSkip(40_000, durationMs=200_000), 0)

    def test_percent_mode_unknown_duration_uses_floor(self):
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_PERCENT, 25)
        # No duration -> fall back to the fixed sub-5s floor.
        self.assertEqual(db.repo.computeIsSkip(SKIP_THRESHOLD_MS - 1, durationMs=0), 1)
        self.assertEqual(db.repo.computeIsSkip(SKIP_THRESHOLD_MS, durationMs=None), 0)
        self.assertEqual(db.repo.computeIsSkip(10_000, durationMs=0), 0)   #< 10s, no duration -> not a skip

    def test_threshold_arg_avoids_settings_read(self):
        db = self._makeDb({}, [])
        # Stored threshold is the default (5s), but an explicit override wins.
        self.assertEqual(db.repo.computeIsSkip(10_000, threshold=(SKIP_MODE_SECONDS, 30)), 1)


class IntSettingTestCase(DatabaseTestCase):
    def test_default_when_unset(self):
        db = self._makeDb({}, [])
        self.assertEqual(db.repo.getDiscoverArtistLimit(5), 5)
        self.assertIsNone(db.repo.getAppSetting(DISCOVER_ARTIST_LIMIT_KEY))

    def test_clamps_and_round_trips(self):
        db = self._makeDb({}, [])
        self.assertEqual(db.repo.setIntSetting(DISCOVER_ARTIST_LIMIT_KEY, 999,
                                               DISCOVER_ARTIST_LIMIT_MIN, DISCOVER_ARTIST_LIMIT_MAX),
                         DISCOVER_ARTIST_LIMIT_MAX)
        self.assertEqual(db.repo.getDiscoverArtistLimit(5), DISCOVER_ARTIST_LIMIT_MAX)

    def test_bad_stored_value_falls_back_to_default(self):
        db = self._makeDb({}, [])
        db.repo.setAppSetting(DISCOVER_ARTIST_LIMIT_KEY, "lots")
        self.assertEqual(db.repo.getDiscoverArtistLimit(7), 7)


class RecomputeSkipFlagsTestCase(DatabaseTestCase):
    """Needs the plays.is_skip column (schema change) + real plays."""

    def _skipFlags(self, db, username="testuser"):
        rows = db.repo._conn().execute(
            "SELECT track_id, time_played, is_skip FROM plays WHERE username=? ORDER BY track_id", (username,)
        ).fetchall()
        return {r["track_id"]: r["is_skip"] for r in rows}

    def test_seconds_mode_reclassifies_all_rows(self):
        tracks = {"short": {"id": "short", "name": "Short", "artists": []},
                  "long": {"id": "long", "name": "Long", "artists": []}}
        entries = [
            {"id": "short", "playedAt": 1000.0, "timePlayed": 10_000},   #< 10s
            {"id": "long", "playedAt": 2000.0, "timePlayed": 200_000},   #< 200s
        ]
        db = self._makeDb(tracks, entries)
        # Default 5s: neither is a skip.
        self.assertEqual(self._skipFlags(db), {"short": 0, "long": 0})
        # Raise to 30s and recompute: the 10s play becomes a skip.
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, 30)
        processed = db.repo.recomputeSkipFlags()
        self.assertEqual(processed, 2)
        self.assertEqual(self._skipFlags(db), {"short": 1, "long": 0})

    def test_percent_mode_reclassifies_with_duration(self):
        tracks = {"t": {"id": "t", "name": "T", "artists": [], "duration": 200_000}}
        entries = [{"id": "t", "playedAt": 1000.0, "timePlayed": 30_000}]   #< 30s of a 200s track = 15%
        db = self._makeDb(tracks, entries)
        db.repo.setSkipThreshold(SKIP_MODE_PERCENT, 20)   #< 20% = 40s threshold
        db.repo.recomputeSkipFlags()
        self.assertEqual(self._skipFlags(db)["t"], 1)     #< 30s < 40s -> skip
        db.repo.setSkipThreshold(SKIP_MODE_PERCENT, 10)   #< 10% = 20s threshold
        db.repo.recomputeSkipFlags()
        self.assertEqual(self._skipFlags(db)["t"], 0)     #< 30s >= 20s -> real play

    def test_percent_mode_unknown_duration_uses_floor(self):
        tracks = {"t": {"id": "t", "name": "T", "artists": [], "duration": 0}}
        entries = [{"id": "t", "playedAt": 1000.0, "timePlayed": 10_000}]   #< 10s, unknown duration
        db = self._makeDb(tracks, entries)
        db.repo.setSkipThreshold(SKIP_MODE_PERCENT, 25)
        db.repo.recomputeSkipFlags()
        # Unknown duration falls back to the <5s floor, so a 10s play is NOT a skip.
        self.assertEqual(self._skipFlags(db)["t"], 0)


class ConfigureWorkerPoolsTestCase(DatabaseTestCase):
    """Worker pool sizes are read from settings once at startup (applies after
    restart). Restores the shared class-level executors after the test."""

    def test_reads_worker_counts_from_settings(self):
        from Database.database import Database, ARTIST_BIO_FETCH_WORKERS
        db = self._makeDb({}, [])

        originals = (Database._imageDownloadExecutor,
                     Database._artistBioFetchExecutor,
                     Database._albumBioFetchExecutor)

        def _restore():
            Database._imageDownloadExecutor = originals[0]
            Database._artistBioFetchExecutor = originals[1]
            Database._albumBioFetchExecutor = originals[2]
        self.addCleanup(_restore)

        db.repo.setIntSetting("image_download_workers", 9, 1, 32)
        Database.configureWorkerPools(db.repo)

        self.assertEqual(Database._imageDownloadExecutor._max_workers, 9)
        # Unset pools fall back to the code default.
        self.assertEqual(Database._artistBioFetchExecutor._max_workers, ARTIST_BIO_FETCH_WORKERS)


if __name__ == "__main__":
    import unittest
    unittest.main()
