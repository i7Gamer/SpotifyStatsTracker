import unittest
import sys
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import Repository
from Database.Listeners.spotifyListener import (
    Listener,
    _refresh_spotify_access_token,
    _fetch_recently_played_from_web_api,
)

def makeTrack(trackId="track1"):
    return {
        "id": trackId,
        "name": "Track Name",
        "url": f"https://open.spotify.com/track/{trackId}",
        "duration": 180000,
        "explicit": False,
        "isrc": "US1234567890",
        "discNumber": 1,
        "trackNumber": 1,
        "album": {
            "id": "album1",
            "name": "Album Name",
            "url": "https://open.spotify.com/album/album1",
            "totalTracks": 10,
            "releaseDate": "2026-01-01",
            "imageUrl": "https://img.com/a.jpg",
        },
        "artists": [
            {
                "id": "artist1",
                "name": "Artist Name",
                "url": "https://open.spotify.com/artist/artist1",
                "imageUrl": "https://img.com/art.jpg",
                "imageId": "artist1",
            }
        ],
        "imageUrl": "https://img.com/a.jpg",
        "imageId": "album1",
    }


class ApiBackfillTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test.db"
        self.repo = Repository(self.db_path)
        # Ensure correct schema
        self.repo.addTrackMetadataColumnsIfMissing()
        self.repo.addSpotifyApiColumnsToUsersIfMissing()
        self.repo.commit()

    def tearDown(self):
        self.repo.connectionManager.close()
        self._tmpdir.cleanup()

    def test_insert_play_upsert(self):
        # Create user and track first
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertTrack(makeTrack("track1"))
        self.repo.commit()

        # Insert a play
        self.repo.insertPlay("alice", "track1", 1000.0, 5000, "playlist1")
        self.repo.commit()

        # Check existing play
        conn = self.repo._conn()
        row = conn.execute("SELECT time_played, played_from FROM plays WHERE username='alice' AND track_id='track1'").fetchone()
        self.assertEqual(row["time_played"], 5000)
        self.assertEqual(row["played_from"], "playlist1")

        # Try to insert identical play -> should return False and not update since duration is the same
        inserted = self.repo.insertPlay("alice", "track1", 1000.0, 5000, "playlist2")
        self.repo.commit()
        self.assertFalse(inserted)
        row = conn.execute("SELECT time_played, played_from FROM plays WHERE username='alice' AND track_id='track1'").fetchone()
        self.assertEqual(row["time_played"], 5000)
        self.assertEqual(row["played_from"], "playlist1")  # played_from was coalesced so it stayed same

        # Try to insert duplicate with different time_played -> should return False but UPDATE time_played
        inserted = self.repo.insertPlay("alice", "track1", 1000.0, 8000, "playlist2")
        self.repo.commit()
        self.assertFalse(inserted)
        row = conn.execute("SELECT time_played, played_from FROM plays WHERE username='alice' AND track_id='track1'").fetchone()
        self.assertEqual(row["time_played"], 8000)
        self.assertEqual(row["played_from"], "playlist2") # COALESCE(?, played_from) -> "playlist2" was set!

    @patch("requests.post")
    def test_refresh_spotify_access_token(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "token123"}
        mock_post.return_value = mock_response

        token = _refresh_spotify_access_token("client_id", "client_secret", "refresh_token")
        self.assertEqual(token, "token123")
        mock_post.assert_called_once()

    @patch("requests.get")
    def test_fetch_recently_played_from_web_api(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "items": [
                {"track": {"id": "track1", "duration_ms": 200000}, "played_at": "2026-07-13T10:00:00Z"}
            ]
        }
        mock_get.return_value = mock_response

        items = _fetch_recently_played_from_web_api("token123")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["track"]["id"], "track1")

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api")
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token")
    def test_check_web_api_backfill(self, mock_refresh, mock_fetch):
        mock_refresh.return_value = "token123"
        mock_fetch.return_value = [
            {"track": {"id": "track_new", "duration_ms": 180000}, "played_at": "2026-07-13T10:05:00Z"},
            {"track": {"id": "track_recorded", "duration_ms": 240000}, "played_at": "2026-07-13T10:00:00Z"}
        ]

        # Set up a listener with a credentials callback
        get_credentials = MagicMock(return_value={
            "client_id": "cid",
            "client_secret": "cs",
            "refresh_token": "rt"
        })

        # Mock spotapi call inside Listener init
        with patch("Database.Listeners.spotifyListener.Spotify") as mock_spotify_cls:
            mock_sp = MagicMock()
            mock_sp.current_user_recently_played.return_value = [
                {"track": {"id": "track_recorded"}, "played_at": "2026-07-13T10:00:00Z", "ms_played": 240000}
            ]
            mock_spotify_cls.return_value = mock_sp

            listener = Listener("dummy_cookie", email="alice@example.com", get_credentials=get_credentials)
            
        callback = MagicMock()
        
        # Override self._lastWebApiPollTime to trigger check immediately
        listener._lastWebApiPollTime = 0
        
        listener._checkWebApiBackfill(callback)
        
        # Should have detected and backfilled the play for "track_new"
        callback.assert_called_once()
        backfilled = callback.call_args[0][0]
        self.assertEqual(len(backfilled), 1)
        self.assertEqual(backfilled[0]["track"]["id"], "track_new")
        # Web API played_at is the END time (10:05:00Z), but we store the START time
        # Track duration is 180000 ms = 180 seconds = 3 minutes
        # So START time = 10:05:00 - 3min = 10:02:00
        self.assertEqual(backfilled[0]["played_at"], "2026-07-13T10:02:00Z")
        self.assertEqual(backfilled[0]["ms_played"], 180000)

        # recentlyPlayed_Z1 is replaced with this batch (not appended onto the
        # prior listener-sourced entry) so it holds exactly the last batch
        # checked, in the API's own order (newest first) with each entry's
        # own correct END time preserved.
        self.assertEqual(len(listener.recentlyPlayed_Z1), 2)
        self.assertEqual(listener.recentlyPlayed_Z1[0]["track"]["id"], "track_new")
        self.assertEqual(listener.recentlyPlayed_Z1[0]["played_at"], "2026-07-13T10:05:00Z")
        self.assertEqual(listener.recentlyPlayed_Z1[1]["track"]["id"], "track_recorded")
        self.assertEqual(listener.recentlyPlayed_Z1[1]["played_at"], "2026-07-13T10:00:00Z")

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api")
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token")
    def test_check_web_api_backfill_duplicate_track_gets_own_end_time(self, mock_refresh, mock_fetch):
        """Same track played twice at different times must each be cached with
        its OWN end time, not both collapsed onto whichever occurrence a
        track-ID-only lookup finds first."""
        mock_refresh.return_value = "token123"
        mock_fetch.return_value = [
            {"track": {"id": "track_dup", "duration_ms": 180000}, "played_at": "2026-07-13T10:10:00Z"},
            {"track": {"id": "track_dup", "duration_ms": 180000}, "played_at": "2026-07-13T10:05:00Z"},
        ]

        get_credentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "cs", "refresh_token": "rt",
        })

        with patch("Database.Listeners.spotifyListener.Spotify") as mock_spotify_cls:
            mock_sp = MagicMock()
            mock_sp.current_user_recently_played.return_value = []
            mock_spotify_cls.return_value = mock_sp
            listener = Listener("dummy_cookie", email="alice@example.com", get_credentials=get_credentials)

        callback = MagicMock()
        listener._lastWebApiPollTime = 0
        listener._checkWebApiBackfill(callback)

        backfilled = callback.call_args[0][0]
        self.assertEqual(len(backfilled), 2)

        # recentlyPlayed_Z1 must retain each occurrence's own end time, not
        # duplicate the same timestamp for both.
        end_times = {item["played_at"] for item in listener.recentlyPlayed_Z1}
        self.assertEqual(end_times, {"2026-07-13T10:10:00Z", "2026-07-13T10:05:00Z"})

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api")
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token")
    def test_check_web_api_backfill_skips_items_missing_track_id_or_played_at(self, mock_refresh, mock_fetch):
        """Items missing a track ID or played_at must be skipped from both
        missed-item detection and the recentlyPlayed_Z1 cache, not cached
        with a None/missing value that would corrupt later comparisons."""
        mock_refresh.return_value = "token123"
        mock_fetch.return_value = [
            {"track": {"id": "track_ok", "duration_ms": 180000}, "played_at": "2026-07-13T10:05:00Z"},
            {"track": {"id": None, "duration_ms": 180000}, "played_at": "2026-07-13T10:04:00Z"},
            {"track": {}, "played_at": "2026-07-13T10:03:00Z"},
            {"track": {"id": "track_no_time", "duration_ms": 180000}, "played_at": None},
            {"track": {"id": "track_no_time2", "duration_ms": 180000}},
        ]

        get_credentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "cs", "refresh_token": "rt",
        })

        with patch("Database.Listeners.spotifyListener.Spotify") as mock_spotify_cls:
            mock_sp = MagicMock()
            mock_sp.current_user_recently_played.return_value = []
            mock_spotify_cls.return_value = mock_sp
            listener = Listener("dummy_cookie", email="alice@example.com", get_credentials=get_credentials)

        callback = MagicMock()
        listener._lastWebApiPollTime = 0
        listener._checkWebApiBackfill(callback)

        backfilled = callback.call_args[0][0]
        self.assertEqual(len(backfilled), 1)
        self.assertEqual(backfilled[0]["track"]["id"], "track_ok")

        self.assertEqual(len(listener.recentlyPlayed_Z1), 1)
        self.assertEqual(listener.recentlyPlayed_Z1[0]["track"]["id"], "track_ok")

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api")
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token")
    def test_check_web_api_backfill_invokes_snapshot_callback_with_full_items(self, mock_refresh, mock_fetch):
        """onWebApiSnapshot must receive every fetched item (not just the ones
        missing locally) - Database._reconcileWithWebApiHistory needs the full
        window to know what the API does and doesn't corroborate."""
        mock_refresh.return_value = "token123"
        apiItems = [
            {"track": {"id": "track_new", "duration_ms": 180000}, "played_at": "2026-07-13T10:05:00Z"},
            {"track": {"id": "track_recorded"}, "played_at": "2026-07-13T10:00:00Z"},
        ]
        mock_fetch.return_value = apiItems

        get_credentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "cs", "refresh_token": "rt",
        })

        with patch("Database.Listeners.spotifyListener.Spotify") as mock_spotify_cls:
            mock_sp = MagicMock()
            mock_sp.current_user_recently_played.return_value = [
                {"track": {"id": "track_recorded"}, "played_at": "2026-07-13T10:00:00Z", "ms_played": 240000}
            ]
            mock_spotify_cls.return_value = mock_sp
            listener = Listener("dummy_cookie", email="alice@example.com", get_credentials=get_credentials)

        listener._lastWebApiPollTime = 0
        onWebApiSnapshot = MagicMock()

        listener._checkWebApiBackfill(MagicMock(), onWebApiSnapshot=onWebApiSnapshot)

        onWebApiSnapshot.assert_called_once_with(apiItems)

    def test_check_web_api_backfill_without_snapshot_callback_does_not_raise(self):
        """onWebApiSnapshot is optional - existing callers that don't pass it
        (e.g. tests, or a Database without reconciliation wired up) must be
        unaffected."""
        listener = Listener.__new__(Listener)
        listener.get_credentials = None

        listener._checkWebApiBackfill(MagicMock())  # must not raise
