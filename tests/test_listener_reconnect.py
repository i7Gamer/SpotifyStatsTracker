"""Tests for the Listener's self-healing reconnect.

spotapi's own websocket-fed "recently played" feed can silently die (its
reconnect() call in LastPlayedManger.updateLoop targets a method that doesn't
exist on PlayerStatus, so recovery from a dropped websocket is broken upstream
- see Database/Listeners/spotifyListener.py). Once that happens,
current_user_recently_played() keeps returning the same frozen list forever:
no exception, no new items, nothing recorded, indefinitely. This tests the
staleness timeout that detects that frozen state and asks the caller to
rebuild the session instead of staying wedged forever.
"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.Listeners.spotifyListener import (
    Listener,
    LISTENER_STALE_TIMEOUT_SECONDS,
    USER_VALIDATION_CACHE_SECONDS,
    _is_auth_error,
    _is_rate_limit_error,
    classifyListenerError,
)

# Comfortably past USER_VALIDATION_CACHE_SECONDS so _validateCurrentUser's
# freshness-cache branch is deterministically bypassed, regardless of how
# large time.monotonic() already is on the host running the test (e.g. a
# freshly booted CI runner has a much smaller monotonic clock than a
# long-uptime dev machine).
_MONOTONIC_NOW = USER_VALIDATION_CACHE_SECONDS * 10


def _bareListener(recentlyPlayed=None):
    listener = Listener.__new__(Listener)
    listener.run = True
    listener.sp = MagicMock()
    listener.recentlyPlayed_Z1 = recentlyPlayed if recentlyPlayed is not None else []
    listener.sp.current_user_recently_played.return_value = listener.recentlyPlayed_Z1
    listener._lastChangeTime = 0.0
    listener._authenticated_user_id = None
    listener.email = None
    listener._last_user_validation_time = None  #< matches Listener.__init__: never validated yet
    listener._last_user_validation_result = True
    return listener


class TestCheckOnceNewItems(unittest.TestCase):
    def test_new_item_invokes_callback_and_resets_change_time(self):
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener.sp.current_user_recently_played.return_value = [{"played_at": 1}, {"played_at": 2}]
        callback = MagicMock()

        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=500.0):
            stillRunning = listener._checkOnce(callback, onStale=None)

        self.assertTrue(stillRunning)
        callback.assert_called_once_with([{"played_at": 2}])
        self.assertEqual(listener.recentlyPlayed_Z1, [{"played_at": 1}, {"played_at": 2}])
        self.assertEqual(listener._lastChangeTime, 500.0)

    def test_unchanged_feed_within_timeout_does_not_trigger_onStale(self):
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener._lastChangeTime = 100.0
        onStale = MagicMock()

        withinTimeout = 100.0 + LISTENER_STALE_TIMEOUT_SECONDS - 1
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=withinTimeout):
            stillRunning = listener._checkOnce(MagicMock(), onStale=onStale)

        self.assertTrue(stillRunning)
        onStale.assert_not_called()


class TestCheckOnceStaleness(unittest.TestCase):
    def test_frozen_feed_past_timeout_triggers_onStale_and_stops(self):
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener._lastChangeTime = 100.0
        onStale = MagicMock()

        pastTimeout = 100.0 + LISTENER_STALE_TIMEOUT_SECONDS + 1
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=pastTimeout):
            stillRunning = listener._checkOnce(MagicMock(), onStale=onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()

    def test_frozen_feed_past_timeout_without_onStale_keeps_running(self):
        """No onStale callback wired (e.g. a bare/manual Listener) - must not
        crash, and since there's no way to recover, it just keeps polling."""
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener._lastChangeTime = 100.0

        pastTimeout = 100.0 + LISTENER_STALE_TIMEOUT_SECONDS + 1
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=pastTimeout):
            stillRunning = listener._checkOnce(MagicMock(), onStale=None)

        self.assertTrue(stillRunning)

    def test_onStale_exception_is_swallowed_and_still_stops(self):
        """A failed reconnect attempt (e.g. cookies genuinely expired) must not
        crash the polling thread silently with no trace - the exception is
        logged and the (now-dead) listener still stops."""
        listener = _bareListener(recentlyPlayed=[{"played_at": 1}])
        listener._lastChangeTime = 100.0
        onStale = MagicMock(side_effect=RuntimeError("reconnect failed"))

        pastTimeout = 100.0 + LISTENER_STALE_TIMEOUT_SECONDS + 1
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=pastTimeout):
            stillRunning = listener._checkOnce(MagicMock(), onStale=onStale)

        self.assertFalse(stillRunning)
        onStale.assert_called_once()


