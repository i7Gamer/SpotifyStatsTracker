import unittest
from unittest.mock import MagicMock, patch
import json
import signal
import threading
import websockets.sync.client
import spotapi.status
import spotapi.websocket

import Database.rate_limit as rateLimitModule


def setUpModule():
    # Database.patches applies its spotapi patches once, at whatever moment
    # Database (the package) first gets imported. If that happened while some
    # other test module's mock was still in sys.modules, the real spotapi
    # would never get patched for the rest of the process. Re-applying here
    # makes this module correct regardless of import order.
    from Database.patches import patch_spotapi_user, patch_totp_secret
    patch_spotapi_user()
    patch_totp_secret()


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


class TestPinnedTotpSecret(unittest.TestCase):
    """Spotify's web player derives a TOTP from a rotating secret. spotapi
    fetched that secret from a third-party mirror on every cold start and
    every 15 minutes after - a host we don't control, can't pin and didn't
    choose, sitting in the login path, with a SILENT fallback when it failed.
    The secret is pinned here instead."""

    def test_the_pinned_secret_is_returned_without_touching_the_network(self):
        import spotapi.client
        from Database.patches import SPOTIFY_TOTP_SECRET_VERSION, SPOTIFY_TOTP_SECRET_BYTES

        with patch("spotapi.client.requests.get") as mirrorGet:
            version, secret = spotapi.client.get_latest_totp_secret()

        mirrorGet.assert_not_called()   #< the mirror is where the fetch used to go
        self.assertEqual(version, SPOTIFY_TOTP_SECRET_VERSION)
        self.assertEqual(secret, bytearray(SPOTIFY_TOTP_SECRET_BYTES))

    def test_the_secret_is_a_fresh_bytearray_each_call(self):
        """generate_totp() enumerates and transforms the bytes; handing out the
        same mutable object every time would let one caller's mutation corrupt
        every later login."""
        import spotapi.client

        _, first = spotapi.client.get_latest_totp_secret()
        first[0] ^= 0xFF
        _, second = spotapi.client.get_latest_totp_secret()

        self.assertNotEqual(first, second)

    def test_generate_totp_still_produces_a_code_for_the_pinned_version(self):
        """The patch replaces only where the secret comes from - the derivation
        spotapi does with it, and the version it reports, must be unchanged."""
        import spotapi.client
        from Database.patches import SPOTIFY_TOTP_SECRET_VERSION

        totp, version = spotapi.client.generate_totp()

        self.assertEqual(version, SPOTIFY_TOTP_SECRET_VERSION)
        self.assertRegex(totp, r"^\d{6}$")

    def test_the_pin_matches_the_dependency_s_own_fallback(self):
        """Sanity check on spotapi itself, and the evidence that pinning
        changed nothing: as of 2026-07-30 the mirror's newest entry and
        spotapi's hardcoded _FALLBACK_SECRET are byte-identical, so the fetch
        this patch removes was returning what the library already had. If a
        spotapi bump changes its fallback, this fails - which is the moment to
        check whether Spotify rotated and the pin needs refreshing."""
        import spotapi.client
        from Database.patches import SPOTIFY_TOTP_SECRET_VERSION, SPOTIFY_TOTP_SECRET_BYTES

        fallbackVersion, fallbackSecret = spotapi.client._FALLBACK_SECRET

        self.assertEqual(int(fallbackVersion), SPOTIFY_TOTP_SECRET_VERSION)
        self.assertEqual(bytes(fallbackSecret), bytes(bytearray(SPOTIFY_TOTP_SECRET_BYTES)))


