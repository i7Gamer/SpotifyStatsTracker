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
        import time
        from Database.repository import ALBUM_BACKFILL_RETRY_SECONDS

        db = self._makeDb({}, [])
        conn = db.repo._conn()
        now = time.time()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url, release_date) VALUES ('alb1', 'Album 1', '', 0.0)")
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb2', 'Album 2', '', 1700000000.0, 12)")
            conn.execute("INSERT INTO albums (id, name, url, release_date) VALUES ('alb3', 'Album 3', '', NULL)")
            # Restricted-style album: track count known, release date missing - must be queued
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb4', 'Album 4', '', 0.0, 5)")
            # Fabricated fallback album ids never existed on Spotify - never queued
            conn.execute("INSERT INTO albums (id, name, url, release_date) VALUES ('album_deadbeef', 'Synthetic', '', 0.0)")
            # Recently attempted - rate-limited out of the queue
            conn.execute("INSERT INTO albums (id, name, url, release_date, backfill_attempted_at) VALUES ('alb5', 'Album 5', '', 0.0, ?)", (now,))
            # Attempted longer than the retry interval ago - queued again
            conn.execute("INSERT INTO albums (id, name, url, release_date, backfill_attempted_at) VALUES ('alb6', 'Album 6', '', 0.0, ?)",
                         (now - ALBUM_BACKFILL_RETRY_SECONDS - 60,))

        missing = db.repo.getAlbumsMissingMetadata(10)
        self.assertIn("alb1", missing)
        self.assertIn("alb3", missing)
        self.assertIn("alb4", missing)
        self.assertIn("alb6", missing)
        self.assertNotIn("alb2", missing)
        self.assertNotIn("album_deadbeef", missing)
        self.assertNotIn("alb5", missing)

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
        row = conn.execute("SELECT name, duration_ms FROM tracks WHERE id='tr1'").fetchone()
        self.assertEqual(row["name"], "Updated Track 1")
        self.assertEqual(row["duration_ms"], 0)

        db.repo.updateTrackName("tr1", "Updated Track 1", duration_ms=215000)
        row = conn.execute("SELECT duration_ms FROM tracks WHERE id='tr1'").fetchone()
        self.assertEqual(row["duration_ms"], 215000)

    def test_repository_get_albums_with_artistless_tracks(self):
        import time
        from Database.repository import ALBUM_BACKFILL_RETRY_SECONDS

        db = self._makeDb({}, [])
        conn = db.repo._conn()
        now = time.time()
        with conn:
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('art1', 'Artist 1', '')")
            # alb1: has a track without artist links -> queued
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb1', 'Album 1', '', 1700000000.0, 10)")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr1', 'Track 1', '', 'alb1')")
            # alb2: all tracks have artists -> not queued
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb2', 'Album 2', '', 1700000000.0, 10)")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr2', 'Track 2', '', 'alb2')")
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES ('tr2', 'art1', 0)")
            # Fabricated fallback album ids never existed on Spotify -> never queued
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('album_deadbeef', 'Synthetic', '')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr3', 'Track 3', '', 'album_deadbeef')")
            # alb4: artist-less track but recently attempted -> rate-limited out
            conn.execute("INSERT INTO albums (id, name, url, backfill_attempted_at) VALUES ('alb4', 'Album 4', '', ?)", (now,))
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr4', 'Track 4', '', 'alb4')")
            # alb5: artist-less track, attempted beyond the retry interval -> queued again
            conn.execute("INSERT INTO albums (id, name, url, backfill_attempted_at) VALUES ('alb5', 'Album 5', '', ?)",
                         (now - ALBUM_BACKFILL_RETRY_SECONDS - 60,))
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr5', 'Track 5', '', 'alb5')")

        queued = db.repo.getAlbumsWithArtistlessTracks(10)
        self.assertIn("alb1", queued)
        self.assertIn("alb5", queued)
        self.assertNotIn("alb2", queued)
        self.assertNotIn("album_deadbeef", queued)
        self.assertNotIn("alb4", queued)

    def test_repository_add_missing_track_artists(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb1', 'Album 1', '')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr1', 'Track 1', '', 'alb1')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr2', 'Track 2', '', 'alb1')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('artOld', 'Existing Artist', '')")
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES ('tr2', 'artOld', 0)")

        newArtists = [
            {"id": "artA", "name": "Artist A", "url": "http://a", "imageId": "artA"},
            {"id": "artB", "name": "Artist B", "url": "http://b", "imageId": "artB"},
        ]
        self.assertTrue(db.repo.addMissingTrackArtists("tr1", newArtists))
        rows = conn.execute(
            "SELECT artist_id FROM track_artists WHERE track_id='tr1' ORDER BY position").fetchall()
        self.assertEqual([r["artist_id"] for r in rows], ["artA", "artB"])
        self.assertEqual(conn.execute("SELECT name FROM artists WHERE id='artA'").fetchone()["name"],
                         "Artist A")

        # A track that already has links is never touched.
        self.assertFalse(db.repo.addMissingTrackArtists("tr2", newArtists))
        rows = conn.execute(
            "SELECT artist_id FROM track_artists WHERE track_id='tr2' ORDER BY position").fetchall()
        self.assertEqual([r["artist_id"] for r in rows], ["artOld"])

        # Unknown tracks and empty artist lists are quiet no-ops.
        self.assertFalse(db.repo.addMissingTrackArtists("trMissing", newArtists))
        self.assertFalse(db.repo.addMissingTrackArtists("tr1", []))

        # Existing artist rows keep their data (no blanked-payload regressions).
        db.repo.addMissingTrackArtists(
            "tr1", [{"id": "artOld", "name": "Blanked", "url": "", "imageId": "artOld"}])
        self.assertEqual(conn.execute("SELECT name FROM artists WHERE id='artOld'").fetchone()["name"],
                         "Existing Artist")

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="mock_token")
    @patch("requests.get")
    def test_backfiller_loop_repairs_artistless_tracks(self, mock_get, mock_refresh):
        """An album with complete metadata but an artist-less track enters the
        queue through getAlbumsWithArtistlessTracks, and the album payload's
        per-track artists repair the missing links - without disturbing
        tracks whose links already exist."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "albums": [{
                "id": "alb1",
                "name": "Album 1",
                "release_date": "2020-05-05",
                "total_tracks": 2,
                "tracks": {"items": [
                    {"id": "tr1", "name": "Track 1", "duration_ms": 1000,
                     "artists": [
                         {"id": "artA", "name": "Artist A",
                          "external_urls": {"spotify": "https://open.spotify.com/artist/artA"}},
                         {"id": "artB", "name": "Artist B",
                          "external_urls": {"spotify": "https://open.spotify.com/artist/artB"}},
                         {"name": "No Id Artist"},   #< skipped: nothing real to link
                     ]},
                    {"id": "tr2", "name": "Track 2", "duration_ms": 1000,
                     "artists": [{"id": "artC", "name": "Artist C"}]},
                ]},
            }]
        }
        mock_get.return_value = mock_response

        with patch.object(Database, "startMetadataBackfiller"):
            db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            # Complete metadata: invisible to getAlbumsMissingMetadata.
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb1', 'Album 1', '', 1600000000.0, 2)")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr1', 'Track 1', '', 'alb1')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr2', 'Track 2', '', 'alb1')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('artOld', 'Existing Artist', '')")
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES ('tr2', 'artOld', 0)")

        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "test_id", "client_secret": "test_secret", "refresh_token": "test_refresh"})

        Database._active_backfills.clear()
        db.backfiller_stop_event = MagicMock()
        db.backfiller_stop_event.is_set.side_effect = [False, True]
        db.backfiller_stop_event.wait.return_value = False

        db._metadataBackfillLoop()

        rows = conn.execute(
            "SELECT artist_id FROM track_artists WHERE track_id='tr1' ORDER BY position").fetchall()
        self.assertEqual([r["artist_id"] for r in rows], ["artA", "artB"])
        self.assertEqual(conn.execute("SELECT name FROM artists WHERE id='artB'").fetchone()["name"],
                         "Artist B")
        # tr2's existing link is untouched despite the payload naming artC.
        rows = conn.execute(
            "SELECT artist_id FROM track_artists WHERE track_id='tr2' ORDER BY position").fetchall()
        self.assertEqual([r["artist_id"] for r in rows], ["artOld"])
        # The album is stamped, so it leaves the repair queue until the retry window.
        self.assertIsNotNone(conn.execute(
            "SELECT backfill_attempted_at FROM albums WHERE id='alb1'").fetchone()[0])
        Database._active_backfills.clear()

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
                                "name": "Updated Track Name",
                                "duration_ms": 215000
                            }
                        ]
                    }
                },
                {
                    # Blanked response (restricted album): names are not data and
                    # must not overwrite what the importer filled from the export
                    "id": "alb3",
                    "name": "",
                    "release_date": "2020-01-01",
                    "total_tracks": 4,
                    "tracks": {
                        "items": [
                            {
                                "id": "tr3",
                                "name": "",
                                "duration_ms": 100000
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
            conn.execute("INSERT INTO albums (id, name, url, release_date, total_tracks) VALUES ('alb3', 'Export Album Name', '', 0.0, 0)")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr1', 'Track 1', '', 'alb1')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr3', 'Export Track Name', '', 'alb3')")

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

        track_row = conn.execute("SELECT name, duration_ms FROM tracks WHERE id='tr1'").fetchone()
        self.assertEqual(track_row["name"], "Updated Track Name")
        self.assertEqual(track_row["duration_ms"], 215000)

        # Verify alb1 was stamped as attempted (rate-limits its next retry)
        self.assertIsNotNone(conn.execute("SELECT backfill_attempted_at FROM albums WHERE id='alb1'").fetchone()[0])

        # Blanked names in the response must not overwrite export-filled names,
        # but the other metadata still lands
        alb3 = conn.execute("SELECT name, release_date, total_tracks FROM albums WHERE id='alb3'").fetchone()
        self.assertEqual(alb3["name"], "Export Album Name")
        self.assertGreater(alb3["release_date"], 0)
        self.assertEqual(alb3["total_tracks"], 4)
        self.assertEqual(conn.execute("SELECT name FROM tracks WHERE id='tr3'").fetchone()["name"], "Export Track Name")

        # Verify alb2 was skipped because it was in _active_backfills
        row2 = conn.execute("SELECT release_date, total_tracks, backfill_attempted_at FROM albums WHERE id='alb2'").fetchone()
        self.assertEqual(row2["release_date"], 0.0)
        self.assertIsNone(row2["backfill_attempted_at"])

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

        # Verify alb1 was updated via SpotipyFree fallback and stamped as attempted
        row = conn.execute("SELECT release_date, total_tracks, backfill_attempted_at FROM albums WHERE id='alb1'").fetchone()
        self.assertGreater(row["release_date"], 0)
        self.assertEqual(row["total_tracks"], 8)
        self.assertIsNotNone(row["backfill_attempted_at"])
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
