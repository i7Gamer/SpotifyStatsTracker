import unittest
from unittest.mock import MagicMock, patch
import json
import signal
import threading
import concurrent.futures
import websockets.sync.client
import spotapi.status
import spotapi.websocket
import spotapi.public

from Database.patches import patch_spotipy_free


def fakeTrackUnion(trackId):
    """Minimal raw trackUnion shape (spotapi's GraphQL response format) - just
    enough fields for SpotifyFormatter.formatTrack/formatArtists to succeed."""
    return {
        "uri": f"spotify:track:{trackId}",
        "name": f"Song {trackId}",
        "duration": {"totalMilliseconds": 200000},
        "contentRating": {"label": "NONE"},
        "firstArtist": {"items": []},
        "otherArtists": {"items": []},
    }


def setUpModule():
    # Database.patches applies its SpotipyFree patch once, at whatever moment
    # Database (the package) first gets imported. If that happened to be while
    # another test module's sys.modules["SpotipyFree"] mock was still in place
    # (unittest discover imports every test module before running any tests), the
    # real SpotipyFree.Spotify would never get patched for the rest of the process.
    # Re-applying here makes this module correct regardless of import order.
    from Database.patches import patch_spotapi_user, patch_last_played
    patch_spotipy_free()
    patch_spotapi_user()
    patch_last_played()


