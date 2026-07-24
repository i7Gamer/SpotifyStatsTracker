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
        
        # 1. Obsession: 6 plays in the last 7 days
        for i in range(6):
            self.repo.insertPlay("alice", "obsession_track", self.now_ts - (i * 3600), 200000)

        # 2. Rediscovery: 5 plays 200 days ago, 2 plays today
        for i in range(5):
            self.repo.insertPlay("alice", "rediscovery_track", self.now_ts - (200 * self.day) - (i * 100), 200000)
        self.repo.insertPlay("alice", "rediscovery_track", self.now_ts - 1000, 200000)
        self.repo.insertPlay("alice", "rediscovery_track", self.now_ts - 500, 200000)

        # 3. Forgotten: 20 plays 200 days ago, none since
        for i in range(20):
            self.repo.insertPlay("alice", "forgotten_track", self.now_ts - (200 * self.day) - (i * 1000), 200000)

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
