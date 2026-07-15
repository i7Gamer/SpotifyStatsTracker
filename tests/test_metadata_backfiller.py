import sys
import os
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
from Database.database import Database


class TestMetadataBackfiller(DatabaseTestCase):
    def test_repository_get_albums_missing_metadata(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url, release_date) VALUES ('alb1', 'Album 1', '', 0.0)")
            conn.execute("INSERT INTO albums (id, name, url, release_date) VALUES ('alb2', 'Album 2', '', 1700000000.0)")
            conn.execute("INSERT INTO albums (id, name, url, release_date) VALUES ('alb3', 'Album 3', '', NULL)")
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb4', 'Album 4', '', 0.0, 5)")

        missing = db.repo.getAlbumsMissingMetadata(10)
        self.assertIn("alb1", missing)
        self.assertIn("alb3", missing)
        self.assertNotIn("alb2", missing)
        self.assertNotIn("alb4", missing)

    def test_repository_update_album_metadata(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb1', 'Album 1', '', 0.0, 0)")

        db.repo.updateAlbumMetadata("alb1", 1600000000.0, 12, "Updated Album 1")
        row = conn.execute("SELECT name, release_date, total_tracks FROM albums WHERE id='alb1'").fetchone()
        self.assertEqual(row["name"], "Updated Album 1")
        self.assertEqual(row["release_date"], 1600000000.0)
        self.assertEqual(row["total_tracks"], 12)

    def test_repository_update_track_name(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb1', 'Album 1', '')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr1', 'Track 1', '', 'alb1')")

        db.repo.updateTrackName("tr1", "Updated Track 1")
        row = conn.execute("SELECT name FROM tracks WHERE id='tr1'").fetchone()
        self.assertEqual(row["name"], "Updated Track 1")

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("requests.get")
    def test_backfiller_loop_fetches_and_deduplicates(self, mock_get, mock_refresh):
        # 1. Setup mock HTTP response for Spotify v1/albums
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "albums": [
                {
                    "id": "alb1",
                    "name": "Updated Album Name",
                    "release_date": "2020-05-05",
                    "total_tracks": 10,
                    "tracks": {
                        "items": [
                            {
                                "id": "tr1",
                                "name": "Updated Track Name"
                            }
                        ]
                    }
                }
            ]
        }
        mock_get.return_value = mock_response

        # Disable the default automatic backfiller from launching automatically in init by overriding startMetadataBackfiller
        with patch.object(Database, "startMetadataBackfiller"):
            db = self._makeDb({}, [])
            
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb1', 'Album 1', '', 0.0, 0)")
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb2', 'Album 2', '', 0.0, 0)")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr1', 'Track 1', '', 'alb1')")

        # Mock getUserSpotifyCredentials to return active credentials
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id",
            "client_secret": "test_secret",
            "refresh_token": "test_refresh"
        })

        # Add alb2 to _active_backfills to simulate another thread already working on it
        Database._active_backfills.clear()
        Database._active_backfills.add("alb2")

        # Mock backfiller_stop_event to break the loop after one run
        db.backfiller_stop_event = MagicMock()
        # Side effect: first call returns False (do run), second call returns True (exit loop)
        db.backfiller_stop_event.is_set.side_effect = [False, True]
        db.backfiller_stop_event.wait.return_value = False

        # Run one iteration of the backfiller
        db._metadataBackfillLoop()

        # Verify alb1 was updated (including its name and tracks)
        row = conn.execute("SELECT name, release_date, total_tracks FROM albums WHERE id='alb1'").fetchone()
        self.assertEqual(row["name"], "Updated Album Name")
        self.assertGreater(row["release_date"], 0)
        self.assertEqual(row["total_tracks"], 10)

        track_row = conn.execute("SELECT name FROM tracks WHERE id='tr1'").fetchone()
        self.assertEqual(track_row["name"], "Updated Track Name")

        # Verify alb2 was skipped because it was in _active_backfills
        row2 = conn.execute("SELECT release_date, total_tracks FROM albums WHERE id='alb2'").fetchone()
        self.assertEqual(row2["release_date"], 0.0)

        # Verify alb2 is still in _active_backfills, but alb1 has been removed after processing
        self.assertIn("alb2", Database._active_backfills)
        self.assertNotIn("alb1", Database._active_backfills)

        # Clean up
        Database._active_backfills.clear()

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value=None)
    @patch("SpotipyFree.Spotify")
    @patch("requests.get")
    def test_backfiller_loop_fallback_to_spotipy_free(self, mock_get, mock_spotipy_class, mock_refresh):
        # 1. Setup mock response for official API: return 403 Forbidden
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        # 2. Setup mock for SpotipyFree.Spotify
        mock_sp = MagicMock()
        mock_spotipy_class.return_value = mock_sp
        
        # Use a real event so we can trigger shutdown naturally
        import threading
        event = threading.Event()
        
        def mock_album_impl(album_id):
            event.set()  # Signal loop to stop after this fetch
            return {
                "id": "alb1",
                "release_date": "2021-01-01",
                "total_tracks": 8
            }
        mock_sp.album.side_effect = mock_album_impl

        # Disable the default automatic backfiller
        with patch.object(Database, "startMetadataBackfiller"):
            db = self._makeDb({}, [])
            
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb1', 'Album 1', '', 0.0, 0)")

        # Mock credentials
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id",
            "client_secret": "test_secret",
            "refresh_token": "test_refresh"
        })

        db.backfiller_stop_event = event
        event.wait = MagicMock(return_value=False)

        # Run backfiller
        db._metadataBackfillLoop()

        # Verify alb1 was updated via SpotipyFree fallback
        row = conn.execute("SELECT release_date, total_tracks FROM albums WHERE id='alb1'").fetchone()
        self.assertGreater(row["release_date"], 0)
        self.assertEqual(row["total_tracks"], 8)
        mock_sp.album.assert_called_once_with("alb1")

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("SpotipyFree.Spotify")
    @patch("requests.get")
    @patch("Database.database.logger")
    def test_backfiller_loop_403_warning_logged_only_with_debug(self, mock_logger, mock_get, mock_spotipy_class, mock_refresh):
        import threading
        # Setup mock 403 response
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        mock_sp = MagicMock()
        mock_spotipy_class.return_value = mock_sp
        mock_sp.album.return_value = {
            "id": "alb1",
            "release_date": "2021-01-01",
            "total_tracks": 8
        }

        # Disable the default automatic backfiller
        with patch.object(Database, "startMetadataBackfiller"):
            db = self._makeDb({}, [])
            
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb1', 'Album 1', '', 0.0, 0)")

        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id",
            "client_secret": "test_secret",
            "refresh_token": "test_refresh"
        })

        # Run with FLASK_DEBUG = "1"
        with patch.dict(os.environ, {"FLASK_DEBUG": "1"}):
            Database._active_backfills.clear()
            stop_mock = MagicMock()
            calls_1 = 0
            def is_set_1():
                nonlocal calls_1
                calls_1 += 1
                return calls_1 > 1
            stop_mock.is_set.side_effect = is_set_1
            stop_mock.wait.return_value = False
            db.backfiller_stop_event = stop_mock
            db._metadataBackfillLoop()
            
            # Check warning was logged
            warning_calls = [
                args[0] for args, _ in mock_logger.warning.call_args_list
                if "Spotify Web API returned status" in args[0]
            ]
            self.assertTrue(len(warning_calls) > 0)

        # Reset mock
        mock_logger.reset_mock()

        # Run with FLASK_DEBUG = "true"
        with patch.dict(os.environ, {"FLASK_DEBUG": "true"}):
            Database._active_backfills.clear()
            stop_mock = MagicMock()
            calls_2 = 0
            def is_set_2():
                nonlocal calls_2
                calls_2 += 1
                return calls_2 > 1
            stop_mock.is_set.side_effect = is_set_2
            stop_mock.wait.return_value = False
            db.backfiller_stop_event = stop_mock
            db._metadataBackfillLoop()
            
            # Check warning was logged
            warning_calls = [
                args[0] for args, _ in mock_logger.warning.call_args_list
                if "Spotify Web API returned status" in args[0]
            ]
            self.assertTrue(len(warning_calls) > 0)

        # Reset mock
        mock_logger.reset_mock()

        # Run with FLASK_DEBUG = "0"
        with patch.dict(os.environ, {"FLASK_DEBUG": "0"}):
            Database._active_backfills.clear()
            stop_mock = MagicMock()
            calls_3 = 0
            def is_set_3():
                nonlocal calls_3
                calls_3 += 1
                return calls_3 > 1
            stop_mock.is_set.side_effect = is_set_3
            stop_mock.wait.return_value = False
            db.backfiller_stop_event = stop_mock
            db._metadataBackfillLoop()
            
            # Check warning was NOT logged
            warning_calls = [
                args[0] for args, _ in mock_logger.warning.call_args_list
                if "Spotify Web API returned status" in args[0]
            ]
            self.assertEqual(len(warning_calls), 0)


if __name__ == "__main__":
    unittest.main()