class TestPatches(unittest.TestCase):
    """Verify that monkey-patches are correctly applied to websockets and spotapi."""

    def test_websockets_connect_default_arguments(self):
        """websockets.sync.client.connect should default ping_interval/ping_timeout to None."""
        mock_connect = MagicMock()
        # Temporarily swap the original connect with our mock
        from Database.patches import original_connect
        try:
            with patch("Database.patches.original_connect", mock_connect):
                # When calling websockets.sync.client.connect with some arguments
                websockets.sync.client.connect("wss://example.com", user_agent_header="test-ua")
                
                # Check that original_connect was called with defaults overridden to None
                mock_connect.assert_called_once_with(
                    "wss://example.com",
                    user_agent_header="test-ua",
                    ping_interval=None,
                    ping_timeout=None
                )
        finally:
            pass

    def test_websocket_streamer_init_restores_previous_sigint_handler(self):
        """WebsocketStreamer.__init__ must not leave spotapi's own SIGINT handler
        installed. Even if the underlying init hijacks SIGINT (as spotapi's real
        implementation does, to call ws.close(); exit(0)), whatever handler was
        registered beforehand (e.g. Python/Werkzeug's default) must win, so Ctrl+C
        doesn't get hijacked mid-request by a background listener thread."""
        def fakeOriginalInit(self, *args, **kwargs):
            signal.signal(signal.SIGINT, lambda signum, frame: None)

        sentinelHandler = lambda signum, frame: None
        originalHandler = signal.signal(signal.SIGINT, sentinelHandler)
        try:
            instance = spotapi.websocket.WebsocketStreamer.__new__(spotapi.websocket.WebsocketStreamer)
            with patch("Database.patches.original_websocket_streamer_init", fakeOriginalInit):
                spotapi.websocket.WebsocketStreamer.__init__(instance, MagicMock())
            self.assertIs(signal.getsignal(signal.SIGINT), sentinelHandler)
        finally:
            signal.signal(signal.SIGINT, originalHandler)

    def test_player_status_has_reconnect_method(self):
        """PlayerStatus class must have reconnect method injected."""
        self.assertTrue(hasattr(spotapi.status.PlayerStatus, "reconnect"))
        self.assertTrue(callable(spotapi.status.PlayerStatus.reconnect))

    @patch("websockets.sync.client.connect")
    def test_player_status_reconnect_flow(self, mock_ws_connect):
        """reconnect() must call close on old socket, renew sessions, connect, get init packet, and register."""
        # Create a mock PlayerStatus instance
        self.assertTrue(hasattr(spotapi.status.PlayerStatus, "reconnect"))
        
        # We will mock the required methods/attributes on PlayerStatus
        mock_ws = MagicMock()
        mock_ws_connect.return_value = mock_ws
        
        instance = MagicMock(spec=spotapi.status.PlayerStatus)
        instance.ws = mock_ws
        instance.base = MagicMock()
        
        # When get_init_packet is called, it returns a new connection ID
        instance.get_init_packet.return_value = "new-conn-id"
        
        # Thread status mock
        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = False
        instance.keep_alive_thread = mock_thread
        
        # Call the reconnect function bound to the instance
        spotapi.status.PlayerStatus.reconnect(instance)
        
        # Verify old websocket is closed
        mock_ws.close.assert_called_once()
        
        # Verify sessions and tokens are renewed
        instance.base.get_session.assert_called_once()
        instance.base.get_client_token.assert_called_once()
        
        # Verify we connect to the new websocket URI
        mock_ws_connect.assert_called_once()
        
        # Verify connection_id was updated
        self.assertEqual(instance.connection_id, "new-conn-id")
        
        # Verify device registration and connection
        instance.register_device.assert_called_once()
        instance.connect_device.assert_called_once()
        
        # Verify keep alive thread was restarted
        mock_thread.is_alive.assert_called_once()

    def test_spotify_init_saves_email(self):
        """SpotipyFree.Spotify should store email on init."""
        import SpotipyFree
        
        # Test with kwarg
        sp1 = SpotipyFree.Spotify(email="user@test.com")
        self.assertEqual(sp1.email, "user@test.com")
        
        # Test with positional arg
        sp2 = SpotipyFree.Spotify(False, False, "dummy.json", "positional@test.com")
        self.assertEqual(sp2.email, "positional@test.com")

    @patch("spotapi.Login.from_saver")
    @patch("SpotipyFree.getCookiesFile")
    @patch("builtins.open")
    def test_spotify_login_resolves_missing_cookies_file_via_module_level_helper(
        self, mock_open, mock_get_cookies_file, mock_from_saver
    ):
        """login(cookiesFile=None) must resolve the path via the module-level
        SpotipyFree.getCookiesFile() function - it's re-exported at package level,
        not a method on the Spotify class, so calling it as
        SpotipyFree.Spotify.getCookiesFile() raises AttributeError and crashes any
        background reconnect/login refresh that omits cookiesFile."""
        mock_get_cookies_file.return_value = "resolved_cookies.json"
        mock_file_data = json.dumps([{"identifier": "user1@test.com", "cookies": {}}])
        mock_open.return_value.__enter__.return_value.read.return_value = mock_file_data

        sp = self._newSpotifyInstance()
        result = sp.login(cookiesFile=None)

        mock_get_cookies_file.assert_called_once()
        mock_open.assert_called_once_with("resolved_cookies.json", "r")
        self.assertTrue(result)

    @patch("spotapi.Login.from_saver")
    @patch("builtins.open")
    def test_spotify_login_retrieves_correct_session(self, mock_open, mock_from_saver):
        """SpotipyFree.Spotify.login should select the session matching self.email."""
        import SpotipyFree
        
        # Mock file content
        import json
        mock_file_data = json.dumps([
            {"identifier": "user1@test.com", "cookies": {}},
            {"identifier": "user2@test.com", "cookies": {}}
        ])
        
        mock_open.return_value.__enter__.return_value.read.return_value = mock_file_data
        
        sp = SpotipyFree.Spotify(cookiesFile="cookies.json", email="user2@test.com")
        
        # SpotipyFree.Spotify init might call login internally. Let's force it again to test.
        sp.login("cookies.json")
        
        # The from_saver call should have been called with identifier="user2@test.com"
        mock_from_saver.assert_called_with(unittest.mock.ANY, unittest.mock.ANY, "user2@test.com")

    @patch("spotapi.Login.from_saver")
    @patch("builtins.open")
    def test_spotify_login_fallback_to_first_session(self, mock_open, mock_from_saver):
        """SpotipyFree.Spotify.login should fallback to first session if email is not found."""
        import SpotipyFree
        import json
        
        mock_file_data = json.dumps([
            {"identifier": "user1@test.com", "cookies": {}},
            {"identifier": "user2@test.com", "cookies": {}}
        ])
        
        mock_open.return_value.__enter__.return_value.read.return_value = mock_file_data
        
        # With email not in sessions list
        sp = SpotipyFree.Spotify(cookiesFile="cookies.json", email="unknown@test.com")
        sp.login("cookies.json")
        mock_from_saver.assert_called_with(unittest.mock.ANY, unittest.mock.ANY, "user1@test.com")
        
        # With no email
        sp_no_email = SpotipyFree.Spotify(cookiesFile="cookies.json")
        sp_no_email.login("cookies.json")
        mock_from_saver.assert_called_with(unittest.mock.ANY, unittest.mock.ANY, "user1@test.com")

    def test_config_client_default_is_shared_singleton(self):
        """Sanity check on the dependency itself: spotapi.Config's `client`
        field is declared as `field(default=TLSClient(...))` rather than
        `field(default_factory=...)`. dataclasses only rejects known mutable
        defaults (list/dict/set), so this TLSClient instance is built once at
        import time and silently reused as the default for every Config()
        call that omits client= - the exact footgun patched_spotify_login
        works around below. If a future spotapi upgrade switches this to a
        default_factory, this test should fail to flag that the workaround
        is no longer needed."""
        cfgA = spotapi.Config(logger=spotapi.Logger())
        cfgB = spotapi.Config(logger=spotapi.Logger())
        self.assertIs(cfgA.client, cfgB.client)

    @patch("spotapi.Login.from_saver")
    @patch("builtins.open")
    def test_spotify_login_uses_isolated_client_not_shared_default(self, mock_open, mock_from_saver):
        """Regression test for cross-user session contamination: since Login
        stores cookies directly on cfg.client (see spotapi.Login.from_cookies,
        which does cfg.client.cookies.clear() then sets this user's cookies),
        two Spotify() instances sharing spotapi.Config's default TLSClient
        would clobber each other's cookies whenever their logins/reconnects
        overlapped - causing current_user() to intermittently return the
        wrong user's identity. login() must construct a fresh TLSClient per
        call so each user gets an isolated cookie jar."""
        mock_file_data = json.dumps([{"identifier": "user1@test.com", "cookies": {}}])
        mock_open.return_value.__enter__.return_value.read.return_value = mock_file_data

        sp1 = self._newSpotifyInstance()
        sp1.email = "user1@test.com"
        sp1.login("cookies.json")

        sp2 = self._newSpotifyInstance()
        sp2.email = "user1@test.com"
        sp2.login("cookies.json")

        self.assertEqual(mock_from_saver.call_count, 2)
        cfg1 = mock_from_saver.call_args_list[0].args[1]
        cfg2 = mock_from_saver.call_args_list[1].args[1]
        self.assertIsNot(cfg1.client, cfg2.client)
        self.assertIsNot(cfg1.client, spotapi.Config(logger=spotapi.Logger()).client)

    def _newSpotifyInstance(self):
        import SpotipyFree
        instance = SpotipyFree.Spotify.__new__(SpotipyFree.Spotify)
        instance.getIsrc = False
        return instance

    @patch("spotapi.Song")
    @patch("spotapi.Public")
    def test_spotify_track_uses_public_song_info_not_song(self, mock_public, mock_song):
        """Spotify.track() must fetch metadata via spotapi.Public's locked client
        pool, not spotapi.Song()'s process-wide shared-default client."""
        mock_public.song_info.return_value = {"data": {"trackUnion": fakeTrackUnion("abc123")}}

        instance = self._newSpotifyInstance()
        result = instance.track("abc123")

        mock_public.song_info.assert_called_once_with("abc123")
        mock_song.assert_not_called()
        self.assertEqual(result["track_id"], "abc123")
        self.assertEqual(result["name"], "Song abc123")

    @patch("spotapi.Song")
    @patch("spotapi.Public")
    def test_spotify_track_passes_playability_through(self, mock_public, mock_song):
        """SpotifyFormatter drops playability - the patched track() must re-attach
        it so downstream formatting can record why a track isn't playable."""
        union = fakeTrackUnion("abc123")
        union["playability"] = {"playable": False, "reason": "COUNTRY_RESTRICTED"}
        mock_public.song_info.return_value = {"data": {"trackUnion": union}}

        instance = self._newSpotifyInstance()
        result = instance.track("abc123")

        self.assertEqual(result["playability"], {"playable": False, "reason": "COUNTRY_RESTRICTED"})

    @patch("spotapi.Song")
    @patch("spotapi.Public")
    def test_spotify_track_resolves_url_before_lookup(self, mock_public, mock_song):
        """A Spotify URL/URI passed to track() must be resolved to a bare id
        before being handed to Public.song_info (unchanged from the original
        behavior - only the fetch mechanism changed)."""
        mock_public.song_info.return_value = {"data": {"trackUnion": fakeTrackUnion("xyz789")}}

        instance = self._newSpotifyInstance()
        instance.track("https://open.spotify.com/track/xyz789")

        mock_public.song_info.assert_called_once_with("xyz789")
        mock_song.assert_not_called()

    @patch("spotapi.Song")
    @patch("spotapi.Public")
    def test_spotify_track_isrc_lookup_still_applied(self, mock_public, mock_song):
        """getIsrc=True must still attach external_ids.isrc, unchanged from the
        original method body."""
        mock_public.song_info.return_value = {"data": {"trackUnion": fakeTrackUnion("iso1")}}

        instance = self._newSpotifyInstance()
        instance.getIsrc = True
        instance._getIsrc = MagicMock(return_value="US-ISO-01")

        result = instance.track("iso1")

        instance._getIsrc.assert_called_once_with("iso1")
        self.assertEqual(result["external_ids"], {"isrc": "US-ISO-01"})

    @patch("spotapi.Song")
    @patch("spotapi.Public")
    def test_spotify_track_concurrent_calls_do_not_cross_contaminate(self, mock_public, mock_song):
        """Regression test for the race this patch fixes: the original
        implementation shared one spotapi.Song() client across every thread, so
        concurrent track() calls (as the importer's ThreadPoolExecutor pre-fetch
        issues) could authenticate/return data for the wrong track. With the
        patch, each call must still resolve to exactly the track it asked for,
        and the unsafe spotapi.Song() path must never be touched."""
        mock_public.song_info.side_effect = lambda trackId: {
            "data": {"trackUnion": fakeTrackUnion(trackId)}
        }

        instance = self._newSpotifyInstance()
        trackIds = [f"track{i}" for i in range(50)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(instance.track, trackIds))

        for trackId, result in zip(trackIds, results):
            self.assertEqual(result["track_id"], trackId)
        mock_song.assert_not_called()

    def test_public_song_info_uses_locked_pool_not_shared_default(self):
        """Sanity check on the dependency itself: spotapi.public.Pooler (what
        spotapi.Public.song_info checks clients out of) must hand out distinct
        objects until one is returned, rather than one shared instance. If a
        future spotapi upgrade changes this, the thread-safety assumption behind
        the patch above no longer holds and this test should fail to flag it."""
        pool = spotapi.public.Pooler(factory=object)
        first = pool.get()
        second = pool.get()
        self.assertIsNot(first, second)

        pool.put(first)
        third = pool.get()
        self.assertIs(third, first)

    def test_patched_user_get_user_info_behavior(self):
        import spotapi.user
        from spotapi.exceptions import UserError

        # Create a mock login and mock client
        mock_login = MagicMock()
        mock_login.logged_in = True

        # Create user instance
        user_inst = spotapi.user.User(mock_login)

        # Define mock responses
        mock_resp_success_json = MagicMock()
        mock_resp_success_json.status_code = 200
        mock_resp_success_json.fail = False
        mock_resp_success_json.response = {"id": "test_user", "email": "test@example.com"}
        mock_resp_success_json.raw.headers = {"X-Csrf-Token": "test_csrf"}

        mock_login.client.get.return_value = mock_resp_success_json

        # Verify success case
        res = user_inst.get_user_info()
        self.assertEqual(res["id"], "test_user")
        self.assertEqual(user_inst.csrf_token, "test_csrf")

        # Verify non-JSON/non-Mapping success response logs warning and raises UserError
        mock_resp_non_json = MagicMock()
        mock_resp_non_json.status_code = 200
        mock_resp_non_json.fail = False
        mock_resp_non_json.response = "Invalid HTML / Cloudflare screen"
        mock_resp_non_json.raw.headers = {}
        mock_login.client.get.return_value = mock_resp_non_json

        with self.assertLogs("Database.patches", level="WARNING") as log_capture:
            with self.assertRaises(UserError) as err_ctx:
                user_inst.get_user_info()

            # Check log and exception message
            self.assertIn("non-Mapping response", log_capture.output[0])
            self.assertIn("Invalid JSON", str(err_ctx.exception))
            self.assertIn("Status: 200", str(err_ctx.exception))
            self.assertIn("Type: str", str(err_ctx.exception))
            self.assertIn("Response: Invalid HTML", str(err_ctx.exception))

        # Verify failed request logs warning and raises UserError
        mock_resp_fail = MagicMock()
        mock_resp_fail.status_code = 429
        mock_resp_fail.fail = True
        mock_resp_fail.error.string = "Too Many Requests"
        mock_resp_fail.response = "Rate limit hit"
        mock_resp_fail.raw.headers = {"Retry-After": "60"}
        mock_login.client.get.return_value = mock_resp_fail

        with self.assertLogs("Database.patches", level="WARNING") as log_capture:
            with self.assertRaises(UserError) as err_ctx:
                user_inst.get_user_info()

            self.assertIn("HTTP request failed", log_capture.output[0])
            self.assertEqual(err_ctx.exception.error, "Too Many Requests")

    def test_patched_user_get_plan_info_behavior(self):
        import spotapi.user
        from spotapi.exceptions import UserError

        mock_login = MagicMock()
        mock_login.logged_in = True
        user_inst = spotapi.user.User(mock_login)

        # Verify success case
        mock_resp_success = MagicMock()
        mock_resp_success.status_code = 200
        mock_resp_success.fail = False
        mock_resp_success.response = {"plan": "premium"}
        mock_login.client.get.return_value = mock_resp_success

        res = user_inst.get_plan_info()
        self.assertEqual(res["plan"], "premium")

        # Verify non-JSON/non-Mapping success response
        mock_resp_non_json = MagicMock()
        mock_resp_non_json.status_code = 200
        mock_resp_non_json.fail = False
        mock_resp_non_json.response = "Plan text error"
        mock_resp_non_json.raw.headers = {}
        mock_login.client.get.return_value = mock_resp_non_json

        with self.assertLogs("Database.patches", level="WARNING") as log_capture:
            with self.assertRaises(UserError) as err_ctx:
                user_inst.get_plan_info()

            self.assertIn("non-Mapping response", log_capture.output[0])
            self.assertIn("Invalid JSON", str(err_ctx.exception))

    def test_patched_keep_alive_exits_quietly_on_clean_close(self):
        """A clean close handshake (ConnectionClosedOK) must end the ping loop
        without any reconnect attempt."""
        from Database.patches import patched_keep_alive
        import websockets.exceptions

        exc = websockets.exceptions.ConnectionClosedOK(rcvd=None, sent=None)
        mock_original = MagicMock(side_effect=exc)

        with patch("Database.patches.original_keep_alive", mock_original):
            instance = MagicMock()
            patched_keep_alive(instance)

        mock_original.assert_called_once_with(instance)
        instance.reconnect.assert_not_called()

    def test_patched_keep_alive_exits_on_deliberate_close_flag(self):
        """spotifyListener.stop() sets _deliberate_close before closing the ws -
        keep_alive must exit instead of reconnecting, even if the close handshake
        was abnormal (ConnectionClosedError)."""
        from Database.patches import patched_keep_alive
        import websockets.exceptions

        exc = websockets.exceptions.ConnectionClosedError(rcvd=None, sent=None)
        mock_original = MagicMock(side_effect=exc)

        with patch("Database.patches.original_keep_alive", mock_original):
            instance = MagicMock()
            instance._deliberate_close = True
            patched_keep_alive(instance)

        mock_original.assert_called_once_with(instance)
        instance.reconnect.assert_not_called()

    def test_patched_keep_alive_reconnects_on_unexpected_drop(self):
        """An unexpected drop (ConnectionClosedError) must trigger self.reconnect()
        and resume the ping loop on the new connection."""
        from Database.patches import patched_keep_alive
        import websockets.exceptions

        exc = websockets.exceptions.ConnectionClosedError(rcvd=None, sent=None)
        # First run drops the connection, second run (after reconnect) exits normally
        mock_original = MagicMock(side_effect=[exc, None])

        with patch("Database.patches.original_keep_alive", mock_original):
            instance = MagicMock()
            instance._deliberate_close = False
            patched_keep_alive(instance)

        self.assertEqual(mock_original.call_count, 2)
        instance.reconnect.assert_called_once_with()

    def test_patched_keep_alive_gives_up_after_max_reconnect_failures(self):
        """If reconnect() keeps failing, the loop must stop after
        WS_KEEP_ALIVE_MAX_RECONNECT_FAILURES attempts instead of spinning forever."""
        from Database.patches import patched_keep_alive, WS_KEEP_ALIVE_MAX_RECONNECT_FAILURES
        import websockets.exceptions

        exc = websockets.exceptions.ConnectionClosedError(rcvd=None, sent=None)
        mock_original = MagicMock(side_effect=exc)

        with patch("Database.patches.original_keep_alive", mock_original), \
                patch("Database.patches.time") as mock_time:
            instance = MagicMock()
            instance._deliberate_close = False
            instance.reconnect.side_effect = Exception("Spotify unreachable")
            patched_keep_alive(instance)

        self.assertEqual(instance.reconnect.call_count, WS_KEEP_ALIVE_MAX_RECONNECT_FAILURES)
        # Backoff sleeps between attempts, but not after the final give-up
        self.assertEqual(mock_time.sleep.call_count, WS_KEEP_ALIVE_MAX_RECONNECT_FAILURES - 1)

    def test_patched_keep_alive_without_reconnect_method_exits(self):
        """A plain WebsocketStreamer (no injected reconnect) must exit gracefully
        instead of raising AttributeError."""
        from Database.patches import patched_keep_alive
        import types
        import websockets.exceptions

        exc = websockets.exceptions.ConnectionClosedError(rcvd=None, sent=None)
        mock_original = MagicMock(side_effect=exc)

        with patch("Database.patches.original_keep_alive", mock_original):
            instance = types.SimpleNamespace()  #< no reconnect attribute
            patched_keep_alive(instance)

        mock_original.assert_called_once_with(instance)

    def test_patched_update_loop_handles_none_timestamp_gracefully(self):
        """The patched updateLoop should sleep and continue without raising or calling reconnect when state or timestamp is None."""
        from SpotipyFree.LastPlayed import LastPlayedManger
        import time
        
        manager = MagicMock()
        callback = MagicMock()
        
        state_none_timestamp = MagicMock()
        state_none_timestamp.timestamp = None
        state_none_timestamp.track = None
        
        manager.state = state_none_timestamp
        
        with patch("SpotipyFree.LastPlayed.PlayerStatus"):
            lpm = LastPlayedManger(MagicMock())
        lpm.manager = manager
        lpm.run = True
        
        def mock_sleep(secs):
            lpm.run = False
            
        with patch("time.sleep", side_effect=mock_sleep):
            lpm.updateLoop(callback, refreshInterval=1)
            
        callback.assert_not_called()
        manager.reconnect.assert_not_called()


if __name__ == "__main__":
    unittest.main()