class TestAuthErrorDetection(unittest.TestCase):
    def test_loginerror_is_detected_as_auth_error(self):
        exc = Exception("spotapi.exceptions.errors.LoginError: Could not GET ...")
        self.assertTrue(_is_auth_error(exc))

    def test_401_status_is_detected_as_auth_error(self):
        exc = Exception("HTTP 401 Unauthorized")
        self.assertTrue(_is_auth_error(exc))

    def test_403_status_is_detected_as_auth_error(self):
        exc = Exception("HTTP 403 Forbidden")
        self.assertTrue(_is_auth_error(exc))

    def test_expired_session_is_detected_as_auth_error(self):
        exc = Exception("Session expired")
        self.assertTrue(_is_auth_error(exc))

    def test_invalid_token_is_detected_as_auth_error(self):
        exc = Exception("Invalid access token")
        self.assertTrue(_is_auth_error(exc))

    def test_503_error_is_not_detected_as_auth_error(self):
        exc = Exception("HTTP 503 Service Unavailable")
        self.assertFalse(_is_auth_error(exc))

    def test_timeout_error_is_not_detected_as_auth_error(self):
        exc = Exception("Connection timeout")
        self.assertFalse(_is_auth_error(exc))


class TestRateLimitErrorDetection(unittest.TestCase):
    """Characterization of _is_rate_limit_error (the transient bucket), which
    had no direct tests before. Pins today's string heuristic so a later
    narrowing pass is a conscious, test-visible change."""

    def test_429_is_transient(self):
        self.assertTrue(_is_rate_limit_error(Exception("HTTP 429 Too Many Requests")))

    def test_rate_limit_phrase_is_transient(self):
        self.assertTrue(_is_rate_limit_error(Exception("Rate limit exceeded, slow down")))

    def test_malformed_json_wording_is_transient(self):
        # The "json" substring is what today catches Spotify answering with a
        # non-JSON bot-check page instead of the profile JSON.
        self.assertTrue(_is_rate_limit_error(Exception("Invalid JSON in response body")))

    def test_503_is_not_transient(self):
        self.assertFalse(_is_rate_limit_error(Exception("HTTP 503 Service Unavailable")))

    def test_timeout_is_not_transient(self):
        self.assertFalse(_is_rate_limit_error(Exception("Connection timeout")))


class TestClassifyListenerError(unittest.TestCase):
    """classifyListenerError is the single seam behind both predicates; its
    (isAuth, isTransient) pair must stay INDEPENDENT - some errors are both,
    and call-site precedence relies on that rather than one flag winning."""

    def test_pure_auth_error(self):
        self.assertEqual(classifyListenerError(Exception("HTTP 401 Unauthorized")), (True, False))

    def test_pure_transient_error(self):
        self.assertEqual(classifyListenerError(Exception("HTTP 429 Too Many Requests")), (False, True))

    def test_neither_bucket(self):
        self.assertEqual(classifyListenerError(Exception("HTTP 503 Service Unavailable")), (False, False))

    def test_error_that_is_both_auth_and_transient(self):
        # A rate-limited login failure matches both; the flags must stay
        # independent so each call site's own precedence still applies.
        self.assertEqual(classifyListenerError(Exception("LoginError: 429 rate limited")), (True, True))

    def test_predicates_are_thin_wrappers_over_the_pair(self):
        exc = Exception("Invalid access token")
        isAuth, isTransient = classifyListenerError(exc)
        self.assertEqual(_is_auth_error(exc), isAuth)
        self.assertEqual(_is_rate_limit_error(exc), isTransient)