class TestTotpSecretOverride(unittest.TestCase):
    """Rotation must not require a new Docker image. The published image is
    what most installs run, so an operator needs a way to apply a new secret
    without waiting for a release - but a malformed value must never be what
    takes an instance's login offline."""

    def _resolve(self, raw):
        from Database.patches import _resolveTotpSecret
        with patch.dict("os.environ", {"SPOTIFY_TOTP_SECRET": raw} if raw is not None else {},
                        clear=False):
            if raw is None:
                import os
                os.environ.pop("SPOTIFY_TOTP_SECRET", None)
            return _resolveTotpSecret()

    def test_unset_uses_the_pinned_default(self):
        from Database.patches import SPOTIFY_TOTP_SECRET_VERSION, SPOTIFY_TOTP_SECRET_BYTES

        version, secret = self._resolve(None)

        self.assertEqual(version, SPOTIFY_TOTP_SECRET_VERSION)
        self.assertEqual(secret, bytearray(SPOTIFY_TOTP_SECRET_BYTES))

    def test_a_well_formed_override_wins(self):
        version, secret = self._resolve("62: 1, 2 ,3")

        self.assertEqual(version, 62)
        self.assertEqual(secret, bytearray([1, 2, 3]))

    def test_a_malformed_override_falls_back_to_the_pin_and_says_so(self):
        from Database.patches import SPOTIFY_TOTP_SECRET_VERSION

        for raw in ("nonsense", "61:", ":1,2,3", "61:1,two,3", "61:1,999,3", "61:-1,2"):
            with self.subTest(raw=raw):
                with self.assertLogs("Database.patches", level="ERROR"):
                    version, _ = self._resolve(raw)
                self.assertEqual(version, SPOTIFY_TOTP_SECRET_VERSION)

    def test_an_empty_override_is_just_unset_and_stays_quiet(self):
        """Set-but-empty is what an unfilled compose placeholder looks like.
        It means "no override", so it must not log an error on every start."""
        from Database.patches import SPOTIFY_TOTP_SECRET_VERSION

        with self.assertNoLogs("Database.patches", level="ERROR"):
            version, _ = self._resolve("   ")

        self.assertEqual(version, SPOTIFY_TOTP_SECRET_VERSION)


