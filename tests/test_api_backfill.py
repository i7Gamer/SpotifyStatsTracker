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
    WEB_API_POLL_INTERVAL_SECONDS,
    _refresh_spotify_access_token,
    _fetch_recently_played_from_web_api,
)

# Comfortably past WEB_API_POLL_INTERVAL_SECONDS so _checkWebApiBackfill's
# poll-interval guard is deterministically bypassed, regardless of how large
# time.monotonic() already is on the host running the test (e.g. a freshly
# booted CI runner has a much smaller monotonic clock than a long-uptime dev
# machine, which previously let a `_lastWebApiPollTime = 0` reset silently
# fail to force an immediate check).
_MONOTONIC_NOW = WEB_API_POLL_INTERVAL_SECONDS * 10

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
        # Mock _get_current_user_from_web_api to avoid real network connections
        self._get_current_user_patcher = patch(
            "Database.Listeners.spotifyListener._get_current_user_from_web_api",
            return_value={"id": "alice", "display_name": "Alice", "email": "alice@example.com"}
        )
        self.mock_get_current_user = self._get_current_user_patcher.start()

    def tearDown(self):
        self._get_current_user_patcher.stop()
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

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            listener._checkWebApiBackfill(callback)

        # Should have detected and backfilled the play for "track_new"
        callback.assert_called_once()
        backfilled = callback.call_args[0][0]
        self.assertEqual(len(backfilled), 1)
        self.assertEqual(backfilled[0]["track"]["id"], "track_new")
        # played_at is stored exactly as the Web API returned it, with no
        # duration subtraction - Spotify's played_at semantics are documented
        # as inconsistent about start vs end time (spotify/web-api#1083), so
        # the code no longer bets on one interpretation for storage.
        self.assertEqual(backfilled[0]["played_at"], "2026-07-13T10:05:00Z")
        self.assertEqual(backfilled[0]["ms_played"], 180000)

        # recentlyPlayed_Z1 (the live listener's own cache) is untouched by
        # _checkWebApiBackfill - it stays exactly as the listener set it.
        self.assertEqual(len(listener.recentlyPlayed_Z1), 1)
        self.assertEqual(listener.recentlyPlayed_Z1[0]["track"]["id"], "track_recorded")

        # webApiRecentlyPlayed_Z1 (this function's OWN cache) is replaced with
        # this batch, in the API's own order (newest first), each entry
        # keeping its own played_at unchanged.
        self.assertEqual(len(listener.webApiRecentlyPlayed_Z1), 2)
        self.assertEqual(listener.webApiRecentlyPlayed_Z1[0]["track"]["id"], "track_new")
        self.assertEqual(listener.webApiRecentlyPlayed_Z1[0]["played_at"], "2026-07-13T10:05:00Z")
        self.assertEqual(listener.webApiRecentlyPlayed_Z1[1]["track"]["id"], "track_recorded")
        self.assertEqual(listener.webApiRecentlyPlayed_Z1[1]["played_at"], "2026-07-13T10:00:00Z")

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api")
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token")
    def test_check_web_api_backfill_duplicate_track_gets_own_timestamp(self, mock_refresh, mock_fetch):
        """Same track played twice at different times must each be cached with
        its OWN played_at, not both collapsed onto whichever occurrence a
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
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            listener._checkWebApiBackfill(callback)

        backfilled = callback.call_args[0][0]
        self.assertEqual(len(backfilled), 2)

        # webApiRecentlyPlayed_Z1 must retain each occurrence's own played_at,
        # not duplicate the same timestamp for both.
        played_ats = {item["played_at"] for item in listener.webApiRecentlyPlayed_Z1}
        self.assertEqual(played_ats, {"2026-07-13T10:10:00Z", "2026-07-13T10:05:00Z"})

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api")
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token")
    def test_check_web_api_backfill_skips_items_missing_track_id_or_played_at(self, mock_refresh, mock_fetch):
        """Items missing a track ID or played_at must be skipped from both
        missed-item detection and the webApiRecentlyPlayed_Z1 cache, not cached
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
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            listener._checkWebApiBackfill(callback)

        backfilled = callback.call_args[0][0]
        self.assertEqual(len(backfilled), 1)
        self.assertEqual(backfilled[0]["track"]["id"], "track_ok")

        self.assertEqual(len(listener.webApiRecentlyPlayed_Z1), 1)
        self.assertEqual(listener.webApiRecentlyPlayed_Z1[0]["track"]["id"], "track_ok")

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api")
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token")
    def test_check_web_api_backfill_does_not_resurface_play_reported_as_start_time(self, mock_refresh, mock_fetch):
        """Regression test for the root-cause bug: the live listener already
        recorded this exact play (true start time). The Web API reports the
        SAME track with played_at equal to that same true start time (i.e.
        Spotify reported it as a start time this time). Must NOT be
        backfilled - the old code compared this against a duration-shifted
        value and would have missed the match, causing a duplicate insert."""
        mock_refresh.return_value = "token123"
        mock_fetch.return_value = [
            {"track": {"id": "track_x", "duration_ms": 180000}, "played_at": "2026-07-13T10:00:00Z"},
        ]

        get_credentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "cs", "refresh_token": "rt",
        })

        with patch("Database.Listeners.spotifyListener.Spotify") as mock_spotify_cls:
            mock_sp = MagicMock()
            mock_sp.current_user_recently_played.return_value = [
                {"track": {"id": "track_x"}, "played_at": "2026-07-13T10:00:00Z", "ms_played": 180000}
            ]
            mock_spotify_cls.return_value = mock_sp
            listener = Listener("dummy_cookie", email="alice@example.com", get_credentials=get_credentials)

        callback = MagicMock()
        listener._lastWebApiPollTime = 0
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            listener._checkWebApiBackfill(callback)

        callback.assert_not_called()

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api")
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token")
    def test_check_web_api_backfill_does_not_resurface_play_reported_as_end_time(self, mock_refresh, mock_fetch):
        """Same as above, but this time the Web API reports played_at as an
        END time (true_start + duration) for the SAME already-recorded play -
        Spotify is documented as inconsistent about which it reports
        (spotify/web-api#1083), so is_recorded must check both
        interpretations, not just a direct match."""
        mock_refresh.return_value = "token123"
        true_start = "2026-07-13T10:00:00Z"
        end_time = "2026-07-13T10:03:00Z"  # true_start + 180s duration
        mock_fetch.return_value = [
            {"track": {"id": "track_x", "duration_ms": 180000}, "played_at": end_time},
        ]

        get_credentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "cs", "refresh_token": "rt",
        })

        with patch("Database.Listeners.spotifyListener.Spotify") as mock_spotify_cls:
            mock_sp = MagicMock()
            mock_sp.current_user_recently_played.return_value = [
                {"track": {"id": "track_x"}, "played_at": true_start, "ms_played": 180000}
            ]
            mock_spotify_cls.return_value = mock_sp
            listener = Listener("dummy_cookie", email="alice@example.com", get_credentials=get_credentials)

        callback = MagicMock()
        listener._lastWebApiPollTime = 0
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            listener._checkWebApiBackfill(callback)

        callback.assert_not_called()

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api")
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token")
    def test_check_web_api_backfill_honors_its_own_previous_batch(self, mock_refresh, mock_fetch):
        """An item already surfaced by a PREVIOUS _checkWebApiBackfill poll
        (cached in webApiRecentlyPlayed_Z1) must not be re-treated as missed
        on a later poll, even if the live listener's own cache never saw it."""
        mock_refresh.return_value = "token123"
        mock_fetch.return_value = [
            {"track": {"id": "track_y", "duration_ms": 180000}, "played_at": "2026-07-13T10:00:00Z"},
        ]

        get_credentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "cs", "refresh_token": "rt",
        })

        with patch("Database.Listeners.spotifyListener.Spotify") as mock_spotify_cls:
            mock_sp = MagicMock()
            mock_sp.current_user_recently_played.return_value = []
            mock_spotify_cls.return_value = mock_sp
            listener = Listener("dummy_cookie", email="alice@example.com", get_credentials=get_credentials)

        listener.webApiRecentlyPlayed_Z1 = [
            {"track": {"id": "track_y"}, "played_at": "2026-07-13T10:00:00Z", "ms_played": 180000, "context": {}}
        ]

        callback = MagicMock()
        listener._lastWebApiPollTime = 0
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            listener._checkWebApiBackfill(callback)

        callback.assert_not_called()

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

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            listener._checkWebApiBackfill(MagicMock(), onWebApiSnapshot=onWebApiSnapshot)

        onWebApiSnapshot.assert_called_once_with(apiItems)

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api")
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token")
    def test_check_web_api_backfill_runs_on_first_poll_even_with_a_low_monotonic_clock(self, mock_refresh, mock_fetch):
        """Regression test: _lastWebApiPollTime must start as None ("never
        polled"), not 0, so the very first poll always runs - even on a host
        where time.monotonic() itself is still small (e.g. shortly after
        boot), which previously made a freshly constructed Listener look like
        it had already polled "recently" and silently skip its first check."""
        mock_refresh.return_value = "token123"
        mock_fetch.return_value = [
            {"track": {"id": "track_new", "duration_ms": 180000}, "played_at": "2026-07-13T10:05:00Z"},
        ]

        get_credentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "cs", "refresh_token": "rt",
        })

        lowUptimeMonotonic = 5.0  # smaller than WEB_API_POLL_INTERVAL_SECONDS

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=lowUptimeMonotonic):
            with patch("Database.Listeners.spotifyListener.Spotify") as mock_spotify_cls:
                mock_sp = MagicMock()
                mock_sp.current_user_recently_played.return_value = []
                mock_spotify_cls.return_value = mock_sp
                listener = Listener("dummy_cookie", email="alice@example.com", get_credentials=get_credentials)

            callback = MagicMock()
            listener._checkWebApiBackfill(callback)  # no manual _lastWebApiPollTime reset

        callback.assert_called_once()

    def test_check_web_api_backfill_without_snapshot_callback_does_not_raise(self):
        """onWebApiSnapshot is optional - existing callers that don't pass it
        (e.g. tests, or a Database without reconciliation wired up) must be
        unaffected."""
        listener = Listener.__new__(Listener)
        listener.get_credentials = None

        listener._checkWebApiBackfill(MagicMock())  # must not raise

    def test_missing_get_backfill_enabled_defaults_to_allowed(self):
        """A Listener built without get_backfill_enabled (every caller before
        this admin kill switch existed, and any test that constructs one
        directly) must behave exactly as before - always allowed."""
        listener = Listener.__new__(Listener)
        listener.get_credentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "cs", "refresh_token": "rt"})
        listener.get_backfill_enabled = None
        listener._lastWebApiPollTime = 0

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW), \
             patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value=None):
            listener._checkWebApiBackfill(MagicMock())  # proceeds past the enabled check, fails later on no token

        listener.get_credentials.assert_called_once()

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api")
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token")
    def test_disabled_kill_switch_skips_the_backfill_check_entirely(self, mock_refresh, mock_fetch):
        get_credentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "cs", "refresh_token": "rt"})
        get_backfill_enabled = MagicMock(return_value=False)

        with patch("Database.Listeners.spotifyListener.Spotify") as mock_spotify_cls:
            mock_sp = MagicMock()
            mock_sp.current_user_recently_played.return_value = []
            mock_spotify_cls.return_value = mock_sp
            listener = Listener("dummy_cookie", email="alice@example.com",
                                get_credentials=get_credentials,
                                get_backfill_enabled=get_backfill_enabled)
        listener._lastWebApiPollTime = 0

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            listener._checkWebApiBackfill(MagicMock())

        get_backfill_enabled.assert_called_once()
        mock_refresh.assert_not_called()
        mock_fetch.assert_not_called()
        # Not polled: the poll-interval guard never got a chance to record a timestamp.
        self.assertEqual(listener._lastWebApiPollTime, 0)

    def _makeQuietBackfillListener(self):
        """Listener whose _checkWebApiBackfill runs the happy path with an empty
        Web API result - so the only possible INFO logs are the routine
        'Running ... backfill check' / 'Web API returned ...' progress lines."""
        get_credentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "cs", "refresh_token": "rt",
        })
        with patch("Database.Listeners.spotifyListener.Spotify") as mock_spotify_cls:
            mock_sp = MagicMock()
            mock_sp.current_user_recently_played.return_value = []
            mock_spotify_cls.return_value = mock_sp
            listener = Listener("dummy_cookie", email="alice@example.com", get_credentials=get_credentials)
        listener._lastWebApiPollTime = 0
        return listener

    def _runQuietBackfill(self, listener):
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            listener._checkWebApiBackfill(MagicMock())

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api", return_value=[])
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="token123")
    def test_backfill_progress_logs_hidden_without_flask_debug(self, mock_refresh, mock_fetch):
        """The routine backfill progress INFO lines must stay silent when
        FLASK_DEBUG is unset."""
        listener = self._makeQuietBackfillListener()

        envWithoutDebug = {k: v for k, v in os.environ.items() if k != "FLASK_DEBUG"}
        with patch.dict(os.environ, envWithoutDebug, clear=True):
            with self.assertNoLogs("Database.Listeners.spotifyListener", level="INFO"):
                self._runQuietBackfill(listener)

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api", return_value=[])
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="token123")
    def test_backfill_progress_logs_hidden_with_falsy_flask_debug(self, mock_refresh, mock_fetch):
        """FLASK_DEBUG=0 must count as disabled, not merely 'set'."""
        listener = self._makeQuietBackfillListener()

        with patch.dict(os.environ, {"FLASK_DEBUG": "0"}):
            with self.assertNoLogs("Database.Listeners.spotifyListener", level="INFO"):
                self._runQuietBackfill(listener)

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api", return_value=[])
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="token123")
    def test_backfill_progress_logs_shown_with_flask_debug(self, mock_refresh, mock_fetch):
        """With FLASK_DEBUG=1 both progress lines must be logged."""
        listener = self._makeQuietBackfillListener()

        with patch.dict(os.environ, {"FLASK_DEBUG": "1"}):
            with self.assertLogs("Database.Listeners.spotifyListener", level="INFO") as cm:
                self._runQuietBackfill(listener)

        self.assertTrue(any("Running Spotify Web API recently-played backfill check" in m for m in cm.output))
        self.assertTrue(any("Web API returned 0 items for backfill check" in m for m in cm.output))