class TestClassifyRealSpotapiExceptions(unittest.TestCase):
    """The classifier must handle actual spotapi exception instances, not just
    Exception('...text...'): LoginError is classified by its type name even
    when its message carries no auth keyword."""

    def test_spotapi_loginerror_is_auth_by_type_name(self):
        from spotapi.exceptions.errors import LoginError
        # Message has NO auth keyword - the type name is what classifies it.
        self.assertEqual(classifyListenerError(LoginError("Could not GET recently played")), (True, False))

    def test_spotapi_requesterror_429_is_transient(self):
        from spotapi.exceptions.errors import RequestError
        self.assertEqual(classifyListenerError(RequestError("Got status 429 from server")), (False, True))

    def test_spotapi_requesterror_503_is_neither(self):
        from spotapi.exceptions.errors import RequestError
        self.assertEqual(classifyListenerError(RequestError("Got status 503 from server")), (False, False))


class TestClassificationDiagnostic(unittest.TestCase):
    """The FLASK_DEBUG-gated diagnostic that records the concrete exception type
    at each classification - the observability a real misclassification report
    needs before the heuristics can be safely narrowed. Off by default so it
    never spams production logs."""

    _LOGGER = "Database.Listeners.spotifyListener"

    def test_logs_type_and_flags_when_flask_debug_enabled(self):
        with patch("Database.Listeners.spotifyListener._flaskDebugEnabled", return_value=True):
            with self.assertLogs(self._LOGGER, level="INFO") as cm:
                classifyListenerError(Exception("HTTP 401 Unauthorized"))
        joined = "\n".join(cm.output)
        self.assertIn("isAuth=True", joined)
        self.assertIn("isTransient=False", joined)
        self.assertIn("builtins.Exception", joined)   #< the fully-qualified type

    def test_silent_when_flask_debug_disabled(self):
        import logging
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record)
        moduleLogger = logging.getLogger(self._LOGGER)
        moduleLogger.addHandler(handler)
        try:
            with patch("Database.Listeners.spotifyListener._flaskDebugEnabled", return_value=False):
                classifyListenerError(Exception("HTTP 401 Unauthorized"))
        finally:
            moduleLogger.removeHandler(handler)
        self.assertEqual([r for r in records if "classified" in r.getMessage()], [])


class TestValidateCurrentUser(unittest.TestCase):
    def test_valid_user_returns_true(self):
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.return_value = {"id": "user1"}
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            self.assertTrue(listener._validateCurrentUser())

    def test_mismatched_user_returns_false(self):
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.return_value = {"id": "user2"}
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            self.assertFalse(listener._validateCurrentUser())

    def test_auth_error_returns_false_and_does_not_raise(self):
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.side_effect = Exception("HTTP 401 Unauthorized")
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            self.assertFalse(listener._validateCurrentUser())

    def test_transient_error_bubbles_up(self):
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.side_effect = Exception("HTTP 503 Service Unavailable")
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=_MONOTONIC_NOW):
            with self.assertRaises(Exception) as ctx:
                listener._validateCurrentUser()
        self.assertIn("503", str(ctx.exception))

    def test_first_check_runs_even_with_a_low_monotonic_clock(self):
        """Regression test: _last_user_validation_time must start as None
        ("never validated"), not 0, so the very first check always performs
        a real validation - even on a host where time.monotonic() itself is
        still small (e.g. shortly after boot), which previously made a
        freshly constructed Listener look like it had already validated
        "recently" and silently return the unvalidated cached default."""
        listener = _bareListener()
        listener._authenticated_user_id = "user1"
        listener.sp.current_user.return_value = {"id": "user1"}

        lowUptimeMonotonic = 1.0  # smaller than USER_VALIDATION_CACHE_SECONDS
        with patch("Database.Listeners.spotifyListener.time.monotonic", return_value=lowUptimeMonotonic):
            listener._validateCurrentUser()

        listener.sp.current_user.assert_called_once()


if __name__ == "__main__":
    unittest.main()