class TestTotpRotationTracking(unittest.TestCase):
    """A rotated secret is invisible in the logs until someone greps for it,
    and its signature is distinctive: EVERY token request fails, for every
    user, persistently. Confirmed empirically (2026-07-30) by running the
    smoke test with a deliberately wrong secret - the public lookups, which
    need the TOTP-derived token but no user cookies, all failed with
    "Could not get session auth tokens", while a bad-cookies run left them
    passing and failed only current_user(). One failure is not that, though:
    a 429, an outage or a blip produce the same exception, so the state only
    flips after a run of them."""

    def setUp(self):
        from Database.patches import resetTotpAuthState
        resetTotpAuthState()
        self.addCleanup(resetTotpAuthState)

    def _fail(self, times):
        from Database.patches import recordTotpAuthFailure
        for _ in range(times):
            recordTotpAuthFailure()

    def test_a_healthy_instance_reports_ok(self):
        from Database.patches import totpAuthSnapshot

        snapshot = totpAuthSnapshot()

        self.assertFalse(snapshot["suspectedRotation"])
        self.assertEqual(snapshot["consecutiveFailures"], 0)
        self.assertEqual(snapshot["pinnedVersion"], 61)

    def test_a_single_failure_is_not_a_rotation(self):
        """Blips must not raise an alarm that sends someone editing secrets."""
        from Database.patches import totpAuthSnapshot

        self._fail(1)

        self.assertFalse(totpAuthSnapshot()["suspectedRotation"])

    def test_a_sustained_run_of_failures_is(self):
        from Database.patches import totpAuthSnapshot, TOTP_ROTATION_CONFIRM_THRESHOLD

        self._fail(TOTP_ROTATION_CONFIRM_THRESHOLD)

        snapshot = totpAuthSnapshot()
        self.assertTrue(snapshot["suspectedRotation"])
        self.assertEqual(snapshot["consecutiveFailures"], TOTP_ROTATION_CONFIRM_THRESHOLD)
        self.assertIsNotNone(snapshot["secondsSinceFirstFailure"])

    def test_one_success_clears_it(self):
        """Whatever it was, it is over - a stale alarm is worse than none."""
        from Database.patches import recordTotpAuthSuccess, totpAuthSnapshot, TOTP_ROTATION_CONFIRM_THRESHOLD

        self._fail(TOTP_ROTATION_CONFIRM_THRESHOLD)
        recordTotpAuthSuccess()

        snapshot = totpAuthSnapshot()
        self.assertFalse(snapshot["suspectedRotation"])
        self.assertEqual(snapshot["consecutiveFailures"], 0)

    def test_the_snapshot_names_what_an_operator_needs(self):
        """The panel has to answer "what now?" without a trip to the source."""
        from Database.patches import totpAuthSnapshot, TOTP_SECRET_ENV_VAR

        snapshot = totpAuthSnapshot()

        self.assertEqual(snapshot["overrideEnvVar"], TOTP_SECRET_ENV_VAR)
        self.assertIn("overrideActive", snapshot)

    def test_it_reports_when_an_override_is_in_force(self):
        """A fetched/overridden secret must not look like the pinned one -
        otherwise "we pin 61" is a lie the panel tells you during an incident."""
        from Database.patches import totpAuthSnapshot, TOTP_SECRET_ENV_VAR

        with patch.dict("os.environ", {TOTP_SECRET_ENV_VAR: "62:1,2,3"}):
            snapshot = totpAuthSnapshot()

        self.assertTrue(snapshot["overrideActive"])
        self.assertEqual(snapshot["activeVersion"], 62)

    def test_reapplying_the_patch_does_not_double_count(self):
        """patch_totp_secret runs at import and is re-applied by setUpModule
        when import order may have missed it. A non-idempotent wrapper would
        nest, so a single failed request would count twice and trip the
        rotation threshold on a third of the evidence it is supposed to need."""
        import spotapi.client
        from spotapi.exceptions import BaseClientError
        from Database.patches import patch_totp_secret, totpAuthSnapshot

        patch_totp_secret()
        patch_totp_secret()

        instance = spotapi.client.BaseClient.__new__(spotapi.client.BaseClient)
        instance.access_token = spotapi.client._Undefined
        instance.client_id = spotapi.client._Undefined
        failure = MagicMock()
        failure.fail = True
        failure.error.string = "400 Bad Request"
        instance.client = MagicMock()
        instance.client.get.return_value = failure

        with self.assertRaises(BaseClientError):
            spotapi.client.BaseClient._get_auth_vars(instance)

        self.assertEqual(totpAuthSnapshot()["consecutiveFailures"], 1)

    def test_the_patched_call_feeds_the_tracker(self):
        """The counter is worthless if the real code path doesn't drive it."""
        import spotapi.client
        from spotapi.exceptions import BaseClientError
        from Database.patches import totpAuthSnapshot

        instance = spotapi.client.BaseClient.__new__(spotapi.client.BaseClient)
        instance.access_token = spotapi.client._Undefined
        instance.client_id = spotapi.client._Undefined
        failure = MagicMock()
        failure.fail = True
        failure.error.string = "400 Bad Request"
        instance.client = MagicMock()
        instance.client.get.return_value = failure

        with self.assertRaises(BaseClientError):
            spotapi.client.BaseClient._get_auth_vars(instance)

        self.assertEqual(totpAuthSnapshot()["consecutiveFailures"], 1)


