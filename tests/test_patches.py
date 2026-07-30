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

import Database.rate_limit as rateLimitModule
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


class _ScriptedStateManager:
    """Stands in for LastPlayedManger.manager: each `state` access consumes the
    next scripted result - Exception instances are raised, anything else is
    returned. reconnect() is a plain MagicMock for call assertions."""
    def __init__(self, results):
        self._results = list(results)
        self.reconnect = MagicMock()

    @property
    def state(self):
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def makeIdleState():
    """A state the update loop skips gracefully (no timestamp/track) - fetching
    it still counts as a successful poll."""
    state = MagicMock()
    state.timestamp = None
    state.track = None
    return state


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
        manager._deliberate_close = False  #< a bare MagicMock's auto-attribute is truthy = deliberate close
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

    def _runUpdateLoopIterations(self, manager, iterations):
        """Run the patched updateLoop for exactly `iterations` passes (each pass
        ends in one time.sleep call), returning the callback mock."""
        from SpotipyFree.LastPlayed import LastPlayedManger

        callback = MagicMock()
        with patch("SpotipyFree.LastPlayed.PlayerStatus"):
            lpm = LastPlayedManger(MagicMock())
        lpm.manager = manager
        lpm.run = True

        sleepCount = [0]

        def mockSleep(_secs):
            sleepCount[0] += 1
            if sleepCount[0] >= iterations:
                lpm.run = False

        with patch("time.sleep", side_effect=mockSleep):
            lpm.updateLoop(callback, refreshInterval=1)
        return callback

    def test_patched_update_loop_transient_state_valueerror_does_not_reconnect(self):
        """A ValueError from manager.state (spotapi's 'Could not get player state')
        below the escalation threshold must be treated like state=None: warn
        concisely and retry - no reconnect, no callback, no ERROR-level spam."""
        from Database.patches import STATE_FAILURE_RECONNECT_THRESHOLD

        failures = STATE_FAILURE_RECONNECT_THRESHOLD - 1
        manager = _ScriptedStateManager(
            [ValueError("Could not get player state")] * failures
        )

        with self.assertLogs("Database.patches", level="WARNING") as cm:
            callback = self._runUpdateLoopIterations(manager, failures)

        manager.reconnect.assert_not_called()
        callback.assert_not_called()
        self.assertTrue(all(record.levelname == "WARNING" for record in cm.records))
        self.assertEqual(len(cm.records), failures)

    def test_patched_update_loop_reconnects_after_consecutive_state_failures(self):
        """Once manager.state fails STATE_FAILURE_RECONNECT_THRESHOLD times in a
        row, the loop must escalate to exactly one manager.reconnect()."""
        from Database.patches import STATE_FAILURE_RECONNECT_THRESHOLD

        manager = _ScriptedStateManager(
            [ValueError("Could not get player state")] * STATE_FAILURE_RECONNECT_THRESHOLD
        )

        self._runUpdateLoopIterations(manager, STATE_FAILURE_RECONNECT_THRESHOLD)

        manager.reconnect.assert_called_once_with()

    def test_patched_update_loop_successful_poll_resets_failure_counter(self):
        """A successful state fetch between failure streaks must reset the
        consecutive-failure counter, so two sub-threshold streaks separated by a
        success never trigger a reconnect."""
        from Database.patches import STATE_FAILURE_RECONNECT_THRESHOLD

        streak = STATE_FAILURE_RECONNECT_THRESHOLD - 1
        results = (
            [ValueError("Could not get player state")] * streak
            + [makeIdleState()]
            + [ValueError("Could not get player state")] * streak
        )
        manager = _ScriptedStateManager(results)

        self._runUpdateLoopIterations(manager, len(results))

        manager.reconnect.assert_not_called()

    def test_player_status_renew_state_logs_error_detail_attribute(self):
        """spotapi's ParentException keeps the HTTP detail in .error, not in
        str(e) - the renew_state warning must surface it."""
        from spotapi.exceptions import WebSocketError
        from spotapi.status import PlayerStatus

        exc = WebSocketError("Could not connect device", error="429: rate limited")

        with patch("spotapi.websocket.WebsocketStreamer.__init__", return_value=None), \
             patch("spotapi.status.PlayerStatus.register_device"), \
             patch("spotapi.status.PlayerStatus.connect_device", side_effect=exc):
            lps = PlayerStatus(MagicMock())
            with self.assertLogs("Database.patches", level="WARNING") as cm:
                lps.renew_state()

        self.assertIsNone(lps._state)
        self.assertTrue(any("429: rate limited" in message for message in cm.output))

    def test_player_status_renew_state_handles_missing_keys_gracefully(self):
        """PlayerStatus.renew_state should not raise KeyError if connect_device returns a dict without devices or player_state."""
        from spotapi.status import PlayerStatus
        
        with patch("spotapi.websocket.WebsocketStreamer.__init__", return_value=None), \
             patch("spotapi.status.PlayerStatus.register_device"), \
             patch("spotapi.status.PlayerStatus.connect_device") as mock_connect:
            
            # Case 1: returns dict without player_state/devices
            mock_connect.return_value = {"something": "else"}
            lps = PlayerStatus(MagicMock())
            lps.renew_state()
            self.assertIsNone(lps._state)
            self.assertIsNone(lps._devices)
            
            # Case 2: returns None
            mock_connect.return_value = None
            lps = PlayerStatus(MagicMock())
            lps.renew_state()
            self.assertIsNone(lps._state)
            self.assertIsNone(lps._devices)
    def test_player_status_renew_state_deep_copies_player_state_to_prevent_mutation(self):
        """Regression: spotapi's Track.from_dict() mutates its input dict in-place,
        replacing metadata dict with a Metadata dataclass:
            data["metadata"] = Metadata.from_dict(metadata)
        The patched state property passes a deep copy of _state to
        PlayerState.from_dict, so _state itself is never mutated. Without this
        patch, _state["track"]["metadata"] becomes a Metadata object after the
        property is accessed, and a subsequent getConnectPlayerState() read then
        calls .get("title") on a Metadata object -> AttributeError."""
        from spotapi.status import PlayerStatus

        raw_track = {"uri": "spotify:track:abc", "uid": "u1",
                     "metadata": {"title": "Test", "artist_name": "Artist"}}
        raw_state = {"is_playing": True, "track": raw_track,
                     "timestamp": "0", "position_as_of_timestamp": "0", "duration": "0",
                     "is_paused": False}
        device_dump = {"player_state": raw_state, "devices": []}

        with patch("spotapi.websocket.WebsocketStreamer.__init__", return_value=None), \
             patch("spotapi.status.PlayerStatus.register_device"), \
             patch("spotapi.status.PlayerStatus.connect_device", return_value=device_dump):
            lps = PlayerStatus(MagicMock())
            lps.renew_state()

        # Access the patched state property - this calls PlayerState.from_dict
        # internally, which would mutate _state without our fix.
        with patch.object(lps, "renew_state"):  # skip renew inside the property
            lps.state  # exercises the patched property

        # _state["track"]["metadata"] must still be a plain dict.
        stored_meta = lps._state["track"]["metadata"]
        self.assertIsInstance(stored_meta, dict,
            f"_state was mutated by state property: metadata is "
            f"{type(stored_meta).__name__}, expected dict")
        self.assertEqual(stored_meta.get("title"), "Test")
        self.assertEqual(stored_meta.get("artist_name"), "Artist")


class TestSessionClosedDetection(unittest.TestCase):
    """_isSessionClosedError must recognize curl_cffi's dead-session state in a
    raw message, in spotapi's .error detail attribute, and through the
    __cause__/__context__ chain - and nothing else."""

    def test_detects_direct_message(self):
        from Database.patches import _isSessionClosedError
        self.assertTrue(_isSessionClosedError(
            RuntimeError("Session is closed, cannot send request.")))

    def test_detects_error_detail_attribute(self):
        """spotapi's RequestError keeps the underlying curl_cffi detail in
        .error, not in str(e)."""
        from Database.patches import _isSessionClosedError
        from spotapi.exceptions import RequestError
        exc = RequestError("Failed to complete request.",
                           error="Session is closed, cannot send request.")
        self.assertTrue(_isSessionClosedError(exc))

    def test_detects_chained_cause(self):
        from Database.patches import _isSessionClosedError
        try:
            try:
                raise RuntimeError("Session is closed, cannot send request.")
            except RuntimeError as inner:
                raise ValueError("outer wrapper") from inner
        except ValueError as outer:
            self.assertTrue(_isSessionClosedError(outer))

    def test_ignores_unrelated_errors_and_none(self):
        from Database.patches import _isSessionClosedError
        self.assertFalse(_isSessionClosedError(RuntimeError("429: rate limited")))
        self.assertFalse(_isSessionClosedError(None))

    def test_survives_self_referencing_chain(self):
        from Database.patches import _isSessionClosedError
        exc = RuntimeError("nope")
        exc.__context__ = exc  #< pathological cycle must not hang the check
        self.assertFalse(_isSessionClosedError(exc))


class TestUpdateLoopShutdown(unittest.TestCase):
    """The patched updateLoop must self-terminate when its transport is gone
    for good (deliberate close / closed HTTP session) instead of retrying
    forever - a leftover loop kept spamming reconnect errors every few seconds
    after Ctrl+C during the 2026-07-17 shutdown hang."""

    def _makeLpm(self, manager):
        from SpotipyFree.LastPlayed import LastPlayedManger
        with patch("SpotipyFree.LastPlayed.PlayerStatus"):
            lpm = LastPlayedManger(MagicMock())
        lpm.manager = manager
        lpm.run = True
        return lpm

    def test_exits_on_deliberate_close_without_polling(self):
        manager = _ScriptedStateManager([])  #< any state access would IndexError
        manager._deliberate_close = True
        lpm = self._makeLpm(manager)

        lpm.updateLoop(MagicMock(), refreshInterval=1)

        self.assertFalse(lpm.run)
        manager.reconnect.assert_not_called()

    def test_stops_when_reconnect_hits_closed_session(self):
        """Once the HTTP session is closed, reconnect can never succeed - the
        loop must stop itself instead of cycling warn/error forever."""
        from Database.patches import STATE_FAILURE_RECONNECT_THRESHOLD
        from spotapi.exceptions import RequestError

        manager = _ScriptedStateManager(
            [ValueError("Could not get player state")] * STATE_FAILURE_RECONNECT_THRESHOLD
        )
        manager.reconnect = MagicMock(side_effect=RequestError(
            "Failed to complete request.",
            error="Session is closed, cannot send request."))
        lpm = self._makeLpm(manager)

        with patch("time.sleep"):  #< the loop must terminate on its own
            lpm.updateLoop(MagicMock(), refreshInterval=1)

        self.assertFalse(lpm.run)
        manager.reconnect.assert_called_once_with()

    def test_keeps_retrying_on_other_reconnect_errors(self):
        """A non-closed-session reconnect failure keeps the retry loop alive -
        transient outages must still recover once Spotify is reachable again."""
        from Database.patches import STATE_FAILURE_RECONNECT_THRESHOLD

        from Database.rate_limit import SPOTIFY_LIMITER

        script = [ValueError("Could not get player state")] * (STATE_FAILURE_RECONNECT_THRESHOLD + 1)
        manager = _ScriptedStateManager(script)
        manager.reconnect = MagicMock(side_effect=RuntimeError("Spotify unreachable"))
        lpm = self._makeLpm(manager)

        sleepCount = [0]

        def mockSleep(_secs):
            sleepCount[0] += 1
            if sleepCount[0] >= len(script):
                lpm.run = False

        # Escalating to a reconnect also opens a process-wide backoff window
        # (see TestConnectStatePollLimiting), which would hold the poll after
        # it - correct in production, but here it would stop the loop before
        # it could prove it survived the failed reconnect. Grant every slot so
        # this test stays about the reconnect path alone.
        with patch.object(SPOTIFY_LIMITER, "acquire", return_value=True):
            with patch("time.sleep", side_effect=mockSleep):
                lpm.updateLoop(MagicMock(), refreshInterval=1)

        manager.reconnect.assert_called_once_with()
        self.assertEqual(manager._results, [])  #< loop survived past the failed reconnect


