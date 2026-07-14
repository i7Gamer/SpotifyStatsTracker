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

        missing = db.repo.getAlbumsMissingMetadata(10)
        self.assertIn("alb1", missing)
        self.assertIn("alb3", missing)
        self.assertNotIn("alb2", missing)

    def test_repository_update_album_metadata(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb1', 'Album 1', '', 0.0, 0)")

        db.repo.updateAlbumMetadata("alb1", 1600000000.0, 12)
        row = conn.execute("SELECT release_date, total_tracks FROM albums WHERE id='alb1'").fetchone()
        self.assertEqual(row["release_date"], 1600000000.0)
        self.assertEqual(row["total_tracks"], 12)

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
                    "release_date": "2020-05-05",
                    "total_tracks": 10
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

        # Verify alb1 was updated
        row = conn.execute("SELECT release_date, total_tracks FROM albums WHERE id='alb1'").fetchone()
        self.assertGreater(row["release_date"], 0)
        self.assertEqual(row["total_tracks"], 10)

        # Verify alb2 was skipped because it was in _active_backfills
        row2 = conn.execute("SELECT release_date, total_tracks FROM albums WHERE id='alb2'").fetchone()
        self.assertEqual(row2["release_date"], 0.0)

        # Verify alb2 is still in _active_backfills, but alb1 has been removed after processing
        self.assertIn("alb2", Database._active_backfills)
        self.assertNotIn("alb1", Database._active_backfills)

        # Clean up
        Database._active_backfills.clear()


if __name__ == "__main__":
    unittest.main()