class TestTotpAutoRecovery(unittest.TestCase):
    """Once a rotation is confirmed, the new secret is read from Spotify's own
    web-player bundle - the source of truth, not a third-party mirror. This is
    the RECOVERY path only: the pinned secret stays the normal one, so a
    Spotify restructure degrades to "recovery unavailable" (today's behaviour)
    rather than to broken logins."""

    def setUp(self):
        from Database.patches import resetTotpAuthState
        resetTotpAuthState()
        self.addCleanup(resetTotpAuthState)

    def _fetches(self, secrets):
        return patch("Database.patches.fetchWebPlayerSecrets", return_value=secrets)

    def test_a_newer_secret_is_adopted(self):
        from Database.patches import attemptTotpRecovery, _resolveTotpSecret

        with self._fetches([(62, bytearray([1, 2, 3])), (61, bytearray([9]))]):
            with self.assertLogs("Database.patches", level="WARNING"):
                adopted = attemptTotpRecovery()

        self.assertTrue(adopted)
        self.assertEqual(_resolveTotpSecret(), (62, bytearray([1, 2, 3])))

    def test_the_same_version_is_not_adopted(self):
        """Re-reading the version we already pin proves nothing changed, so
        swapping it in would only muddy which secret is in force."""
        from Database.patches import attemptTotpRecovery, SPOTIFY_TOTP_SECRET_VERSION

        with self._fetches([(SPOTIFY_TOTP_SECRET_VERSION, bytearray([1, 2, 3]))]):
            self.assertFalse(attemptTotpRecovery())

    def test_an_older_secret_is_never_adopted(self):
        """A stale or rolled-back bundle must not walk us backwards."""
        from Database.patches import attemptTotpRecovery, _resolveTotpSecret, SPOTIFY_TOTP_SECRET_VERSION

        with self._fetches([(SPOTIFY_TOTP_SECRET_VERSION - 5, bytearray([1, 2, 3]))]):
            self.assertFalse(attemptTotpRecovery())
        self.assertEqual(_resolveTotpSecret()[0], SPOTIFY_TOTP_SECRET_VERSION)

    def test_finding_nothing_is_not_a_crash(self):
        from Database.patches import attemptTotpRecovery, _resolveTotpSecret, SPOTIFY_TOTP_SECRET_VERSION

        with self._fetches([]):
            self.assertFalse(attemptTotpRecovery())
        self.assertEqual(_resolveTotpSecret()[0], SPOTIFY_TOTP_SECRET_VERSION)

    def test_an_explicit_override_still_wins(self):
        """A human setting the variable is a decision; a scrape is a guess."""
        from Database.patches import attemptTotpRecovery, _resolveTotpSecret, TOTP_SECRET_ENV_VAR

        with self._fetches([(62, bytearray([1, 2, 3]))]):
            with self.assertLogs("Database.patches", level="WARNING"):
                attemptTotpRecovery()

        with patch.dict("os.environ", {TOTP_SECRET_ENV_VAR: "70:7,7,7"}):
            self.assertEqual(_resolveTotpSecret(), (70, bytearray([7, 7, 7])))

    def test_recovery_is_rate_limited(self):
        """An instance in this state retries constantly; without a cooldown it
        would hammer Spotify's CDN for every failed login."""
        from Database.patches import attemptTotpRecovery

        with self._fetches([]) as fetch:
            attemptTotpRecovery()
            attemptTotpRecovery()
            attemptTotpRecovery()

        self.assertEqual(fetch.call_count, 1)

    def test_it_can_be_switched_off(self):
        from Database.patches import attemptTotpRecovery, TOTP_AUTO_RECOVER_ENV_VAR

        with self._fetches([(62, bytearray([1, 2, 3]))]) as fetch:
            with patch.dict("os.environ", {TOTP_AUTO_RECOVER_ENV_VAR: "0"}):
                self.assertFalse(attemptTotpRecovery())

        fetch.assert_not_called()

    def test_a_confirmed_rotation_triggers_it(self):
        """The wiring: the failure streak reaching the threshold is what calls
        recovery - it must not need anything else to notice."""
        import spotapi.client
        from spotapi.exceptions import BaseClientError
        from Database.patches import TOTP_ROTATION_CONFIRM_THRESHOLD

        instance = spotapi.client.BaseClient.__new__(spotapi.client.BaseClient)
        instance.access_token = spotapi.client._Undefined
        instance.client_id = spotapi.client._Undefined
        failure = MagicMock()
        failure.fail = True
        failure.error.string = "400 Bad Request"
        instance.client = MagicMock()
        instance.client.get.return_value = failure

        with self._fetches([]) as fetch:
            with self.assertLogs("Database.patches", level="ERROR"):
                for _ in range(TOTP_ROTATION_CONFIRM_THRESHOLD):
                    with self.assertRaises(BaseClientError):
                        spotapi.client.BaseClient._get_auth_vars(instance)
            self._joinRecoveryThread()

        fetch.assert_called_once()

    def test_recovery_runs_off_the_failing_thread(self):
        """The thread that hit the auth failure can be a Flask request thread
        (login's cookie verification reaches _get_auth_vars), and recovery is
        two 15s-timeout GETs plus a multi-MB bundle download - inline, a login
        POST during a rotation hung for ~30s+. The adopted secret only takes
        effect on the NEXT attempt anyway, so nothing needs to wait: the fetch
        must run on its own thread, never the caller's."""
        import threading
        import spotapi.client
        from spotapi.exceptions import BaseClientError
        from Database.patches import TOTP_ROTATION_CONFIRM_THRESHOLD

        instance = spotapi.client.BaseClient.__new__(spotapi.client.BaseClient)
        instance.access_token = spotapi.client._Undefined
        instance.client_id = spotapi.client._Undefined
        failure = MagicMock()
        failure.fail = True
        failure.error.string = "400 Bad Request"
        instance.client = MagicMock()
        instance.client.get.return_value = failure

        fetchThreads = []

        def recordingFetch():
            fetchThreads.append(threading.current_thread())
            return []

        with patch("Database.patches.fetchWebPlayerSecrets", side_effect=recordingFetch):
            with self.assertLogs("Database.patches", level="ERROR"):
                for _ in range(TOTP_ROTATION_CONFIRM_THRESHOLD):
                    with self.assertRaises(BaseClientError):
                        spotapi.client.BaseClient._get_auth_vars(instance)
            self._joinRecoveryThread()

        self.assertEqual(len(fetchThreads), 1)
        self.assertIsNot(fetchThreads[0], threading.current_thread(),
                         "recovery fetched on the failing caller's thread - "
                         "a login request would block on it")

    def _joinRecoveryThread(self):
        """Recovery is asynchronous by design; tests wait for it explicitly
        instead of sleeping (deterministic, no clock)."""
        import Database.patches as patches
        thread = patches._totpRecoveryThread
        self.assertIsNotNone(thread, "no recovery thread was started")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "recovery thread did not finish")

    def test_a_single_failure_does_not_trigger_it(self):
        """Recovery is for a confirmed rotation, not for every blip."""
        import spotapi.client
        from spotapi.exceptions import BaseClientError

        instance = spotapi.client.BaseClient.__new__(spotapi.client.BaseClient)
        instance.access_token = spotapi.client._Undefined
        instance.client_id = spotapi.client._Undefined
        failure = MagicMock()
        failure.fail = True
        failure.error.string = "429 Too Many Requests"
        instance.client = MagicMock()
        instance.client.get.return_value = failure

        with self._fetches([]) as fetch:
            with self.assertRaises(BaseClientError):
                spotapi.client.BaseClient._get_auth_vars(instance)

        fetch.assert_not_called()

    def test_a_malformed_override_does_not_report_as_active(self):
        """_resolveTotpSecret IGNORES a malformed override (a typo must not
        take login offline), so the snapshot saying OVERRIDDEN for one is a
        lie with a cost: it also suppresses the autoRecovered flag, whose
        admin-panel note is the only prompt to pin an adopted secret before a
        restart loses it. The operator trusts their (dead) override and never
        pins."""
        from Database.patches import attemptTotpRecovery, totpAuthSnapshot, TOTP_SECRET_ENV_VAR

        with self._fetches([(62, bytearray([1, 2, 3]))]):
            with self.assertLogs("Database.patches", level="WARNING"):
                attemptTotpRecovery()

        with patch.dict("os.environ", {TOTP_SECRET_ENV_VAR: "62:44,55,notabyte"}):
            snapshot = totpAuthSnapshot()

        self.assertFalse(snapshot["overrideActive"])
        self.assertTrue(snapshot["autoRecovered"])
        self.assertEqual(snapshot["activeVersion"], 62)   #< what is actually in force

    def test_a_wellformed_override_reports_as_active(self):
        from Database.patches import totpAuthSnapshot, TOTP_SECRET_ENV_VAR

        with patch.dict("os.environ", {TOTP_SECRET_ENV_VAR: "70:7,7,7"}):
            snapshot = totpAuthSnapshot()

        self.assertTrue(snapshot["overrideActive"])
        self.assertFalse(snapshot["autoRecovered"])
        self.assertEqual(snapshot["activeVersion"], 70)

    def test_the_admin_snapshot_reports_an_adopted_secret(self):
        """"We pin 61" must not be what the panel says while running on 62."""
        from Database.patches import attemptTotpRecovery, totpAuthSnapshot

        with self._fetches([(62, bytearray([1, 2, 3]))]):
            with self.assertLogs("Database.patches", level="WARNING"):
                attemptTotpRecovery()

        snapshot = totpAuthSnapshot()
        self.assertTrue(snapshot["autoRecovered"])
        self.assertEqual(snapshot["activeVersion"], 62)
        self.assertEqual(snapshot["pinnedVersion"], 61)