class TestIncompleteTrackInfo(unittest.TestCase):
    """spotapi's song_info can come back degraded in three different shapes, all
    seen in Database/Data/app.log over 2026-07-16. Each one killed the whole
    recently-played callback iteration, so the play was dropped entirely:

      data=None                 -> TypeError: 'NoneType' object is not subscriptable
      trackUnion without "uri"  -> KeyError: 'uri', raised deep inside
                                   SpotipyFree/Formatter.py where the track id
                                   is no longer in scope
      spotapi's own SongError   -> raised on the FIRST attempt, because the
                                   substring classifier saw neither "rate
                                   limit" nor "session" in it

    _get_track_info_with_retry now recognises the first two as one typed error
    at our own seam, retries it a bounded number of times, and Spotify.track()
    degrades to a fallback record only once those are exhausted - so the play
    survives either way. The third is a failed HTTP request (spotapi raises it
    only from `if resp.fail`), so it goes to the transient ladder that already
    existed for exactly that, matched by type rather than by message."""

    def setUp(self):
        """The incomplete-response retry sleeps between attempts; patch it away
        class-wide so no test in here pays real seconds, and so a test that
        forgets can't quietly add them."""
        sleepPatcher = patch("Database.patches.time.sleep")
        self.mockSleep = sleepPatcher.start()
        self.addCleanup(sleepPatcher.stop)

        # These tests are about the retry BUDGETS, but a "429 rate limit" in
        # their scripts now also opens a real process-wide backoff window (see
        # TestSharedLimiterWiring) that the next attempt would wait out. Worse,
        # the sleep patch above is the one the limiter's own wait uses, so it
        # would spin out the full timeout rather than sleep it. Grant every
        # slot here; the shared limiter has its own coverage elsewhere.
        acquirePatcher = patch.object(rateLimitModule.SPOTIFY_LIMITER, "acquire", return_value=True)
        acquirePatcher.start()
        self.addCleanup(acquirePatcher.stop)

    def _newSpotifyInstance(self):
        import SpotipyFree
        instance = SpotipyFree.Spotify.__new__(SpotipyFree.Spotify)
        instance.getIsrc = False
        return instance

    @patch("spotapi.Public")
    def test_none_data_raises_typed_error_naming_the_track(self, mock_public):
        from Database.patches import _get_track_info_with_retry, IncompleteTrackInfoError

        mock_public.song_info.return_value = {"data": None}

        with self.assertRaises(IncompleteTrackInfoError) as ctx:
            _get_track_info_with_retry("deadbeef")
        self.assertIn("deadbeef", str(ctx.exception))

    @patch("spotapi.Public")
    def test_none_trackUnion_raises_typed_error(self, mock_public):
        from Database.patches import _get_track_info_with_retry, IncompleteTrackInfoError

        mock_public.song_info.return_value = {"data": {"trackUnion": None}}

        with self.assertRaises(IncompleteTrackInfoError):
            _get_track_info_with_retry("deadbeef")

    @patch("spotapi.Public")
    def test_trackUnion_without_uri_raises_typed_error(self, mock_public):
        """The KeyError case: trackUnion IS a dict, so no null check catches it.
        Only a shape check does."""
        from Database.patches import _get_track_info_with_retry, IncompleteTrackInfoError

        union = fakeTrackUnion("abc123")
        del union["uri"]
        mock_public.song_info.return_value = {"data": {"trackUnion": union}}

        with self.assertRaises(IncompleteTrackInfoError):
            _get_track_info_with_retry("abc123")

    @patch("spotapi.Public")
    def test_blank_uri_is_also_incomplete(self, mock_public):
        from Database.patches import _get_track_info_with_retry, IncompleteTrackInfoError

        union = fakeTrackUnion("abc123")
        union["uri"] = ""
        mock_public.song_info.return_value = {"data": {"trackUnion": union}}

        with self.assertRaises(IncompleteTrackInfoError):
            _get_track_info_with_retry("abc123")

    @patch("spotapi.Public")
    def test_complete_response_is_returned_unchanged(self, mock_public):
        from Database.patches import _get_track_info_with_retry

        union = fakeTrackUnion("abc123")
        mock_public.song_info.return_value = {"data": {"trackUnion": union}}

        self.assertIs(_get_track_info_with_retry("abc123"), union)

    @patch("spotapi.Public")
    def test_incomplete_info_is_retried_before_giving_up(self, mock_public):
        """Originally this asserted the opposite - a degraded response was
        treated as a fact about the track, not a transient failure. The log
        says otherwise: all 11 incomplete responses in 11 days fall inside one
        4m47s window (2026-07-16 12:59:44-13:04:31), alongside 14 session and
        websocket failures. That's one degraded Spotify session, not 11
        undescribable tracks, so it belongs in the same transient class the
        retry ladder already exists for."""
        from Database.patches import (
            _get_track_info_with_retry, IncompleteTrackInfoError,
            INCOMPLETE_TRACK_INFO_RETRIES,
        )

        mock_public.song_info.return_value = {"data": None}

        with self.assertRaises(IncompleteTrackInfoError):
            _get_track_info_with_retry("abc123")

        self.assertEqual(mock_public.song_info.call_count, INCOMPLETE_TRACK_INFO_RETRIES + 1)

    @patch("spotapi.Public")
    def test_a_retry_that_succeeds_returns_the_track(self, mock_public):
        """The whole point: a track that was undescribable during the blip is
        described a moment later, and no fallback row is ever created."""
        from Database.patches import _get_track_info_with_retry

        union = fakeTrackUnion("abc123")
        mock_public.song_info.side_effect = [{"data": None}, {"data": {"trackUnion": union}}]

        self.assertIs(_get_track_info_with_retry("abc123"), union)

    @patch("spotapi.Public")
    def test_incomplete_retry_uses_a_short_fixed_delay(self, mock_public):
        """Not the transient ladder's 1/2/4s exponential backoff: this runs
        inside the poll loop's callback, and a burst means every affected track
        pays the wait."""
        from Database.patches import (
            _get_track_info_with_retry, IncompleteTrackInfoError,
            INCOMPLETE_TRACK_INFO_RETRY_DELAY_SECONDS,
        )

        mock_public.song_info.return_value = {"data": None}

        with self.assertRaises(IncompleteTrackInfoError):
            _get_track_info_with_retry("abc123")

        self.assertTrue(self.mockSleep.called)
        for call in self.mockSleep.call_args_list:
            self.assertEqual(call.args[0], INCOMPLETE_TRACK_INFO_RETRY_DELAY_SECONDS)

    @patch("spotapi.Public")
    def test_incomplete_retries_do_not_consume_the_transient_budget(self, mock_public):
        """The two failure modes get separate budgets - an incomplete response
        followed by a rate limit must still get the transient ladder's own
        attempts, or a blip early in a fetch would silently shorten the
        recovery window for a completely different problem."""
        from Database.patches import _get_track_info_with_retry

        union = fakeTrackUnion("abc123")
        mock_public.song_info.side_effect = [
            {"data": None},                       #< incomplete: costs an incomplete retry
            Exception("429 rate limit"),          #< transient: attempt 1
            Exception("429 rate limit"),          #< transient: attempt 2
            {"data": {"trackUnion": union}},      #< transient: attempt 3 succeeds
        ]

        self.assertIs(_get_track_info_with_retry("abc123"), union)

    @patch("spotapi.Public")
    def test_a_failed_request_is_retried_like_any_other_transport_blip(self, mock_public):
        """The third shape in this class's docstring. spotapi raises
        SongError("Could not get song info", error=...) from exactly one place:
        `if resp.fail` in Song.get_track_info - i.e. the HTTP request failed. It
        is a transport failure, but the substring classifier matched neither
        "rate limit" nor "session" on it, so it was re-raised on the FIRST
        attempt, propagated through _addToRecentlyPlayed, and killed the whole
        poll iteration. Five plays went that way in the audited window, and the
        retry ladder that exists for precisely this never ran."""
        from spotapi.exceptions import SongError
        from Database.patches import _get_track_info_with_retry

        union = fakeTrackUnion("abc123")
        mock_public.song_info.side_effect = [
            SongError("Could not get song info", error="502 Bad Gateway"),
            {"data": {"trackUnion": union}},
        ]

        self.assertIs(_get_track_info_with_retry("abc123"), union)

    @patch("spotapi.Public")
    def test_a_request_that_keeps_failing_still_raises(self, mock_public):
        """Retrying is the fix, not swallowing: a genuine, persistent failure
        must still reach the caller rather than silently becoming a fallback
        record that claims the track is undescribable."""
        from spotapi.exceptions import SongError
        from Database.patches import _get_track_info_with_retry

        mock_public.song_info.side_effect = SongError("Could not get song info", error="502")

        with self.assertRaises(SongError):
            _get_track_info_with_retry("abc123")

    @patch("spotapi.Public")
    def test_a_non_transport_spotapi_error_is_not_retried(self, mock_public):
        """The classification stays narrow - an unrecognised error is still a
        fact about the request, raised on the first attempt."""
        from Database.patches import _get_track_info_with_retry

        mock_public.song_info.side_effect = ValueError("something else entirely")

        with self.assertRaises(ValueError):
            _get_track_info_with_retry("abc123")
        self.assertEqual(mock_public.song_info.call_count, 1)

    @patch("spotapi.Public")
    def test_exhausted_incomplete_retries_still_reach_the_fallback(self, mock_public):
        """Retrying reduces how often a fallback is created; it must not remove
        the safety net when Spotify really has nothing."""
        mock_public.song_info.return_value = {"data": None}

        result = self._newSpotifyInstance().track("abc123")

        self.assertEqual(result["track_id"], "abc123")


