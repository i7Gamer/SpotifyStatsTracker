import unittest
import sys
import os
import datetime
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import Repository
from Database.Listeners.spotifyListener import (
    Listener,
    WEB_API_POLL_INTERVAL_SECONDS,
    SCOPE_ERROR_CONFIRM_THRESHOLD,
    _refresh_spotify_access_token,
    _fetch_recently_played_from_web_api,
    _get_current_user_from_web_api,
    _SCOPE_ERROR,
)
from Database.utils import timeToInt

# Comfortably past WEB_API_POLL_INTERVAL_SECONDS so _checkWebApiBackfill's
# poll-interval guard is deterministically bypassed, regardless of how large
# time.monotonic() already is on the host running the test (e.g. a freshly
# booted CI runner has a much smaller monotonic clock than a long-uptime dev
# machine, which previously let a `_lastWebApiPollTime = 0` reset silently
# fail to force an immediate check).
_MONOTONIC_NOW = WEB_API_POLL_INTERVAL_SECONDS * 10


def _isoFromTimestamp(ts):
    """The Web API's played_at spelling, so a test can place an item at an exact
    offset from another one (timeToInt is the inverse)."""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    @patch("requests.get")
    def test_fetch_recently_played_returns_scope_error_sentinel_on_insufficient_scope(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = '{"error": {"status": 403, "message": "Insufficient client scope"}}'
        mock_get.return_value = mock_response

        result = _fetch_recently_played_from_web_api("token123")
        self.assertIs(result, _SCOPE_ERROR)

    @patch("requests.get")
    def test_fetch_recently_played_returns_none_for_other_403(self, mock_get):
        """A 403 that isn't the scope rejection (e.g. a revoked/expired
        token) must NOT be mistaken for the scope-error sentinel - it's a
        different failure mode entirely."""
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = '{"error": {"status": 403, "message": "User not registered in the Developer Dashboard"}}'
        mock_get.return_value = mock_response

        result = _fetch_recently_played_from_web_api("token123")
        self.assertIsNone(result)

    @patch("requests.get")
    def test_fetch_recently_played_returns_none_on_other_failure(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_get.return_value = mock_response

        result = _fetch_recently_played_from_web_api("token123")
        self.assertIsNone(result)

    @patch("requests.get", side_effect=Exception("network down"))
    def test_fetch_recently_played_returns_none_on_network_exception(self, mock_get):
        result = _fetch_recently_played_from_web_api("token123")
        self.assertIsNone(result)

    @patch("requests.get")
    def test_get_current_user_failure_logs_the_account_it_belongs_to(self, mock_get):
        """Regression test: an admin scanning logs for a 502/other failure
        from /v1/me must be able to tell WHICH account's worker it came
        from - previously this log line carried no user identifier at all."""
        mock_response = MagicMock()
        mock_response.status_code = 502
        mock_response.text = '{"error": {"status": 502, "message": "An unexpected error occurred."}}'
        mock_get.return_value = mock_response

        with self.assertLogs("Database.Listeners.spotifyListener", level="ERROR") as cm:
            result = _get_current_user_from_web_api("token123", logUser="alice")

        self.assertIsNone(result)
        self.assertTrue(any("alice" in m for m in cm.output))

    @patch("requests.get", side_effect=Exception("network down"))
    def test_get_current_user_exception_logs_the_account_it_belongs_to(self, mock_get):
        with self.assertLogs("Database.Listeners.spotifyListener", level="ERROR") as cm:
            _get_current_user_from_web_api("token123", logUser="alice")

        self.assertTrue(any("alice" in m for m in cm.output))

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="token123")
    def test_check_web_api_backfill_validation_warning_identifies_the_account(self, mock_refresh):
        """Regression test for the reported log line: 'Could not validate Web
        API user, skipping backfill.' gave no way to tell which of several
        concurrently-running per-user workers it came from."""
        self.mock_get_current_user.return_value = None   #< simulates the /v1/me failure

        get_credentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "cs", "refresh_token": "rt",
        })
        with patch("Database.Listeners.spotifyListener.Spotify") as mock_spotify_cls:
            mock_sp = MagicMock()
            mock_sp.current_user_recently_played.return_value = []
            mock_spotify_cls.return_value = mock_sp
            listener = Listener("dummy_cookie", email="alice@example.com", get_credentials=get_credentials)
        listener._lastWebApiPollTime = 0

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW), \
             self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm:
            listener._checkWebApiBackfill(MagicMock())

        self.assertTrue(any(
            "Could not validate Web API user" in m and "alice" in m for m in cm.output))

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
        listener.email = "alice@example.com"
        listener.user = "alice"  #< matches Listener.__init__; self.logUser reads both
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

    def _makeListenerWithScopeCallback(self, on_scope_status_change):
        get_credentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "cs", "refresh_token": "rt",
        })
        with patch("Database.Listeners.spotifyListener.Spotify") as mock_spotify_cls:
            mock_sp = MagicMock()
            mock_sp.current_user_recently_played.return_value = []
            mock_spotify_cls.return_value = mock_sp
            listener = Listener("dummy_cookie", email="alice@example.com",
                                 get_credentials=get_credentials,
                                 on_scope_status_change=on_scope_status_change)
        listener._lastWebApiPollTime = 0
        return listener

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api", return_value=_SCOPE_ERROR)
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="token123")
    def test_check_web_api_backfill_single_scope_error_does_not_report(self, mock_refresh, mock_fetch):
        """Spotify's recently-played endpoint has been observed to answer a
        one-off poll with a scope error even for a correctly-scoped token -
        one blip must not yet trigger the reauth-needed flag/prompt."""
        on_scope_status_change = MagicMock()
        listener = self._makeListenerWithScopeCallback(on_scope_status_change)
        callback = MagicMock()

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            listener._checkWebApiBackfill(callback)

        on_scope_status_change.assert_not_called()
        callback.assert_not_called()   #< never reaches item processing

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api", return_value=_SCOPE_ERROR)
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="token123")
    def test_check_web_api_backfill_reports_scope_error_after_threshold(self, mock_refresh, mock_fetch):
        """Only once the scope error repeats SCOPE_ERROR_CONFIRM_THRESHOLD
        times in a row (no successful poll in between) is it treated as
        definitive and reported."""
        on_scope_status_change = MagicMock()
        listener = self._makeListenerWithScopeCallback(on_scope_status_change)

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            for _ in range(SCOPE_ERROR_CONFIRM_THRESHOLD):
                listener._lastWebApiPollTime = 0   #< bypass the poll-interval guard each iteration
                on_scope_status_change.assert_not_called()
                listener._checkWebApiBackfill(MagicMock())

        on_scope_status_change.assert_called_once_with(True)

    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="token123")
    def test_check_web_api_backfill_success_resets_scope_error_streak(self, mock_refresh):
        """A single successful poll between two scope errors resets the
        consecutive-error count, so the flag never gets set from unrelated
        blips spread across an otherwise-healthy token."""
        on_scope_status_change = MagicMock()
        listener = self._makeListenerWithScopeCallback(on_scope_status_change)

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            for _ in range(SCOPE_ERROR_CONFIRM_THRESHOLD - 1):
                listener._lastWebApiPollTime = 0
                with patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api", return_value=_SCOPE_ERROR):
                    listener._checkWebApiBackfill(MagicMock())

            listener._lastWebApiPollTime = 0
            with patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api", return_value=[]):
                listener._checkWebApiBackfill(MagicMock())

            self.assertEqual(listener._consecutiveScopeErrors, 0)
            on_scope_status_change.assert_called_once_with(False)

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api", return_value=[])
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="token123")
    def test_check_web_api_backfill_clears_scope_error_on_success(self, mock_refresh, mock_fetch):
        """Any definitive response - even an empty item list - proves the
        token currently carries the required scope, so a previously-recorded
        reauth-needed flag must be cleared."""
        on_scope_status_change = MagicMock()
        listener = self._makeListenerWithScopeCallback(on_scope_status_change)

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            listener._checkWebApiBackfill(MagicMock())

        on_scope_status_change.assert_called_once_with(False)

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api", return_value=None)
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="token123")
    def test_check_web_api_backfill_leaves_scope_status_alone_on_transient_failure(self, mock_refresh, mock_fetch):
        """A transient failure (network error, rate limit, ...) is not proof
        of anything about the token's scope, so the flag must be left
        exactly as it was - neither set nor cleared."""
        on_scope_status_change = MagicMock()
        listener = self._makeListenerWithScopeCallback(on_scope_status_change)

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            listener._checkWebApiBackfill(MagicMock())

        on_scope_status_change.assert_not_called()

    @patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api", return_value=_SCOPE_ERROR)
    @patch("Database.Listeners.spotifyListener._refresh_spotify_access_token", return_value="token123")
    def test_check_web_api_backfill_without_scope_callback_does_not_raise(self, mock_refresh, mock_fetch):
        """on_scope_status_change is optional - existing callers/tests that
        don't pass it must be unaffected by a scope error."""
        listener = self._makeListenerWithScopeCallback(None)

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            listener._checkWebApiBackfill(MagicMock())   # must not raise

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

    _MISSED_ITEMS = [
        {"track": {"id": "track1", "duration_ms": 200000}, "played_at": "2026-07-26T14:30:00Z"},
        {"track": {"id": "track2", "duration_ms": 180000}, "played_at": "2026-07-26T14:20:00Z"},
    ]

    def _runBackfillWithMissedPlays(self):
        """Backfill over two plays neither cache knows about, so the
        'Backfilling N plays' line is reached and the callback runs."""
        listener = self._makeQuietBackfillListener()
        callback = MagicMock()
        with patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api",
                   return_value=list(self._MISSED_ITEMS)):
            with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                       return_value="token123"):
                with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
                    listener._checkWebApiBackfill(callback)
        return callback

    def test_backfilling_line_hidden_without_flask_debug(self):
        """The per-poll 'Backfilling N plays' line is routine progress, and the
        plays it announces are already recorded in the database with their
        web_api_backfill source - so it stays behind FLASK_DEBUG like the other
        backfill progress lines."""
        envWithoutDebug = {k: v for k, v in os.environ.items() if k != "FLASK_DEBUG"}
        with patch.dict(os.environ, envWithoutDebug, clear=True):
            with self.assertNoLogs("Database.Listeners.spotifyListener", level="INFO"):
                callback = self._runBackfillWithMissedPlays()

        # Quieter logging must not mean less backfilling.
        callback.assert_called_once()
        self.assertEqual(len(callback.call_args[0][0]), len(self._MISSED_ITEMS))

    def test_backfilling_line_hidden_with_falsy_flask_debug(self):
        with patch.dict(os.environ, {"FLASK_DEBUG": "0"}):
            with self.assertNoLogs("Database.Listeners.spotifyListener", level="INFO"):
                self._runBackfillWithMissedPlays()

    def test_backfilling_line_shown_with_flask_debug(self):
        with patch.dict(os.environ, {"FLASK_DEBUG": "1"}):
            with self.assertLogs("Database.Listeners.spotifyListener", level="INFO") as cm:
                self._runBackfillWithMissedPlays()

        self.assertTrue(any("Backfilling 2 plays from Web API" in m for m in cm.output))