class TestAuthFailureHint(unittest.TestCase):
    """When Spotify rotates the secret, the only symptom is
    BaseClientError("Could not get session auth tokens") from a module nobody
    here owns - a message that names neither TOTP nor the pinned constant.
    Without a hint, the pin is undiscoverable from the failure it causes."""

    def setUp(self):
        from Database.patches import resetTotpAuthState
        resetTotpAuthState()   #< the failure streak is process-global
        self.addCleanup(resetTotpAuthState)

    def _failingClient(self):
        import spotapi.client
        instance = spotapi.client.BaseClient.__new__(spotapi.client.BaseClient)
        instance.access_token = spotapi.client._Undefined
        instance.client_id = spotapi.client._Undefined
        failure = MagicMock()
        failure.fail = True
        failure.error.string = "401 Unauthorized"
        instance.client = MagicMock()
        instance.client.get.return_value = failure
        return instance

    def test_a_sustained_token_failure_points_at_the_pinned_secret(self):
        """Reported once the streak confirms it, not per attempt: an instance
        in this state retries constantly, and a line per attempt would bury
        the incident it is reporting."""
        import spotapi.client
        from spotapi.exceptions import BaseClientError
        from Database.patches import TOTP_ROTATION_CONFIRM_THRESHOLD

        client = self._failingClient()
        with self.assertLogs("Database.patches", level="ERROR") as logCapture:
            for _ in range(TOTP_ROTATION_CONFIRM_THRESHOLD):
                with self.assertRaises(BaseClientError):
                    spotapi.client.BaseClient._get_auth_vars(client)

        self.assertEqual(len(logCapture.records), 1, "one line per confirmed streak, not per attempt")
        message = " ".join(logCapture.output)
        self.assertIn("SPOTIFY_TOTP_SECRET", message)   #< the env override an operator can set
        self.assertIn("61", message)                    #< the version currently pinned

    def test_a_successful_call_is_untouched(self):
        import spotapi.client

        instance = self._failingClient()
        ok = MagicMock()
        ok.fail = False
        ok.response = {"accessToken": "tok", "clientId": "cid",
                       "accessTokenExpirationTimestampMs": "1234"}
        instance.client.get.return_value = ok

        spotapi.client.BaseClient._get_auth_vars(instance)

        self.assertEqual(instance.access_token, "tok")
        self.assertEqual(instance.client_id, "cid")
        self.assertEqual(instance.access_token_expires_at_ms, 1234.0)


if __name__ == "__main__":
    unittest.main()