class TestTrackFallbackOnIncompleteInfo(unittest.TestCase):
    """Spotify.track() degrades to a minimal fallback record rather than raising
    when Spotify can't describe a track. Raising propagates through
    SpotipyFree's _addToRecentlyPlayed and kills the poll iteration, losing a
    play that really happened - 11 plays went that way over 11 days.

    The record is marked RESTRICTED_FALLBACK_REASON (not SYNTHETIC): the track
    id is real, so the Spotify link is real too, and upsertTrack is explicitly
    built never to let a fallback overwrite metadata that arrives later."""

    def setUp(self):
        """The incomplete-response retry sleeps between attempts; patch it away
        class-wide so no test in here pays real seconds, and so a test that
        forgets can't quietly add them."""
        sleepPatcher = patch("Database.patches.time.sleep")
        self.mockSleep = sleepPatcher.start()
        self.addCleanup(sleepPatcher.stop)

        # These tests are about the retry BUDGETS, but a "429 rate limit" in
        # their scripts now also opens a real process-wide backoff window (see
        # TestSharedLimiterWiring) that the next attempt would wait out. Worse,
        # the sleep patch above is the one the limiter's own wait uses, so it
        # would spin out the full timeout rather than sleep it. Grant every
        # slot here; the shared limiter has its own coverage elsewhere.
        acquirePatcher = patch.object(rateLimitModule.SPOTIFY_LIMITER, "acquire", return_value=True)
        acquirePatcher.start()
        self.addCleanup(acquirePatcher.stop)

    def _newSpotifyInstance(self):
        import SpotipyFree
        instance = SpotipyFree.Spotify.__new__(SpotipyFree.Spotify)
        instance.getIsrc = False
        return instance

    def _track(self, mock_public, payload, trackId="abc123"):
        mock_public.song_info.return_value = payload
        return self._newSpotifyInstance().track(trackId)

    @patch("spotapi.Public")
    def test_incomplete_track_returns_fallback_instead_of_raising(self, mock_public):
        result = self._track(mock_public, {"data": None})

        self.assertEqual(result["track_id"], "abc123")
        self.assertEqual(result["id"], "abc123")

    @patch("spotapi.Public")
    def test_fallback_keeps_the_real_spotify_link(self, mock_public):
        """The id is real even when the metadata isn't, so the link must work -
        only fabricated ids get an empty url."""
        result = self._track(mock_public, {"data": {"trackUnion": None}})

        self.assertEqual(result["external_urls"]["spotify"],
                         "https://open.spotify.com/track/abc123")

    @patch("spotapi.Public")
    def test_fallback_is_marked_as_a_fallback(self, mock_public):
        """The marker is what stops the degraded row from being mistaken for
        real metadata, both in the UI badge and in upsertTrack's overwrite
        guard."""
        from Database.db import RESTRICTED_FALLBACK_REASON

        result = self._track(mock_public, {"data": {"trackUnion": None}})

        self.assertEqual(result["created_reason"], RESTRICTED_FALLBACK_REASON)

    @patch("spotapi.Public")
    def test_fallback_records_why_it_is_unplayable(self, mock_public):
        """playability is the field Client.formatTrack turns into
        tracks.availability_reason, which is what the UI badges on."""
        from Database.patches import TRACK_INFO_UNAVAILABLE_REASON

        result = self._track(mock_public, {"data": None})

        self.assertEqual(result["playability"]["playable"], False)
        self.assertEqual(result["playability"]["reason"], TRACK_INFO_UNAVAILABLE_REASON)

    @patch("spotapi.Public")
    def test_fallback_invents_no_facts(self, mock_public):
        """No fabricated duration or artists - a made-up number reads as real
        metadata downstream."""
        union = fakeTrackUnion("abc123")
        del union["uri"]

        result = self._track(mock_public, {"data": {"trackUnion": union}})

        self.assertEqual(result["duration_ms"], 0)
        self.assertEqual(result["artists"], [])

    @patch("spotapi.Public")
    def test_fallback_title_is_the_shared_placeholder_not_blank(self, mock_public):
        """A blank name rendered as an empty row in every list the track
        appeared in. The placeholder is safe because upsertTrack replaces a
        fallback row's name unconditionally once real metadata arrives, so it
        never blocks the repair - and it's the same constant Client.formatTrack
        defaults to, so the two can't drift."""
        from Database.db import UNKNOWN_TRACK_NAME
        from Database.Formatters.spotifyClient import Client

        result = self._track(mock_public, {"data": None})

        self.assertEqual(result["name"], UNKNOWN_TRACK_NAME)
        formatted = Client.formatTrack(result, timestamp=1000, msPlayed=5000)
        self.assertEqual(formatted["name"], UNKNOWN_TRACK_NAME)

    @patch("spotapi.Public")
    def test_fallback_album_name_is_the_placeholder_not_blank(self, mock_public):
        """The same reasoning as the title above, applied to the album the record
        has to invent - and UNKNOWN_ALBUM_NAME exists for exactly this ("Companion
        for the per-track album a fallback record has to invent", Database/db.py).

        The album dict passed "" and _formatAlbum reads .get("name", "Unknown
        album"), so the DEFAULT never applied - the key was present - and
        albums.name was stored empty: a blank album name on the song's detail page
        and in every album link. migrate1_43_0 already uses the constant for this
        identical placeholder shape."""
        from Database.db import UNKNOWN_ALBUM_NAME
        from Database.Formatters.spotifyClient import Client

        result = self._track(mock_public, {"data": None})

        self.assertEqual(result["album"]["name"], UNKNOWN_ALBUM_NAME)
        formatted = Client.formatTrack(result, timestamp=1000, msPlayed=5000)
        self.assertEqual(formatted["album"]["name"], UNKNOWN_ALBUM_NAME)

    @patch("spotapi.Public")
    def test_fallback_album_is_per_track_not_shared(self, mock_public):
        """Two unrelated incomplete tracks must not be merged under one
        "Unknown album" - a shared fabricated album id would link strangers'
        tracks together on the album page."""
        first = self._track(mock_public, {"data": None}, trackId="aaa")
        second = self._track(mock_public, {"data": None}, trackId="bbb")

        self.assertNotEqual(first["album"]["id"], second["album"]["id"])

    @patch("spotapi.Public")
    def test_fallback_survives_the_apps_own_formatter(self, mock_public):
        """The record's whole purpose is to reach the database, and it gets
        there through Client.formatTrack - which indexes track["id"] and
        track["external_urls"]["spotify"] directly."""
        from Database.Formatters.spotifyClient import Client
        from Database.db import RESTRICTED_FALLBACK_REASON

        raw = self._track(mock_public, {"data": None})
        formatted = Client.formatTrack(raw, timestamp=1000, msPlayed=5000)

        self.assertEqual(formatted["id"], "abc123")
        self.assertEqual(formatted["url"], "https://open.spotify.com/track/abc123")
        self.assertEqual(formatted["created_reason"], RESTRICTED_FALLBACK_REASON)
        self.assertEqual(formatted["availability_reason"], "TRACK_INFO_UNAVAILABLE")

    @patch("spotapi.Public")
    def test_fallback_is_logged_with_the_track_id(self, mock_public):
        with self.assertLogs("Database.patches", level="WARNING") as logCapture:
            self._track(mock_public, {"data": None})

        self.assertTrue(any("abc123" in m for m in logCapture.output))

    @patch("spotapi.Public")
    def test_real_transport_failures_still_raise(self, mock_public):
        """Only an incomplete *response* degrades. A genuine exception out of
        spotapi is still an error - recording a fallback for every network blip
        would poison the catalog with rows that look definitively unavailable."""
        mock_public.song_info.side_effect = RuntimeError("connection reset")

        with self.assertRaises(RuntimeError):
            self._newSpotifyInstance().track("abc123")

    @patch("spotapi.Public")
    def test_complete_track_is_unaffected(self, mock_public):
        result = self._track(mock_public, {"data": {"trackUnion": fakeTrackUnion("abc123")}})

        self.assertEqual(result["name"], "Song abc123")
        self.assertIsNone(result.get("created_reason"))


