import datetime
import json
import time
import unittest
from unittest.mock import patch, MagicMock
from conftest import DatabaseTestCase

import app as appModule
from app import SpotifyDashboardApp
import Database.utils as utilsModule
from Database.Migrators.migrate1_12_0 import Migrator as Migrator_1_12_0


class TestWrappedCacheSchema(DatabaseTestCase):
    def test_migration_creates_table_and_updates_version(self):
        # DatabaseTestCase setups a temp database and runs all schemas/migrations up to current.
        # Let's verify the user_wrapped table exists.
        db = self._makeDb({}, [])
        conn = db.repo.connection()
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_wrapped'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "user_wrapped")


class TestWrappedCacheRepository(DatabaseTestCase):
    def test_repo_cache_operations(self):
        db = self._makeDb({}, [])
        repo = db.repo
        username = "testuser"
        year = 2026

        # Test getMaxPlayedAtInPeriod
        # insert some dummy plays
        repo.upsertTrack({
            "id": "t1", "name": "Song 1", "url": "u1", "imageId": "img1", "duration": 30000,
            "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album 1", "url": "u", "imageId": "i", "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
            "artists": [{"id": "art1", "name": "Artist 1", "url": "u", "imageId": "i"}]
        })
        
        # Add plays
        repo.insertPlay(username, "t1", 1774000000, 30000, "listener") # 2026-03-04
        repo.insertPlay(username, "t1", 1775000000, 30000, "listener") # later

        max_play = repo.getMaxPlayedAtInPeriod(username, 1767225600, 1798761600) # Year 2026 range
        self.assertEqual(max_play, 1775000000)

        # Test save and fetch from cache
        dummy_data = {
            "calculated_at": time.time(),
            "max_played_at": 1775000000,
            "total_plays": 2,
            "total_ms": 60000,
            "longest_streak": 1,
            "peak_day": "2026-03-04",
            "peak_plays": 1,
            "unique_songs": 1,
            "unique_artists": 1,
            "discovered_songs": 1,
            "discovered_artists": 1,
            "time_series_day": "[]",
            "time_series_week": "[]",
            "time_series_month": "[]",
            "top_songs": "[]",
            "top_artists": "[]",
            "top_albums": "[]",
            "discovered_songs_list": "[]",
            "discovered_artists_list": "[]",
            "discovered_albums_list": "[]",
        }

        repo.saveCachedWrapped(username, year, dummy_data)
        
        cached_max = repo.getCachedWrappedMaxPlayedAt(username, year)
        self.assertEqual(cached_max, 1775000000)

        cached_data = repo.getCachedWrapped(username, year)
        self.assertIsNotNone(cached_data)
        self.assertEqual(cached_data["total_plays"], 2)
        self.assertEqual(cached_data["peak_day"], "2026-03-04")

        # Test delete
        repo.deleteUserWrapped(username, year)
        self.assertIsNone(repo.getCachedWrapped(username, year))


class TestWrappedBackgroundWorker(DatabaseTestCase):
    def test_worker_triggers_recalculation(self):
        db = self._makeDb({}, [])
        # Insert a play in 2026
        db.repo.upsertTrack({
            "id": "t1", "name": "Song 1", "url": "u1", "imageId": "img1", "duration": 30000,
            "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album 1", "url": "u", "imageId": "i", "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
            "artists": [{"id": "art1", "name": "Artist 1", "url": "u", "imageId": "i"}]
        })
        db.repo.insertPlay(db.user, "t1", 1774000000, 30000, "listener")

        # Clear existing cache if any
        db.repo.deleteUserWrapped(db.user, 2026)

        # Run checkAndRecalculate
        db._checkAndRecalculateWrapped()

        # Check if cache is now populated
        cached = db.repo.getCachedWrapped(db.user, 2026)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["total_plays"], 1)
        self.assertEqual(cached["max_played_at"], 1774000000)


class TestWrappedRouteAjax(unittest.TestCase):
    def setUp(self):
        self.tzPatcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        self.tzPatcher.start()
        self.addCleanup(self.tzPatcher.stop)

        self.nowPatcher = patch.object(appModule, "now",
                                       return_value=datetime.datetime(2026, 7, 11, tzinfo=datetime.timezone.utc))
        self.nowPatcher.start()
        self.addCleanup(self.nowPatcher.stop)

    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def test_ajax_returns_json_fragments(self):
        dash = self._makeApp()
        
        # Setup mock db
        db = MagicMock()
        db.tz = datetime.timezone.utc
        db.user = "alice"
        db.getEntriesFromOld.return_value = [{"playedAt": 1774000000}]
        
        # Return dummy cached data
        dummy_cached = {
            "total_plays": 12,
            "total_ms": 360000,
            "longest_streak": 3,
            "peak_day": "2026-03-04",
            "peak_plays": 4,
            "unique_songs": 5,
            "unique_artists": 2,
            "discovered_songs": 2,
            "discovered_artists": 1,
            "time_series_day": "[]",
            "time_series_week": "[]",
            "time_series_month": "[]",
            "top_songs": "[]",
            "top_artists": "[]",
            "top_albums": "[]",
            "discovered_songs_list": "[]",
            "discovered_artists_list": "[]",
            "discovered_albums_list": "[]",
        }
        db.repo.getCachedWrapped.return_value = dummy_cached

        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            
            resp = client.get("/wrapped?year=2026&ajax=true")
            self.assertEqual(resp.status_code, 200)
            data = json.loads(resp.data.decode())
            
            self.assertEqual(data["totalPlays"], 12)
            self.assertEqual(data["longestStreak"], 3)
            self.assertEqual(data["peakDay"], "2026-03-04")
            self.assertIn("topSongsHtml", data)
            self.assertIn("topArtistsHtml", data)
