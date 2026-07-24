import unittest
import time
from pathlib import Path
from Database.repository import Repository
from Database.database import Database


def makeTrack(trackId="t1", name="Song One", albumId="alb1", artistId="art1"):
    return {
        "id": trackId,
        "name": name,
        "url": f"http://example.com/track/{trackId}",
        "artists": [
            {"id": artistId, "name": f"Artist {artistId}", "url": f"http://example.com/artist/{artistId}",
             "imageUrl": "", "imageId": artistId},
        ],
        "album": {
            "id": albumId, "name": f"Album {albumId}", "url": f"http://example.com/album/{albumId}",
            "imageId": albumId, "imageUrl": "http://img.example.com/a.jpg",
            "totalTracks": 10, "releaseDate": 12345.0,
        },
        "imageUrl": "http://img.example.com/a.jpg",
        "imageId": albumId,
        "duration": 200000,
        "explicit": False,
        "isrc": "US1234567890",
        "discNumber": 1,
        "trackNumber": 3,
        "releaseDate": 12345.0,
    }


class TestTrendQueries(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(":memory:")
        self.db = Database("alice", dbPath=self.db_path)
        self.repo = self.db.repo
        self.repo.upsertUser("alice", "alice@example.com")
        
        self.now_ts = 1000000.0
        self.day = 86400

        # Catalog
        self.repo.upsertTrack(makeTrack(trackId="obsession_track", name="Obsession Song"))
        self.repo.upsertTrack(makeTrack(trackId="rediscovery_track", name="Rediscovery Song"))
        self.repo.upsertTrack(makeTrack(trackId="forgotten_track", name="Forgotten Song"))
        self.repo.upsertTrack(makeTrack(trackId="partial_track", name="Partial Song"))
        
        # 1. Obsession: 6 plays in the last 7 days
        for i in range(6):
            self.repo.insertPlay("alice", "obsession_track", self.now_ts - (i * 3600), 200000)

        # 2. Rediscovery: 5 plays 200 days ago, 2 plays today
        for i in range(5):
            self.repo.insertPlay("alice", "rediscovery_track", self.now_ts - (200 * self.day) - (i * 100), 200000)
        self.repo.insertPlay("alice", "rediscovery_track", self.now_ts - 1000, 200000)
        self.repo.insertPlay("alice", "rediscovery_track", self.now_ts - 500, 200000)

        # 3. Forgotten: 20 full plays 200 days ago, none since
        for i in range(20):
            self.repo.insertPlay("alice", "forgotten_track", self.now_ts - (200 * self.day) - (i * 1000), 200000)

        # 4. Partial-only: 30 plays 300 days ago that never finished (25% of the
        # 200000ms duration - well past the skip threshold but under the 80%
        # completion-complete percent). Despite outnumbering forgotten_track's
        # raw play count, none of these are full listens, so this track must
        # never win (or even qualify for) the Forgotten Favorite slot.
        for i in range(30):
            self.repo.insertPlay("alice", "partial_track", self.now_ts - (300 * self.day) - (i * 1000), 50000)

        self.repo.commit()

    def tearDown(self):
        self.repo.connectionManager.close()

    def test_get_dashboard_trends_raw(self):
        raw = self.repo.getDashboardTrendsRaw("alice", now_ts=self.now_ts)
        self.assertIsNotNone(raw["obsession"])
        self.assertEqual(raw["obsession"]["track_id"], "obsession_track")
        self.assertGreaterEqual(raw["obsession"]["recent_count"], 5)

        self.assertIsNotNone(raw["rediscovery"])
        self.assertEqual(raw["rediscovery"]["track_id"], "rediscovery_track")

        self.assertIsNotNone(raw["forgotten"])
        self.assertEqual(raw["forgotten"]["track_id"], "forgotten_track")
        self.assertEqual(raw["forgotten"]["total_plays"], 20)

    def test_forgotten_favorite_excludes_partial_plays(self):
        # partial_track has 30 raw plays (more than forgotten_track's 20) but
        # none are full listens, so it must not outrank or replace forgotten_track.
        raw = self.repo.getDashboardTrendsRaw("alice", now_ts=self.now_ts)
        self.assertEqual(raw["forgotten"]["track_id"], "forgotten_track")

        # Directly confirm partial_track never qualifies at all, even alone.
        self.repo.connection().execute("DELETE FROM plays WHERE track_id = 'forgotten_track'")
        self.repo.commit()
        raw = self.repo.getDashboardTrendsRaw("alice", now_ts=self.now_ts)
        self.assertIsNone(raw["forgotten"])

    def test_obsession_excludes_skips(self):
        # A track skipped many times in the last 7 days is not an obsession -
        # an obsession has to be something actually listened to. Even though
        # skip_spam_track has more raw recent rows (10) than obsession_track (6),
        # every one of them is a skip, so obsession_track must still win.
        self.repo.upsertTrack(makeTrack(trackId="skip_spam_track", name="Skip Spam"))
        for i in range(10):
            self.repo.insertPlay("alice", "skip_spam_track", self.now_ts - (i * 60), 3000, is_skip=1)
        self.repo.commit()

        raw = self.repo.getDashboardTrendsRaw("alice", now_ts=self.now_ts)
        self.assertEqual(raw["obsession"]["track_id"], "obsession_track")
        self.assertEqual(raw["obsession"]["recent_count"], 6)

    def test_rediscovery_excludes_skips(self):
        # A track whose only recent plays are skips has not been "rediscovered".
        # skip_redisc_track has more real old plays (6) than rediscovery_track (5)
        # and 3 recent rows, but those recent rows are all skips - so once skips
        # are excluded its recent_count is 0 and it fails the >=1 recent gate,
        # leaving rediscovery_track as the winner.
        self.repo.upsertTrack(makeTrack(trackId="skip_redisc_track", name="Skip Redisc"))
        for i in range(6):
            self.repo.insertPlay("alice", "skip_redisc_track", self.now_ts - (200 * self.day) - (i * 100), 200000)
        for i in range(3):
            self.repo.insertPlay("alice", "skip_redisc_track", self.now_ts - (i * 100) - 200, 3000, is_skip=1)
        self.repo.commit()

        raw = self.repo.getDashboardTrendsRaw("alice", now_ts=self.now_ts)
        self.assertEqual(raw["rediscovery"]["track_id"], "rediscovery_track")

    def test_get_dashboard_trends_hydrated(self):
        trends = self.db.getDashboardTrends(now_ts=self.now_ts)
        self.assertIsNotNone(trends["obsession"])
        self.assertEqual(trends["obsession"]["id"], "obsession_track")
        self.assertIn("plays", trends["obsession"]["trend_subtitle"])

        self.assertIsNotNone(trends["rediscovery"])
        self.assertEqual(trends["rediscovery"]["id"], "rediscovery_track")
        self.assertIn("unplayed for", trends["rediscovery"]["trend_subtitle"])

        self.assertIsNotNone(trends["forgotten"])
        self.assertEqual(trends["forgotten"]["id"], "forgotten_track")
        self.assertIn("plays all-time", trends["forgotten"]["trend_subtitle"])