class TestSafeResponseHeaders(unittest.TestCase):
    """The spotapi.User diagnostics log response headers to identify rate
    limiting and Cloudflare blocks. Spotify's responses to those same calls
    carry live session credentials (__Host-sp_csrf_sid, x-csrf-token), so only
    an explicit allowlist may reach the log."""

    #< a realistic failing-response header set: the two credential headers seen
    #  verbatim in Database/Data/app.log, plus the diagnostics worth keeping
    SAMPLE_HEADERS = {
        "Set-Cookie": "__Host-sp_csrf_sid=be483bc22ea1d2dc9b5d0ece1ed4cd0f; Path=/; HttpOnly; Secure",
        "X-Csrf-Token": "013acda7194cb9484d5810d31fbe0b5d826f20a0683137",
        "Content-Type": "text/html; charset=utf-8",
        "Retry-After": "60",
        "X-RateLimit-Remaining": "0",
        "cf-ray": "9a1b2c3d4e5f6789-FRA",
        "Authorization": "Bearer supersecret",
    }

    def test_drops_credential_bearing_headers(self):
        from Database.patches import _safeResponseHeaders

        safe = _safeResponseHeaders(self.SAMPLE_HEADERS)

        self.assertNotIn("Set-Cookie", safe)
        self.assertNotIn("X-Csrf-Token", safe)
        self.assertNotIn("Authorization", safe)
        self.assertNotIn("be483bc22ea1d2dc9b5d0ece1ed4cd0f", str(safe))
        self.assertNotIn("013acda7194cb9484d5810d31fbe0b5d826f20a0683137", str(safe))

    def test_keeps_diagnostic_headers(self):
        """Whatever is dropped, the headers these log sites exist for must
        survive - otherwise the diagnostics are gone along with the leak."""
        from Database.patches import _safeResponseHeaders

        safe = _safeResponseHeaders(self.SAMPLE_HEADERS)

        self.assertEqual(safe["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(safe["Retry-After"], "60")
        self.assertEqual(safe["X-RateLimit-Remaining"], "0")
        self.assertEqual(safe["cf-ray"], "9a1b2c3d4e5f6789-FRA")

    def test_unknown_header_is_dropped_not_kept(self):
        """Allowlist, not denylist: a header nobody anticipated must default to
        dropped, so a future credential-bearing header can't leak before anyone
        notices it exists."""
        from Database.patches import _safeResponseHeaders

        self.assertEqual(_safeResponseHeaders({"X-Some-Future-Token": "secret"}), {})

    def test_tolerates_missing_or_unusable_headers(self):
        """These call sites run on a failing response - header access must never
        be the thing that raises inside an error path."""
        from Database.patches import _safeResponseHeaders

        self.assertEqual(_safeResponseHeaders(None), {})
        self.assertEqual(_safeResponseHeaders(object()), {})

    def test_get_user_info_failure_log_has_no_credentials(self):
        """End-to-end over the real patched method: the credential values must
        not appear anywhere in the emitted log record."""
        import spotapi.user
        from spotapi.exceptions import UserError

        mock_login = MagicMock()
        mock_login.logged_in = True
        user_inst = spotapi.user.User(mock_login)

        resp = MagicMock()
        resp.status_code = 429
        resp.fail = True
        resp.error.string = "Too Many Requests"
        resp.response = "Rate limit hit"
        resp.raw.headers = dict(self.SAMPLE_HEADERS)
        mock_login.client.get.return_value = resp

        with self.assertLogs("Database.patches", level="WARNING") as logCapture:
            with self.assertRaises(UserError):
                user_inst.get_user_info()

        emitted = "\n".join(logCapture.output)
        self.assertNotIn("__Host-sp_csrf_sid", emitted)
        self.assertNotIn("013acda7194cb9484d5810d31fbe0b5d826f20a0683137", emitted)
        self.assertIn("Retry-After", emitted)  #< the diagnostic still lands


class TestResponseBodyLogging(unittest.TestCase):
    """Spotify answers a rate-limited/bot-checked request to these endpoints
    with its full HTML fallback page, so the verbatim body used to put a
    kilobyte of markup and CSS into the log - three times per flake, once here
    and twice more when the listener re-logged the exception carrying it.
    Routine operation gets a description of the page; FLASK_DEBUG brings the
    raw snippet back."""

    _LOGGER = "Database.patches"

    #< the real fallback page from Database/Data/app.log, shortened but same shape
    FALLBACK_PAGE = (
        '<!DOCTYPE html><html><head><meta charSet="utf-8" data-next-head=""/>'
        '<title data-next-head="">Oh nein!</title>'
        '<style data-next-head="">body { background-color: #ff4834; }</style>'
        + ("<p>x</p>" * 300) + "</head></html>"
    )

    def _describe(self, response, maxLen=None):
        from Database.patches import _describeResponseBody, RESPONSE_SNIPPET_MAX_LEN
        return _describeResponseBody(response, RESPONSE_SNIPPET_MAX_LEN if maxLen is None else maxLen)

    def _userInstance(self, response, status=200, fail=False):
        import spotapi.user
        mockLogin = MagicMock()
        mockLogin.logged_in = True
        resp = MagicMock()
        resp.status_code = status
        resp.fail = fail
        resp.response = response
        resp.raw.headers = {}
        mockLogin.client.get.return_value = resp
        return spotapi.user.User(mockLogin)

    def test_missing_body_stays_missing(self):
        self.assertIsNone(self._describe(None))

    def test_html_page_collapses_to_type_size_and_title(self):
        """The <title> is what distinguishes Spotify's own fallback page from a
        Cloudflare challenge - the reason these log sites exist - so it is the
        one part of the markup worth keeping."""
        described = self._describe(self.FALLBACK_PAGE)

        self.assertNotIn("<!DOCTYPE", described)
        self.assertNotIn("background-color", described)
        self.assertIn("html page", described)
        self.assertIn(str(len(self.FALLBACK_PAGE)), described)
        self.assertIn("Oh nein!", described)

    def test_short_plain_body_is_kept_verbatim(self):
        """The useful case: a short API error message is the diagnostic itself."""
        self.assertEqual(self._describe("Rate limit hit"), "Rate limit hit")

    def test_long_plain_body_is_truncated_with_its_length(self):
        from Database.patches import RESPONSE_SUMMARY_MAX_LEN

        body = "e" * (RESPONSE_SUMMARY_MAX_LEN + 500)
        described = self._describe(body)

        self.assertLess(len(described), len(body))
        self.assertTrue(described.startswith("e" * RESPONSE_SUMMARY_MAX_LEN))
        self.assertIn(str(len(body)), described)

    def test_flask_debug_returns_the_raw_snippet(self):
        with patch("Database.patches._flaskDebugEnabled", return_value=True):
            described = self._describe(self.FALLBACK_PAGE)

        self.assertTrue(described.startswith("<!DOCTYPE"))

    def test_flask_debug_snippet_still_respects_the_length_cap(self):
        with patch("Database.patches._flaskDebugEnabled", return_value=True):
            described = self._describe(self.FALLBACK_PAGE, maxLen=50)

        self.assertEqual(described, self.FALLBACK_PAGE[:50])

    def test_get_user_info_keeps_markup_out_of_log_and_error(self):
        from spotapi.exceptions import UserError

        userInst = self._userInstance(self.FALLBACK_PAGE)

        with patch("Database.patches._flaskDebugEnabled", return_value=False):
            with self.assertLogs(self._LOGGER, level="WARNING") as logCapture:
                with self.assertRaises(UserError) as errCtx:
                    userInst.get_user_info()

        emitted = "\n".join(logCapture.output)
        self.assertNotIn("<!DOCTYPE", emitted)
        self.assertNotIn("background-color", emitted)
        self.assertIn("non-Mapping response", emitted)
        self.assertIn("html page", emitted)          #< the flake is still reported...
        self.assertIn("status=200", emitted)         #< ...with the diagnostics that identify it

        message = str(errCtx.exception)
        self.assertNotIn("<!DOCTYPE", message)
        self.assertIn("Invalid JSON", message)
        self.assertIn("Status: 200", message)
        self.assertIn("Type: str", message)

    def test_get_user_info_error_still_classifies_as_transient(self):
        """The listener buckets this exception by substring ("json"), and that
        bucket is what triggers the rate-limit backoff instead of a reconnect -
        shortening the message must not change the classification."""
        from spotapi.exceptions import UserError
        from Database.Listeners.spotifyListener import _is_rate_limit_error, _is_auth_error

        userInst = self._userInstance(self.FALLBACK_PAGE)

        with patch("Database.patches._flaskDebugEnabled", return_value=False):
            with self.assertLogs(self._LOGGER, level="WARNING"):
                with self.assertRaises(UserError) as errCtx:
                    userInst.get_user_info()

        self.assertTrue(_is_rate_limit_error(errCtx.exception))
        self.assertFalse(_is_auth_error(errCtx.exception))

    def test_get_user_info_with_flask_debug_logs_the_body(self):
        from spotapi.exceptions import UserError

        userInst = self._userInstance(self.FALLBACK_PAGE)

        with patch("Database.patches._flaskDebugEnabled", return_value=True):
            with self.assertLogs(self._LOGGER, level="WARNING") as logCapture:
                with self.assertRaises(UserError):
                    userInst.get_user_info()

        self.assertIn("<!DOCTYPE", "\n".join(logCapture.output))

    def test_failed_request_log_omits_markup_too(self):
        """The resp.fail path dumps the same body from the same endpoint."""
        from spotapi.exceptions import UserError

        userInst = self._userInstance(self.FALLBACK_PAGE, status=429, fail=True)

        with patch("Database.patches._flaskDebugEnabled", return_value=False):
            with self.assertLogs(self._LOGGER, level="WARNING") as logCapture:
                with self.assertRaises(UserError):
                    userInst.get_user_info()

        emitted = "\n".join(logCapture.output)
        self.assertNotIn("<!DOCTYPE", emitted)
        self.assertIn("HTTP request failed", emitted)

    def test_get_plan_info_keeps_markup_out_of_log_and_error(self):
        from spotapi.exceptions import UserError

        userInst = self._userInstance(self.FALLBACK_PAGE)

        with patch("Database.patches._flaskDebugEnabled", return_value=False):
            with self.assertLogs(self._LOGGER, level="WARNING") as logCapture:
                with self.assertRaises(UserError) as errCtx:
                    userInst.get_plan_info()

        self.assertNotIn("<!DOCTYPE", "\n".join(logCapture.output))
        self.assertNotIn("<!DOCTYPE", str(errCtx.exception))
        self.assertIn("Invalid JSON", str(errCtx.exception))


class TestFlaskDebugEnabled(unittest.TestCase):
    """Same switch (and same accepted values) as the rest of the app's verbose
    diagnostics - a body dump must not turn on for FLASK_DEBUG=0."""

    def _enabled(self, value):
        import os
        from Database.patches import _flaskDebugEnabled
        if value is None:
            env = {k: v for k, v in os.environ.items() if k != "FLASK_DEBUG"}
            with patch.dict(os.environ, env, clear=True):
                return _flaskDebugEnabled()
        with patch.dict(os.environ, {"FLASK_DEBUG": value}):
            return _flaskDebugEnabled()

    def test_truthy_values_enable(self):
        self.assertTrue(self._enabled("1"))
        self.assertTrue(self._enabled("true"))
        self.assertTrue(self._enabled("TRUE"))

    def test_falsy_and_unset_values_disable(self):
        self.assertFalse(self._enabled("0"))
        self.assertFalse(self._enabled(""))
        self.assertFalse(self._enabled(None))


class _FakeWebsocket:
    """Stands in for the sync websocket client: each recv() consumes the next
    scripted result - Exception instances are raised, anything else returned.
    Records the timeout it was called with, which is the whole point of the
    patch under test."""

    def __init__(self, results):
        self._results = list(results)
        self.recvTimeouts = []

    def recv(self, timeout=None, decode=None):
        self.recvTimeouts.append(timeout)
        if not self._results:
            raise TimeoutError("no more scripted results")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _fakeStreamer(results, deliberateClose=False):
    """A stand-in WebsocketStreamer carrying only what get_packet touches."""
    import types
    streamer = types.SimpleNamespace()
    streamer.ws = _FakeWebsocket(results)
    streamer.rlock = threading.Lock()
    streamer.ws_dump = None
    streamer._deliberate_close = deliberateClose
    streamer.reconnect = MagicMock()
    return streamer


def _getPacket(streamer, **kwargs):
    import spotapi.websocket
    return spotapi.websocket.WebsocketStreamer.get_packet(streamer, **kwargs)


class TestPatchedGetPacket(unittest.TestCase):
    """spotapi's own get_packet holds rlock across an UNBOUNDED ws.recv(), and
    keep_alive needs that same lock to send its 60s ping. On a quiet account
    the receive loop parks in recv() holding the lock, the ping never goes out,
    and Spotify drops the connection on its idle timeout - recovered by a full
    re-login, the most bot-detection-sensitive path there is.

    That is why LastPlayedManger polls instead of listening, and why adopting
    spotapi's EventManager as-is would be worse than the polling it replaces.
    Phase 0 measured the fix working: 601/601 keepalive pongs over 10 hours
    (see eventDrivenConnectStatePlan.md).

    Its bare `except Exception` is the second half of the problem: it treats a
    plain timeout - the NORMAL state of a push channel - as a reason to
    reconnect, and retries forever with a 1s sleep."""

    def test_a_frame_is_returned_as_a_dict_and_cached(self):
        streamer = _fakeStreamer(['{"type":"pong"}'])

        packet = _getPacket(streamer)

        self.assertEqual(packet, {"type": "pong"})
        self.assertEqual(streamer.ws_dump, {"type": "pong"})

    def test_the_recv_wait_is_bounded(self):
        """The starvation fix: the lock may only be held for a bounded recv,
        never an open-ended one. An unbounded wait is what stops the ping."""
        from Database.patches import WS_RECV_TIMEOUT_SECONDS

        streamer = _fakeStreamer(['{"type":"pong"}'])
        _getPacket(streamer)

        self.assertEqual(streamer.ws.recvTimeouts, [WS_RECV_TIMEOUT_SECONDS])

    def test_the_lock_is_released_before_returning(self):
        streamer = _fakeStreamer(['{"type":"pong"}'])
        _getPacket(streamer)

        self.assertTrue(streamer.rlock.acquire(blocking=False))
        streamer.rlock.release()

    def test_a_timeout_returns_none_without_reconnecting(self):
        """Silence is the normal state of a push channel - Phase 0 saw 3.75 h
        between state pushes. Reconnecting on it would turn every idle period
        into a re-login."""
        streamer = _fakeStreamer([TimeoutError("nothing pushed")])

        self.assertIsNone(_getPacket(streamer))
        streamer.reconnect.assert_not_called()

    def test_a_deliberate_close_returns_without_touching_the_socket(self):
        streamer = _fakeStreamer(['{"type":"pong"}'], deliberateClose=True)

        self.assertIsNone(_getPacket(streamer))
        self.assertEqual(streamer.ws.recvTimeouts, [])
        streamer.reconnect.assert_not_called()

    def test_a_missing_socket_returns_none_rather_than_raising(self):
        streamer = _fakeStreamer([])
        streamer.ws = None

        self.assertIsNone(_getPacket(streamer))
        streamer.reconnect.assert_not_called()

    def test_a_clean_close_does_not_reconnect(self):
        import websockets.exceptions

        streamer = _fakeStreamer([
            websockets.exceptions.ConnectionClosedOK(None, None)])

        self.assertIsNone(_getPacket(streamer))
        streamer.reconnect.assert_not_called()

    def test_an_unexpected_drop_reconnects_once(self):
        import websockets.exceptions

        streamer = _fakeStreamer([
            websockets.exceptions.ConnectionClosedError(None, None)])

        with self.assertLogs("Database.patches", level="WARNING"):
            self.assertIsNone(_getPacket(streamer))

        streamer.reconnect.assert_called_once_with()

    def test_reconnect_failures_are_bounded(self):
        """spotapi retries forever on a 1s sleep. A dead endpoint must not be
        hammered - the stale-feed detector rebuilds the session instead."""
        import websockets.exceptions
        from Database.patches import WS_RECV_MAX_RECONNECT_FAILURES

        drops = [websockets.exceptions.ConnectionClosedError(None, None)
                 for _ in range(WS_RECV_MAX_RECONNECT_FAILURES + 3)]
        streamer = _fakeStreamer(drops)
        streamer.reconnect = MagicMock(side_effect=RuntimeError("endpoint down"))

        with self.assertLogs("Database.patches", level="WARNING"):
            with patch("Database.patches.time.sleep"):
                for _ in range(WS_RECV_MAX_RECONNECT_FAILURES + 3):
                    _getPacket(streamer)

        self.assertLessEqual(streamer.reconnect.call_count, WS_RECV_MAX_RECONNECT_FAILURES)

    def test_a_closed_session_gives_up_immediately(self):
        """curl_cffi's closed session can never be revived - see
        _isSessionClosedError. One attempt, then stop."""
        import websockets.exceptions
        from spotapi.exceptions import RequestError

        streamer = _fakeStreamer([
            websockets.exceptions.ConnectionClosedError(None, None),
            websockets.exceptions.ConnectionClosedError(None, None)])
        streamer.reconnect = MagicMock(side_effect=RequestError(
            "Failed to complete request.", error="Session is closed, cannot send request."))

        with self.assertLogs("Database.patches", level="ERROR"):
            _getPacket(streamer)
            _getPacket(streamer)

        streamer.reconnect.assert_called_once_with()

    def test_an_unparsable_frame_is_dropped_not_reconnected(self):
        """A malformed frame says nothing about the connection's health."""
        streamer = _fakeStreamer(["<html>nope</html>"])

        with self.assertLogs("Database.patches", level="WARNING"):
            self.assertIsNone(_getPacket(streamer))

        streamer.reconnect.assert_not_called()

    def test_a_non_object_frame_is_dropped(self):
        """dict(json.loads(...)) raises on a JSON array or scalar - spotapi's
        version let that reach its reconnect-on-anything handler."""
        streamer = _fakeStreamer(["[1, 2, 3]"])

        with self.assertLogs("Database.patches", level="WARNING"):
            self.assertIsNone(_getPacket(streamer))

        streamer.reconnect.assert_not_called()

    def test_the_caller_can_override_the_timeout(self):
        streamer = _fakeStreamer(['{"type":"pong"}'])

        _getPacket(streamer, timeout=0.25)

        self.assertEqual(streamer.ws.recvTimeouts, [0.25])

    def test_a_slotted_instance_says_so_instead_of_retrying_forever(self):
        """WebsocketStreamer declares __slots__ without the failure counter.
        Every instance this app builds is a PlayerStatus, which has a __dict__ -
        but if that ever changed, losing the ceiling silently would restore
        exactly the reconnect storm this patch exists to prevent."""
        import websockets.exceptions
        from Database.patches import _setRecvReconnectFailures

        class Slotted:
            __slots__ = ()

        with self.assertLogs("Database.patches", level="WARNING") as logCapture:
            _setRecvReconnectFailures(Slotted(), 1)

        self.assertIn("__slots__", "\n".join(logCapture.output))

    def test_a_successful_reconnect_clears_the_failure_count(self):
        import websockets.exceptions

        streamer = _fakeStreamer([
            websockets.exceptions.ConnectionClosedError(None, None),
            websockets.exceptions.ConnectionClosedError(None, None)])
        streamer.reconnect = MagicMock(side_effect=[RuntimeError("blip"), None])

        with self.assertLogs("Database.patches", level="WARNING"):
            _getPacket(streamer)
            _getPacket(streamer)

        self.assertEqual(streamer._recvReconnectFailures, 0)

    def test_event_manager_still_gets_the_shape_it_expects(self):
        """EventManager._listen does `if event is None or event.get("payloads")
        is None: continue` - returning None on a quiet socket is exactly what
        that already handles, so the existing caller stays correct."""
        streamer = _fakeStreamer([TimeoutError()])
        event = _getPacket(streamer)

        self.assertIsNone(event)   #< _listen's own guard covers this


def pushedCluster(trackUri="spotify:track:aaa", uid="uid-1", timestampMs="1785367061986",
                  contextUri="spotify:playlist:ctx", isPaused=False):
    """A connect-state cluster in the exact shape the dealer pushes - verified
    against the raw frames Phase 0 captured (payloads[].cluster.player_state,
    same keys connect_device's reply carries)."""
    return {
        "timestamp": timestampMs,
        "active_device_id": "device-1",
        "devices": {"device-1": {"name": "laptop"}},
        "player_state": {
            "timestamp": timestampMs,
            "context_uri": contextUri,
            "is_playing": True,
            "is_paused": isPaused,
            "duration": "200000",
            "position_as_of_timestamp": "1000",
            "prev_tracks": [],
            "next_tracks": [],
            "track": {"uri": trackUri, "uid": uid, "metadata": {"title": "Song"}},
        },
    }


def pushFrame(cluster, updateReason="DEVICE_STATE_CHANGED"):
    return {"headers": {}, "type": "message", "uri": "hm://connect-state/v1/cluster",
            "payloads": [{"update_reason": updateReason, "cluster": cluster}]}


class _PushManager:
    """Stands in for PlayerStatus during a push loop: hands out scripted frames
    from get_packet and records connect_device calls.

    Starts with EMPTY caches on purpose. spotapi's connect_device() only returns
    the cluster - it never writes _state/_device_dump (renew_state is what
    normally does that), so a double that pre-filled them hid the fact that the
    push loop began with no state at all."""

    def __init__(self, frames, initialCluster=None):
        self._frames = list(frames)
        self._deliberate_close = False
        self._device_dump = None
        self._state = None
        self._devices = None
        self.cluster = initialCluster      #< what connect_device replies with
        self.connectCalls = 0
        self.connectError = None
        self.onExhausted = None

    def connect_device(self):
        self.connectCalls += 1
        if self.connectError is not None:
            raise self.connectError
        return self.cluster

    def get_packet(self, timeout=None):
        if self._frames:
            return self._frames.pop(0)
        if self.onExhausted is not None:
            self.onExhausted()
        return None


def _pushLastPlayed(manager):
    from SpotipyFree.LastPlayed import LastPlayedManger
    with patch("SpotipyFree.LastPlayed.PlayerStatus"):
        lpm = LastPlayedManger(MagicMock())
    lpm.manager = manager
    lpm.run = True
    return lpm


class TestPushedClusterHandling(unittest.TestCase):
    """A dealer frame carries more than playback - keepalive pongs and
    social-connect broadcasts share the socket. Identifying a connect-state
    frame by its CONTENT (a cluster with a player_state) rather than by its uri
    string means an upstream rename degrades to 'ignored', not to a listener
    that silently records nothing."""

    def test_a_connect_state_frame_yields_its_cluster(self):
        from Database.patches import _clusterFromPacket

        cluster = pushedCluster()
        self.assertIs(_clusterFromPacket(pushFrame(cluster)), cluster)

    def test_a_keepalive_pong_is_not_a_cluster(self):
        from Database.patches import _clusterFromPacket

        self.assertIsNone(_clusterFromPacket({"type": "pong"}))

    def test_a_social_connect_broadcast_is_not_a_cluster(self):
        """Seen live in Phase 0: uri social-connect/v2/broadcast_status_update,
        payloads present, no cluster."""
        from Database.patches import _clusterFromPacket

        frame = {"type": "message", "uri": "social-connect/v2/broadcast_status_update",
                 "payloads": [{"deviceBroadcastStatus": {}}]}
        self.assertIsNone(_clusterFromPacket(frame))

    def test_a_cluster_without_a_player_state_is_ignored(self):
        from Database.patches import _clusterFromPacket

        frame = {"payloads": [{"cluster": {"devices": {}}}]}
        self.assertIsNone(_clusterFromPacket(frame))

    def test_adopting_a_cluster_fills_the_same_caches_renew_state_does(self):
        """getConnectPlayerState (Now Playing, the missed-track cross-check)
        reads _state, and device_ids reads _device_dump - a push has to leave
        all of them exactly as a connect_device reply would."""
        from Database.patches import _adoptCluster

        manager = _PushManager([])
        cluster = pushedCluster()

        self.assertTrue(_adoptCluster(manager, cluster))
        self.assertIs(manager._device_dump, cluster)
        self.assertIs(manager._state, cluster["player_state"])
        self.assertEqual(manager._devices, cluster["devices"])


class TestPlayedDuration(unittest.TestCase):
    """time_played used to be wall-clock since the track became current, which
    counts pause time. app.log 2026-07-30 10:14:59 recorded a track paused
    overnight as 35,377,992ms - 154x its length - and only the corruption guard
    stopped it landing as a full play. Worse: a track paused 30 seconds in and
    abandoned got clamped to its duration and counted as a COMPLETE listen,
    which is also what decides plays.is_skip.

    This is on the shared path, so it applies to polling as well as push."""

    def _state(self, uid="uid-1", positionMs="30000", timestampMs="1785000000000",
               durationMs="200000", isPaused=False, trackUri="spotify:track:aaa"):
        cluster = pushedCluster(trackUri=trackUri, uid=uid, timestampMs=timestampMs,
                                isPaused=isPaused)
        cluster["player_state"]["position_as_of_timestamp"] = positionMs
        cluster["player_state"]["duration"] = durationMs
        return spotapi.status.PlayerState.from_dict(cluster["player_state"])

    def _observe(self, lpm, state, callback, nowEpoch):
        from Database.patches import _applyStateToTracking
        with patch("Database.patches.time.time", return_value=nowEpoch):
            _applyStateToTracking(lpm, state, callback)

    def test_a_paused_track_records_its_paused_position(self):
        """The regression: paused 30s in at 1785000000, changed 5 hours later.
        The old code recorded 5 hours."""
        lpm = _pushLastPlayed(_PushManager([]))
        callback = MagicMock()

        self._observe(lpm, self._state(uid="uid-1", positionMs="30000", isPaused=True),
                      callback, nowEpoch=1785000000)
        self._observe(lpm, self._state(uid="uid-2", trackUri="spotify:track:bbb"),
                      callback, nowEpoch=1785000000 + 5 * 3600)

        callback.assert_called_once()
        self.assertEqual(callback.call_args[0][3], 30000)

    def test_a_playing_track_counts_position_plus_elapsed(self):
        lpm = _pushLastPlayed(_PushManager([]))
        callback = MagicMock()

        #< snapshot says 30s in at t=1785000000; 10s later it is 40s in
        self._observe(lpm, self._state(uid="uid-1", positionMs="30000",
                                       timestampMs="1785000000000"),
                      callback, nowEpoch=1785000000)
        self._observe(lpm, self._state(uid="uid-2", trackUri="spotify:track:bbb"),
                      callback, nowEpoch=1785000010)

        self.assertEqual(callback.call_args[0][3], 40000)

    def test_it_never_exceeds_the_track_duration(self):
        lpm = _pushLastPlayed(_PushManager([]))
        callback = MagicMock()

        self._observe(lpm, self._state(uid="uid-1", positionMs="30000", durationMs="200000"),
                      callback, nowEpoch=1785000000)
        self._observe(lpm, self._state(uid="uid-2", trackUri="spotify:track:bbb"),
                      callback, nowEpoch=1785000000 + 3600)

        self.assertLessEqual(callback.call_args[0][3], 200000)

    def test_joining_mid_track_counts_where_playback_actually_was(self):
        """A listener joins mid-track on EVERY rebuild, and lastPlayedAt is then
        its first sighting, not the real start. Measuring from that sighting -
        which is what capping at wall-clock would do - reports 10s for a track
        already 3 minutes in, and a full listen looks like a skip."""
        lpm = _pushLastPlayed(_PushManager([]))
        callback = MagicMock()

        self._observe(lpm, self._state(uid="uid-1", positionMs="180000", isPaused=True),
                      callback, nowEpoch=1785000000)
        self._observe(lpm, self._state(uid="uid-2", trackUri="spotify:track:bbb"),
                      callback, nowEpoch=1785000010)

        self.assertEqual(callback.call_args[0][3], 180000)

    def test_a_state_without_a_position_falls_back_to_wall_clock(self):
        """Old behaviour, kept for anything that doesn't carry the fields."""
        from Database.patches import _playedMsForOutgoingTrack
        import datetime as dt

        lpm = _pushLastPlayed(_PushManager([]))
        lpm.lastPlayedAt = dt.datetime.fromtimestamp(1785000000, tz=dt.timezone.utc)

        with patch("Database.patches.time.time", return_value=1785000060):
            self.assertEqual(_playedMsForOutgoingTrack(lpm, None), 60000)

    def test_the_observation_is_refreshed_without_a_track_change(self):
        """A mid-track pause is exactly the update that makes the next
        measurement accurate, so it must be recorded even though nothing
        changed tracks."""
        from Database.patches import _applyStateToTracking

        lpm = _pushLastPlayed(_PushManager([]))
        callback = MagicMock()

        self._observe(lpm, self._state(uid="uid-1", positionMs="1000"), callback,
                      nowEpoch=1785000000)
        self._observe(lpm, self._state(uid="uid-1", positionMs="95000", isPaused=True),
                      callback, nowEpoch=1785000094)

        callback.assert_not_called()
        self.assertEqual(lpm._lastObservedPlayback[0], 95000)
        self.assertTrue(lpm._lastObservedPlayback[2])


class TestPushLoop(unittest.TestCase):
    """The push loop must record plays through exactly the same code the poll
    loop uses (_applyStateToTracking), and must hand back to polling rather
    than ever record nothing silently."""

    def _run(self, manager, lpm=None):
        from Database.patches import _runPushLoop

        lpm = lpm or _pushLastPlayed(manager)
        callback = MagicMock()
        manager.onExhausted = lambda: setattr(lpm, "run", False)
        return _runPushLoop(lpm, callback), callback, lpm

    def test_a_pushed_track_change_records_the_previous_track_once(self):
        first, second = pushedCluster(uid="uid-1"), pushedCluster(
            trackUri="spotify:track:bbb", uid="uid-2")
        manager = _PushManager([pushFrame(first), pushFrame(second)], initialCluster=first)

        outcome, callback, _ = self._run(manager)

        self.assertEqual(outcome, "stopped")
        callback.assert_called_once()
        self.assertEqual(callback.call_args[0][0], "spotify:track:aaa")   #< the PREVIOUS track

    def test_a_repeated_push_for_the_same_track_records_nothing(self):
        cluster = pushedCluster(uid="uid-1")
        manager = _PushManager([pushFrame(cluster), pushFrame(cluster), pushFrame(cluster)],
                               initialCluster=cluster)

        _, callback, _ = self._run(manager)

        callback.assert_not_called()

    def test_a_pause_push_is_not_a_track_change(self):
        playing = pushedCluster(uid="uid-1")
        paused = pushedCluster(uid="uid-1", isPaused=True)
        manager = _PushManager([pushFrame(playing), pushFrame(paused)], initialCluster=playing)

        _, callback, _ = self._run(manager)

        callback.assert_not_called()

    def test_the_pushed_state_reaches_getConnectPlayerState(self):
        """Now Playing and the missed-track cross-check read manager._state
        directly - if push didn't keep it current they would freeze."""
        first = pushedCluster(uid="uid-1")
        second = pushedCluster(trackUri="spotify:track:bbb", uid="uid-2")
        manager = _PushManager([pushFrame(second)], initialCluster=first)

        self._run(manager)

        self.assertEqual(manager._state["track"]["uri"], "spotify:track:bbb")

    def test_a_deliberate_close_stops_without_falling_back(self):
        manager = _PushManager([], initialCluster=pushedCluster())
        lpm = _pushLastPlayed(manager)
        manager._deliberate_close = True

        outcome, callback, _ = self._run(manager, lpm=lpm)

        self.assertEqual(outcome, "stopped")
        callback.assert_not_called()

    def test_total_frame_silence_falls_back_to_polling(self):
        """Keyed on ANY frame, pongs included: genuine state silence ran to
        3.75 h in Phase 0, so only a socket with no traffic at all is dead."""
        from Database.patches import PUSH_FRAME_SILENCE_FALLBACK_SECONDS

        manager = _PushManager([], initialCluster=pushedCluster())
        manager.onExhausted = None   #< never stop; the watchdog must be what ends it
        lpm = _pushLastPlayed(manager)
        clock = [1000.0]

        def advancingMonotonic():
            clock[0] += PUSH_FRAME_SILENCE_FALLBACK_SECONDS / 2
            return clock[0]

        from Database.patches import _runPushLoop
        with patch("Database.patches.time.monotonic", side_effect=advancingMonotonic):
            with self.assertLogs("Database.patches", level="WARNING"):
                outcome = _runPushLoop(lpm, MagicMock())

        self.assertEqual(outcome, "fallback")

    def test_a_keepalive_pong_keeps_the_channel_alive(self):
        """A pong carries no state but proves the socket works, so it must
        reset the watchdog. Phase 0 saw 3.75 h between state pushes against a
        pong every 60s - without this an idle account would flap back to
        polling every few minutes.

        The clock is driven past the fallback threshold deliberately: a test
        that merely feeds two pongs in real time passes whether or not the
        reset happens."""
        from Database.patches import PUSH_FRAME_SILENCE_FALLBACK_SECONDS, _runPushLoop

        pongs = 4
        manager = _PushManager([{"type": "pong"} for _ in range(pongs)],
                               initialCluster=pushedCluster())
        lpm = _pushLastPlayed(manager)
        manager.onExhausted = lambda: setattr(lpm, "run", False)

        clock = [1000.0]

        def steppingMonotonic():
            #< each pass advances half the threshold, so two unreset passes trip it
            clock[0] += PUSH_FRAME_SILENCE_FALLBACK_SECONDS / 2
            return clock[0]

        callback = MagicMock()
        with patch("Database.patches.time.monotonic", side_effect=steppingMonotonic):
            outcome = _runPushLoop(lpm, callback)

        self.assertEqual(outcome, "stopped")   #< never fell back despite the elapsed time
        callback.assert_not_called()

    def test_the_subscribe_reply_seeds_the_state(self):
        """connect_device() returns the cluster but never writes the caches -
        renew_state is what normally does that, and push mode does not call it.
        Without adopting the reply the loop would start blind, and the first
        real push would look like a track change from nothing."""
        manager = _PushManager([], initialCluster=pushedCluster(uid="uid-1"))

        self._run(manager)

        self.assertEqual(manager._state["track"]["uid"], "uid-1")
        self.assertIsInstance(manager._device_dump, dict)

    def test_a_first_push_after_seeding_is_not_a_phantom_change(self):
        """Seeded with uid-1, pushed uid-1: nothing played, nothing recorded."""
        cluster = pushedCluster(uid="uid-1")
        manager = _PushManager([pushFrame(cluster)], initialCluster=cluster)

        _, callback, _ = self._run(manager)

        callback.assert_not_called()

    def test_a_periodic_resubscribe_refreshes_the_state(self):
        """The floor under push mode: if the subscription silently stops
        delivering while the socket stays healthy, pongs keep the watchdog
        quiet and _state would freeze forever. Re-subscribing re-reads it, and
        a change that happened meanwhile is recovered rather than lost."""
        from Database.patches import CONNECT_STATE_RESUBSCRIBE_SECONDS, _runPushLoop

        # Pongs throughout: the socket is healthy, so the frame watchdog stays
        # quiet - which is exactly the failure it cannot see, and why the
        # re-subscribe has to be the probe.
        manager = _PushManager([{"type": "pong"}] * 10,
                               initialCluster=pushedCluster(uid="uid-1"))
        lpm = _pushLastPlayed(manager)
        manager.onExhausted = lambda: setattr(lpm, "run", False)

        clock = [1000.0]
        step = CONNECT_STATE_RESUBSCRIBE_SECONDS / 2

        def steppingMonotonic():
            clock[0] += step
            return clock[0]

        original = manager.connect_device

        def connectThenAdvanceTheTrack():
            result = original()
            #< after the seeding call, Spotify reports a different track: the
            #  change that arrived while pushes were silently dead
            manager.cluster = pushedCluster(trackUri="spotify:track:bbb", uid="uid-2")
            if manager.connectCalls >= 2:
                lpm.run = False
            return result
        manager.connect_device = connectThenAdvanceTheTrack

        callback = MagicMock()
        with patch("Database.patches.time.monotonic", side_effect=steppingMonotonic):
            _runPushLoop(lpm, callback)

        self.assertGreaterEqual(manager.connectCalls, 2)     #< it really re-subscribed
        callback.assert_called_once()                         #< and caught the missed change
        self.assertEqual(callback.call_args[0][0], "spotify:track:aaa")

    def test_the_initial_subscribe_takes_a_limiter_slot(self):
        from Database.rate_limit import SPOTIFY_LIMITER

        manager = _PushManager([], initialCluster=pushedCluster())
        with patch.object(SPOTIFY_LIMITER, "acquire", return_value=True) as acquire:
            self._run(manager)

        acquire.assert_called()
        self.assertEqual(manager.connectCalls, 1)

    def test_a_failed_initial_subscribe_falls_back(self):
        manager = _PushManager([], initialCluster=pushedCluster())
        manager.connectError = RuntimeError("connect-state refused")

        with self.assertLogs("Database.patches", level="WARNING"):
            outcome, callback, _ = self._run(manager)

        self.assertEqual(outcome, "fallback")

    def test_a_locally_paused_slot_is_not_a_subscribe_failure(self):
        """Same conflation that turned one rate-limit event into one per
        listener: our own backoff window is not evidence about Spotify, and
        must not push us off the push channel."""
        from Database.rate_limit import SPOTIFY_LIMITER

        manager = _PushManager([], initialCluster=pushedCluster())
        lpm = _pushLastPlayed(manager)
        with patch.object(SPOTIFY_LIMITER, "acquire", side_effect=[False, False, True]):
            outcome, _, _ = self._run(manager, lpm=lpm)

        self.assertEqual(outcome, "stopped")     #< kept waiting, never fell back
        self.assertEqual(manager.connectCalls, 1)


class TestUpdateLoopModeSelection(unittest.TestCase):
    """The toggle is read once per loop entry, and anything unreadable keeps
    polling - a settings lookup must never be what changes how plays are
    recorded."""

    def tearDown(self):
        from Database.patches import setPushListenerEnabledHook
        setPushListenerEnabledHook(None)

    def test_no_hook_means_polling(self):
        from Database.patches import _pushListenerEnabled, setPushListenerEnabledHook

        setPushListenerEnabledHook(None)
        self.assertFalse(_pushListenerEnabled())

    def test_a_raising_hook_means_polling(self):
        from Database.patches import _pushListenerEnabled, setPushListenerEnabledHook

        setPushListenerEnabledHook(MagicMock(side_effect=RuntimeError("db down")))
        with self.assertLogs("Database.patches", level="WARNING"):
            self.assertFalse(_pushListenerEnabled())

    def test_the_hook_enables_push(self):
        from Database.patches import _pushListenerEnabled, setPushListenerEnabledHook

        setPushListenerEnabledHook(lambda: True)
        self.assertTrue(_pushListenerEnabled())

    def test_with_push_off_the_loop_never_reads_the_socket(self):
        from Database.patches import setPushListenerEnabledHook
        from SpotipyFree.LastPlayed import LastPlayedManger

        setPushListenerEnabledHook(lambda: False)
        manager = _ScriptedStateManager([makeIdleState()])
        manager.get_packet = MagicMock()
        with patch("SpotipyFree.LastPlayed.PlayerStatus"):
            lpm = LastPlayedManger(MagicMock())
        lpm.manager = manager
        lpm.run = True

        with patch("time.sleep", side_effect=lambda _s: setattr(lpm, "run", False)):
            lpm.updateLoop(MagicMock(), refreshInterval=1)

        manager.get_packet.assert_not_called()

    def test_a_push_fallback_hands_over_to_polling(self):
        """The fallback has to actually reach the poll loop, or 'falls back
        automatically' is just a log line."""
        from Database.patches import setPushListenerEnabledHook
        from SpotipyFree.LastPlayed import LastPlayedManger

        setPushListenerEnabledHook(lambda: True)
        manager = _ScriptedStateManager([makeIdleState()])
        manager._deliberate_close = False
        manager.get_packet = MagicMock(return_value=None)
        manager.connect_device = MagicMock(side_effect=RuntimeError("no subscription"))
        with patch("SpotipyFree.LastPlayed.PlayerStatus"):
            lpm = LastPlayedManger(MagicMock())
        lpm.manager = manager
        lpm.run = True

        with self.assertLogs("Database.patches", level="WARNING"):
            with patch("time.sleep", side_effect=lambda _s: setattr(lpm, "run", False)):
                lpm.updateLoop(MagicMock(), refreshInterval=1)

        self.assertEqual(manager._results, [])   #< the poll loop consumed the scripted state


class TestThrottleDetection(unittest.TestCase):
    """_looksThrottled decides whether a reply is Spotify pushing back. The
    hard case is the one that actually happens: HTTP 200 carrying an HTML
    bot-check page, which no status-code check can see."""

    def _looksThrottled(self, status, response):
        from Database.patches import _looksThrottled
        return _looksThrottled(status, response)

    def test_explicit_throttle_statuses_count(self):
        self.assertTrue(self._looksThrottled(429, None))
        self.assertTrue(self._looksThrottled(503, None))

    def test_html_body_counts_even_at_http_200(self):
        """The "Oh nein!" fallback page - the exact shape seen in app.log."""
        page = "<!DOCTYPE html><html><head><title>Oh nein!</title></head><body>x</body></html>"
        self.assertTrue(self._looksThrottled(200, page))

    def test_a_normal_json_reply_is_not_throttling(self):
        self.assertFalse(self._looksThrottled(200, {"id": "alice"}))

    def test_an_ordinary_failure_is_not_throttling(self):
        """A 404 or a 500 with a plain body is a failure, not back-pressure -
        pausing every user's Spotify traffic for it would be wrong."""
        self.assertFalse(self._looksThrottled(404, "not found"))
        self.assertFalse(self._looksThrottled(500, "internal error"))

    def test_an_unstringable_body_never_raises(self):
        class Hostile:
            def __str__(self):
                raise RuntimeError("nope")

        self.assertFalse(self._looksThrottled(200, Hostile()))


class TestSharedLimiterWiring(unittest.TestCase):
    """Every Spotify request in the process goes through one limiter, and
    every rate-limit signal pauses all of them.

    This is the property the old code did NOT have: a rate limit paused the
    one listener poll thread that noticed it (~0.3 requests/minute) while every
    user's connect-state poll carried on at ~10 requests/minute each."""

    _LOGGER = "Database.patches"
    FALLBACK_PAGE = (
        "<!DOCTYPE html><html><head><title>Oh nein!</title>"
        "<style>body { background-color: #eee; }</style></head><body>x</body></html>"
    )

    def setUp(self):
        from Database.rate_limit import SPOTIFY_LIMITER
        self.limiter = SPOTIFY_LIMITER   #< conftest resets its state per test

    def _userInstance(self, response, status=200, fail=False):
        import spotapi.user
        mockLogin = MagicMock()
        mockLogin.logged_in = True
        resp = MagicMock()
        resp.status_code = status
        resp.fail = fail
        resp.response = response
        resp.raw.headers = {}
        mockLogin.client.get.return_value = resp
        return spotapi.user.User(mockLogin), mockLogin

    def test_profile_lookup_takes_a_slot_before_requesting(self):
        userInst, mockLogin = self._userInstance({"id": "alice"})

        with patch.object(self.limiter, "acquire", return_value=True) as acquire:
            userInst.get_user_info()

        acquire.assert_called_once()
        mockLogin.client.get.assert_called_once()

    def test_profile_lookup_is_skipped_while_the_process_is_paused(self):
        """No slot means the request is never sent - a backoff that still let
        the request through would be decorative."""
        from Database.patches import SpotifyLocallyRateLimitedError

        userInst, mockLogin = self._userInstance({"id": "alice"})

        with patch.object(self.limiter, "acquire", return_value=False):
            with self.assertRaises(SpotifyLocallyRateLimitedError):
                userInst.get_user_info()

        mockLogin.client.get.assert_not_called()

    def test_local_pause_classifies_as_transient_not_auth(self):
        """The listener buckets this exception the same way it buckets a real
        rate limit: back off and retry, never bounce the user to a re-login."""
        from Database.patches import SpotifyLocallyRateLimitedError
        from Database.Listeners.spotifyListener import _is_rate_limit_error, _is_auth_error

        error = SpotifyLocallyRateLimitedError(
            "Spotify rate limit backoff in progress - skipped account-settings/profile")
        self.assertTrue(_is_rate_limit_error(error))
        self.assertFalse(_is_auth_error(error))

    def test_bot_check_page_pauses_every_spotify_request(self):
        from spotapi.exceptions import UserError

        userInst, _ = self._userInstance(self.FALLBACK_PAGE)

        with patch("Database.patches._flaskDebugEnabled", return_value=False):
            with self.assertLogs(self._LOGGER, level="WARNING"):
                with self.assertRaises(UserError):
                    userInst.get_user_info()

        snapshot = self.limiter.snapshot()
        self.assertEqual(snapshot["backoffs"], 1)
        self.assertGreater(snapshot["backoffRemainingSeconds"], 0)

    def test_the_backoff_names_the_endpoint_that_pushed_back(self):
        """Which Spotify surface complained is the thing the logs could not
        answer before - all three of them logged an anonymous line."""
        from spotapi.exceptions import UserError
        from Database.patches import ENDPOINT_ACCOUNT_PROFILE, ENDPOINT_ACCOUNT_PLAN

        # Each half grants its own slot explicitly: the first bot-check opens a
        # real process-wide window, which would (correctly) refuse the second
        # call outright - the very behaviour the other tests here assert.
        for fetch, expected in (("get_user_info", ENDPOINT_ACCOUNT_PROFILE),
                                ("get_plan_info", ENDPOINT_ACCOUNT_PLAN)):
            userInst, _ = self._userInstance(self.FALLBACK_PAGE)
            with patch.object(self.limiter, "acquire", return_value=True):
                with patch("Database.patches._flaskDebugEnabled", return_value=False):
                    with self.assertLogs(self._LOGGER, level="WARNING"):
                        with self.assertRaises(UserError):
                            getattr(userInst, fetch)()
            self.assertEqual(self.limiter.snapshot()["lastReason"], expected)

    def test_the_endpoint_is_named_in_the_warning_too(self):
        from spotapi.exceptions import UserError
        from Database.patches import ENDPOINT_ACCOUNT_PROFILE

        userInst, _ = self._userInstance(self.FALLBACK_PAGE)

        with patch("Database.patches._flaskDebugEnabled", return_value=False):
            with self.assertLogs(self._LOGGER, level="WARNING") as logCapture:
                with self.assertRaises(UserError):
                    userInst.get_user_info()

        self.assertIn(ENDPOINT_ACCOUNT_PROFILE, "\n".join(logCapture.output))

    def test_a_clean_reply_pauses_nothing(self):
        userInst, _ = self._userInstance({"id": "alice"})
        userInst.get_user_info()
        self.assertEqual(self.limiter.snapshot()["backoffs"], 0)

    def test_an_ordinary_failure_pauses_nothing(self):
        """resp.fail with a plain body and a non-throttle status is a broken
        request, not back-pressure."""
        from spotapi.exceptions import UserError

        userInst, _ = self._userInstance("boom", status=500, fail=True)

        with self.assertLogs(self._LOGGER, level="WARNING"):
            with self.assertRaises(UserError):
                userInst.get_user_info()

        self.assertEqual(self.limiter.snapshot()["backoffs"], 0)

    def test_a_429_pauses_every_spotify_request(self):
        from spotapi.exceptions import UserError

        userInst, _ = self._userInstance("Too Many Requests", status=429, fail=True)

        with self.assertLogs(self._LOGGER, level="WARNING"):
            with self.assertRaises(UserError):
                userInst.get_user_info()

        self.assertEqual(self.limiter.snapshot()["backoffs"], 1)


class TestConnectStatePollLimiting(unittest.TestCase):
    """The connect-state poll loop is the dominant Spotify traffic in this
    process (~10 requests/minute per user at refreshInterval=6) and was the one
    caller no backoff could reach - it runs on spotapi's own thread, not the
    listener's."""

    def _lastPlayedManager(self, manager):
        from SpotipyFree.LastPlayed import LastPlayedManger
        with patch("SpotipyFree.LastPlayed.PlayerStatus"):
            lpm = LastPlayedManger(MagicMock())
        lpm.manager = manager
        lpm.run = True
        return lpm

    def _runIterations(self, manager, iterations):
        lpm = self._lastPlayedManager(manager)
        callback = MagicMock()
        sleepCount = [0]

        def mockSleep(_secs):
            sleepCount[0] += 1
            if sleepCount[0] >= iterations:
                lpm.run = False

        with patch("time.sleep", side_effect=mockSleep):
            lpm.updateLoop(callback, refreshInterval=1)
        return callback

    def test_each_poll_takes_a_slot(self):
        from Database.rate_limit import SPOTIFY_LIMITER

        manager = _ScriptedStateManager([makeIdleState(), makeIdleState()])

        with patch.object(SPOTIFY_LIMITER, "acquire", return_value=True) as acquire:
            self._runIterations(manager, 2)

        self.assertEqual(acquire.call_count, 2)

    def test_a_refused_slot_skips_the_poll_without_counting_a_failure(self):
        """Being held back locally is not a Spotify failure: counting it would
        escalate a backoff into a websocket reconnect, i.e. MORE traffic."""
        from Database.rate_limit import SPOTIFY_LIMITER

        manager = _ScriptedStateManager([])   #< any state access would IndexError
        lpm = self._lastPlayedManager(manager)
        refusals = [0]

        def refuseThenStop(*args, **kwargs):
            refusals[0] += 1
            if refusals[0] >= 3:
                lpm.run = False
            return False

        with patch.object(SPOTIFY_LIMITER, "acquire", side_effect=refuseThenStop):
            lpm.updateLoop(MagicMock(), refreshInterval=1)

        manager.reconnect.assert_not_called()

    def test_a_state_failure_streak_pauses_every_spotify_request(self):
        """spotapi collapses a throttled connect-state PUT to a bare
        ValueError, so a whole streak of them is the only throttling signal
        this endpoint gives us."""
        from Database.patches import STATE_FAILURE_RECONNECT_THRESHOLD, ENDPOINT_CONNECT_STATE
        from Database.rate_limit import SPOTIFY_LIMITER

        manager = _ScriptedStateManager(
            [ValueError("Could not get player state")] * STATE_FAILURE_RECONNECT_THRESHOLD
        )

        self._runIterations(manager, STATE_FAILURE_RECONNECT_THRESHOLD)

        snapshot = SPOTIFY_LIMITER.snapshot()
        self.assertEqual(snapshot["backoffs"], 1)
        self.assertEqual(snapshot["lastReason"], ENDPOINT_CONNECT_STATE)

    def test_a_sub_threshold_streak_pauses_nothing(self):
        """One blip must not stall every user - only a sustained streak does."""
        from Database.patches import STATE_FAILURE_RECONNECT_THRESHOLD
        from Database.rate_limit import SPOTIFY_LIMITER

        failures = STATE_FAILURE_RECONNECT_THRESHOLD - 1
        manager = _ScriptedStateManager([ValueError("Could not get player state")] * failures)

        with self.assertLogs("Database.patches", level="WARNING"):
            self._runIterations(manager, failures)

        self.assertEqual(SPOTIFY_LIMITER.snapshot()["backoffs"], 0)


class TestTrackFetchLimiting(unittest.TestCase):
    """Track metadata rides the same budget, but with a much longer patience:
    giving up here loses a play that really happened."""

    def test_track_fetch_takes_a_slot(self):
        from Database.patches import _get_track_info_with_retry
        from Database.rate_limit import SPOTIFY_LIMITER

        with patch("spotapi.Public.song_info", return_value={"data": {"trackUnion": fakeTrackUnion("t1")}}):
            with patch.object(SPOTIFY_LIMITER, "acquire", return_value=True) as acquire:
                _get_track_info_with_retry("t1")

        acquire.assert_called_once()

    def test_track_fetch_waits_out_a_whole_penalty_window(self):
        """A short polling timeout here would drop plays, so this call site
        gets the longer one."""
        from Database.patches import _get_track_info_with_retry
        from Database.rate_limit import SPOTIFY_LIMITER, SPOTIFY_TRACK_ACQUIRE_TIMEOUT_SECONDS

        with patch("spotapi.Public.song_info", return_value={"data": {"trackUnion": fakeTrackUnion("t1")}}):
            with patch.object(SPOTIFY_LIMITER, "acquire", return_value=True) as acquire:
                _get_track_info_with_retry("t1")

        self.assertEqual(acquire.call_args.kwargs["timeout"], SPOTIFY_TRACK_ACQUIRE_TIMEOUT_SECONDS)

    def test_a_refused_slot_is_retried_not_raised_immediately(self):
        """SpotifyLocallyRateLimitedError is the most transient failure there
        is - nothing was even sent - so it must ride the existing ladder."""
        from Database.patches import _get_track_info_with_retry
        from Database.rate_limit import SPOTIFY_LIMITER

        songInfo = MagicMock(return_value={"data": {"trackUnion": fakeTrackUnion("t1")}})
        with patch("spotapi.Public.song_info", songInfo):
            with patch.object(SPOTIFY_LIMITER, "acquire", side_effect=[False, True]):
                with patch("time.sleep"):
                    track = _get_track_info_with_retry("t1")

        self.assertEqual(track["uri"], "spotify:track:t1")
        songInfo.assert_called_once_with("t1")   #< the refused attempt sent nothing

    def test_a_refused_slot_never_re_arms_the_window_it_tripped_over(self):
        """Regression: the refusal message says "rate limit" too, so the
        substring classifier here counted our own open window as Spotify
        pushback and extended it - the same feedback loop the listener had
        (2026-07-29 17:43). Matched by type, ahead of the strings."""
        from Database.patches import _get_track_info_with_retry
        from Database.rate_limit import SPOTIFY_LIMITER

        songInfo = MagicMock(return_value={"data": {"trackUnion": fakeTrackUnion("t1")}})
        with patch("spotapi.Public.song_info", songInfo):
            with patch.object(SPOTIFY_LIMITER, "acquire", side_effect=[False, True]):
                with patch("time.sleep"):
                    _get_track_info_with_retry("t1")

        self.assertEqual(SPOTIFY_LIMITER.snapshot()["backoffs"], 0)

    def test_a_spotify_rate_limit_pauses_every_spotify_request(self):
        from Database.patches import _get_track_info_with_retry, ENDPOINT_TRACK_INFO
        from Database.rate_limit import SPOTIFY_LIMITER

        with patch("spotapi.Public.song_info", side_effect=Exception("429 rate limit exceeded")):
            with patch("time.sleep"):
                with self.assertRaisesRegex(Exception, "429"):
                    _get_track_info_with_retry("t1", max_retries=1)

        snapshot = SPOTIFY_LIMITER.snapshot()
        self.assertGreaterEqual(snapshot["backoffs"], 1)
        self.assertEqual(snapshot["lastReason"], ENDPOINT_TRACK_INFO)

    def test_a_real_404_still_raises_without_pausing_anything(self):
        from Database.patches import _get_track_info_with_retry
        from Database.rate_limit import SPOTIFY_LIMITER

        with patch("spotapi.Public.song_info", side_effect=Exception("404 not found")):
            with self.assertRaisesRegex(Exception, "404"):
                _get_track_info_with_retry("t1")

        self.assertEqual(SPOTIFY_LIMITER.snapshot()["backoffs"], 0)


if __name__ == "__main__":
    unittest.main()