class BackfillDatabaseDedupTestCase(unittest.TestCase):
    """The backfill's "is this play already recorded" test used to consult only
    two in-memory caches, both of which live and die with the Listener object -
    and a listener is rebuilt on every stale-feed reconnect (1,568 times over 11
    days for 3 users in app.log). So the first poll after every rebuild declared
    the whole 50-item page missing: 74,579 plays announced, 201 actually new.
    The database is what can tell a genuine gap from an empty cache."""

    #< played_at values two minutes apart, and a 3-minute track: long enough
    #  that an end-time interpretation lands well outside the 2s tolerance
    FIRST_PLAYED_AT = "2026-07-26T14:20:00Z"
    SECOND_PLAYED_AT = "2026-07-26T14:22:00Z"
    DURATION_MS = 180000

    def _items(self):
        return [
            {"track": {"id": "track1", "duration_ms": self.DURATION_MS}, "played_at": self.SECOND_PLAYED_AT},
            {"track": {"id": "track2", "duration_ms": self.DURATION_MS}, "played_at": self.FIRST_PLAYED_AT},
        ]

    def _runBackfill(self, getRecordedPlayTimes, items=None):
        getCredentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "cs", "refresh_token": "rt",
        })
        with patch("Database.Listeners.spotifyListener.Spotify") as mockSpotifyCls:
            mockSp = MagicMock()
            mockSp.current_user_recently_played.return_value = []
            mockSpotifyCls.return_value = mockSp
            listener = Listener("dummy_cookie", email="alice@example.com",
                                get_credentials=getCredentials,
                                get_recorded_play_times=getRecordedPlayTimes)
        listener._lastWebApiPollTime = 0
        callback = MagicMock()
        with patch("Database.Listeners.spotifyListener._get_current_user_from_web_api",
                   return_value={"id": "alice", "display_name": "Alice", "email": "alice@example.com"}):
            with patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api",
                       return_value=self._items() if items is None else items):
                with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                           return_value="token123"):
                    with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
                        listener._checkWebApiBackfill(callback)
        return callback

    def _backfilledTrackIds(self, callback):
        if not callback.call_args_list:
            return []
        return [item["track"]["id"] for item in callback.call_args[0][0]]

    def test_plays_already_in_the_database_are_not_backfilled(self):
        recorded = [("track2", timeToInt(self.FIRST_PLAYED_AT), None),
                    ("track1", timeToInt(self.SECOND_PLAYED_AT), None)]

        callback = self._runBackfill(MagicMock(return_value=recorded))

        callback.assert_not_called()

    def test_only_the_genuinely_missing_play_is_backfilled(self):
        """The case the feature exists for: one of the two plays was never
        recorded, and it must still come through."""
        callback = self._runBackfill(MagicMock(return_value=[("track2", timeToInt(self.FIRST_PLAYED_AT), None)]))

        self.assertEqual(self._backfilledTrackIds(callback), ["track1"])

    def test_a_recorded_play_stored_at_the_start_time_still_counts(self):
        """Spotify's played_at may be the END of a play, in which case the
        stored row sits one track-length earlier - the same both-interpretations
        test the in-memory dedup already applies."""
        recorded = [
            ("track2", timeToInt(self.FIRST_PLAYED_AT) - self.DURATION_MS // 1000, None),
            ("track1", timeToInt(self.SECOND_PLAYED_AT) - self.DURATION_MS // 1000, None),
        ]

        callback = self._runBackfill(MagicMock(return_value=recorded))

        callback.assert_not_called()

    def test_a_paused_plays_backfill_copy_is_recognised_by_the_recorded_end_time(self):
        """The 2026-08-04 incident: a play paused for ~3 minutes mid-track. The
        listener row holds the START time, and Spotify's played_at reported the
        END - which sits duration PLUS pause after the start, so both existing
        interpretations (start matches start, start = end - duration) miss and
        the same listen was recorded twice. The listener inserts its row at the
        track-change moment, so the row's created_at IS the observed end,
        pauses included - that is the anchor that recognises the copy."""
        pauseSeconds = 186
        insertLagSeconds = 1  #< callback-to-insert latency observed live
        endTs = timeToInt(self.SECOND_PLAYED_AT)
        startTs = endTs - self.DURATION_MS // 1000 - pauseSeconds
        items = [{"track": {"id": "track1", "duration_ms": self.DURATION_MS},
                  "played_at": self.SECOND_PLAYED_AT}]
        recorded = [("track1", startTs, endTs + insertLagSeconds)]

        callback = self._runBackfill(MagicMock(return_value=recorded), items=items)

        callback.assert_not_called()

    def test_a_recorded_end_outside_the_tolerance_does_not_suppress(self):
        """The end-time arm must stay a point match, not a window: a recorded
        end a minute away is evidence of a DIFFERENT listen, and suppressing on
        it would lose a genuine play for good (nothing retries a suppressed
        item)."""
        farSeconds = 60
        endTs = timeToInt(self.SECOND_PLAYED_AT)
        startTs = endTs - self.DURATION_MS // 1000 - 186
        items = [{"track": {"id": "track1", "duration_ms": self.DURATION_MS},
                  "played_at": self.SECOND_PLAYED_AT}]
        recorded = [("track1", startTs, endTs - farSeconds)]

        callback = self._runBackfill(MagicMock(return_value=recorded), items=items)

        self.assertEqual(self._backfilledTrackIds(callback), ["track1"])

    def test_another_tracks_recorded_end_does_not_suppress_a_missing_one(self):
        """The end-time arm is per track like the other two: another track's
        recorded end at the same instant proves nothing about this one."""
        endTs = timeToInt(self.SECOND_PLAYED_AT)
        items = [{"track": {"id": "track1", "duration_ms": self.DURATION_MS},
                  "played_at": self.SECOND_PLAYED_AT}]
        recorded = [("unrelated", endTs - 400, endTs)]

        callback = self._runBackfill(MagicMock(return_value=recorded), items=items)

        self.assertEqual(self._backfilledTrackIds(callback), ["track1"])

    def test_another_tracks_recorded_play_does_not_suppress_a_missing_one(self):
        """The dedup compares per track. A bare set of timestamps let ANY
        recorded play within the tolerance answer for the candidate - and the
        database half of that set spans the whole page's window (hours, hundreds
        of rows, skip bursts seconds apart), so an unrelated row silently
        consumed a genuine gap."""
        recorded = [("unrelated", timeToInt(self.FIRST_PLAYED_AT), None),
                    ("unrelated", timeToInt(self.SECOND_PLAYED_AT), None)]

        callback = self._runBackfill(MagicMock(return_value=recorded))

        self.assertEqual(sorted(self._backfilledTrackIds(callback)), ["track1", "track2"])

    def test_the_previous_tracks_recorded_end_does_not_suppress_a_gapless_next_play(self):
        """The systematic case, not a coincidence: under gapless playback the
        DERIVED start of the missing track (played_at - duration, the end-time
        interpretation) equals the recorded end of the track before it. So the
        one recorded neighbour a real gap always has beside it was exactly what
        suppressed the gap."""
        firstEnd = timeToInt(self.FIRST_PLAYED_AT)
        items = [
            #< gapless: track1 ends one full track-length after track2 did
            {"track": {"id": "track1", "duration_ms": self.DURATION_MS},
             "played_at": _isoFromTimestamp(firstEnd + self.DURATION_MS // 1000)},
            {"track": {"id": "track2", "duration_ms": self.DURATION_MS},
             "played_at": self.FIRST_PLAYED_AT},
        ]

        callback = self._runBackfill(MagicMock(return_value=[("track2", firstEnd, None)]), items=items)

        self.assertEqual(self._backfilledTrackIds(callback), ["track1"])

    def test_lookup_window_covers_the_whole_page_plus_a_track_length(self):
        """A too-narrow window would silently miss the rows it went looking for,
        which reads exactly like "nothing was recorded"."""
        lookup = MagicMock(return_value=[])

        self._runBackfill(lookup)

        startTs, endTs = lookup.call_args[0]
        durationSeconds = self.DURATION_MS // 1000
        self.assertLessEqual(startTs, timeToInt(self.FIRST_PLAYED_AT) - durationSeconds)
        self.assertGreaterEqual(endTs, timeToInt(self.SECOND_PLAYED_AT))

    def test_a_failing_lookup_falls_back_to_the_previous_behaviour(self):
        """A database problem must not silence the backfill: announcing an
        already-recorded play is harmless (appendTrackData's own guard drops
        it), losing a genuinely missing one is not."""
        callback = self._runBackfill(MagicMock(side_effect=RuntimeError("database is locked")))

        self.assertEqual(sorted(self._backfilledTrackIds(callback)), ["track1", "track2"])

    def test_without_the_callback_behaviour_is_unchanged(self):
        """Callers that don't pass the callback (older tests, any embedder) keep
        the in-memory-cache-only behaviour."""
        callback = self._runBackfill(None)

        self.assertEqual(sorted(self._backfilledTrackIds(callback)), ["track1", "track2"])


class ListenerLogIdentityTestCase(unittest.TestCase):
    """The listener's routine log lines identify the account by internal user
    key, not email address. These lines are the highest-volume in app.log
    (~3,000 email occurrences over 11 days, 1,568 from listener init alone);
    the key identifies the account exactly as well without writing an address
    to disk on every poll."""

    def _makeListener(self, user="alice", email="alice@example.com"):
        with patch("Database.Listeners.spotifyListener.Spotify") as mockSpotifyCls:
            mockSp = MagicMock()
            mockSp.current_user_recently_played.return_value = []
            mockSp.current_user.return_value = {"id": "spotify-alice", "email": email}
            mockSpotifyCls.return_value = mockSp
            return Listener("dummy_cookie", email=email, user=user)

    def test_logUser_prefers_the_internal_key(self):
        self.assertEqual(self._makeListener().logUser, "alice")

    def test_logUser_falls_back_to_email_when_no_key_given(self):
        """Callers that predate the `user` kwarg (and tests) must still produce
        an identifiable log line - an anonymous one would be worse than a
        private one."""
        self.assertEqual(self._makeListener(user=None).logUser, "alice@example.com")

    def test_listener_init_logs_the_key_not_the_email(self):
        #< DEBUG: the line itself was demoted with the rest of the reconnect
        #  cycle, but what it prints when it does fire still must not be an email
        with self.assertLogs("Database.Listeners.spotifyListener", level="DEBUG") as cm:
            self._makeListener()

        initLines = [m for m in cm.output if "Listener initialized" in m]
        self.assertTrue(initLines, "expected an init line")
        self.assertTrue(all("alice@example.com" not in m for m in initLines))
        self.assertTrue(any("alice" in m for m in initLines))

    def test_callback_line_logs_the_key_not_the_email(self):
        listener = self._makeListener()
        listener.sp.current_user_recently_played.return_value = [{"played_at": 1}]

        with self.assertLogs("Database.Listeners.spotifyListener", level="INFO") as cm:
            listener._checkOnce(MagicMock(), onStale=None)

        callbackLines = [m for m in cm.output if "Listener callback" in m]
        self.assertTrue(callbackLines, "expected a callback line")
        self.assertTrue(all("alice@example.com" not in m for m in callbackLines))

    def test_web_api_helpers_take_the_key_not_the_email(self):
        """The three module-level helpers exist below the Listener and receive
        the identifier explicitly - they must be handed the key too, or the
        highest-volume error lines keep carrying addresses."""
        listener = self._makeListener()
        listener._lastWebApiPollTime = 0

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW), \
             patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                   return_value="token123") as mockRefresh, \
             patch("Database.Listeners.spotifyListener._get_current_user_from_web_api",
                   return_value={"id": "spotify-alice", "email": "alice@example.com"}) as mockUser, \
             patch("Database.Listeners.spotifyListener._fetch_recently_played_from_web_api",
                   return_value=[]) as mockFetch:
            listener.get_credentials = MagicMock(return_value={
                "client_id": "cid", "client_secret": "cs", "refresh_token": "rt"})
            listener._checkWebApiBackfill(MagicMock())

        self.assertEqual(mockRefresh.call_args.kwargs["logUser"], "alice")
        self.assertEqual(mockUser.call_args.kwargs["logUser"], "alice")
        self.assertEqual(mockFetch.call_args.kwargs["logUser"], "alice")
