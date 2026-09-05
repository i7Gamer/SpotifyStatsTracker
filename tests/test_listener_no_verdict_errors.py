# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A current_user() failure that never got Spotify's opinion of the cookies is
not a logout.

Two live shapes read as one for two months (2026-09-05 review, L1). A transport
failure - spotapi's RequestError("Failed to complete request.", error=<curl
detail>) - and a Spotify 5xx on the profile endpoint - LoginError("Could not
GET <url>. Status Code: 503") - both fell through isLoggedIn()'s except to the
"real refusal" False. user_registry then caches that False for
LOGIN_CACHE_TTL_SECONDS: every page bounced to /login and a correct password
was refused for the TTL (39x503 + 1x504 on live). _validateCurrentUser read the
503 the same way and rebuilt the whole listener for it, 32 times.

The fix is a no-verdict predicate consulted only at those two sites - NOT a
widening of classifyListenerError, whose (isAuth, isTransient) pair drives
startListener's precedence: a transport failure that became "transient" there
would open the process-wide SPOTIFY_LIMITER backoff for a local outage. The
anti-regression class at the bottom pins that the classifier still answers
(False, False) for the real RequestError and that startListener leaves the
shared limiter alone.

The exceptions are the real ones, driven through spotapi's own TLSClient (see
tests/_spotapi_exceptions.py) - the earlier Exception('...') stand-ins pinned
message shapes spotapi never produces."""
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from _spotapi_exceptions import realProfileStatusError, realTransportRequestError
from Database.Listeners import spotifyListener
from Database.Listeners.spotifyListener import (
    Listener,
    USER_VALIDATION_ERROR_COOLDOWN_SECONDS,
    _isNoVerdictError,
    classifyListenerError,
)

_MONOTONIC = "Database.Listeners.spotifyListener.time.monotonic"
_LOGGER = "Database.Listeners.spotifyListener"
_T0 = 100_000.0
_ONE_SECOND = 1.0
_OWN_SPOTIFY_ID = "alice@example.com"   #< for these accounts Spotify returns the email as the id


def _sessionClosedRequestError():
    """How spotapi wraps curl_cffi's closed-session error: the detail is in
    .error, str(exc) is the same constant as any other transport failure."""
    from spotapi.exceptions.errors import RequestError
    return RequestError("Failed to complete request.", error="Session is closed, cannot send request.")


def _listener(lastKnownResult=True):
    li = Listener.__new__(Listener)
    li.sp = MagicMock()
    li.sp.isLoggedIn.return_value = True
    li.email = _OWN_SPOTIFY_ID
    li.user = "alice"
    li.contaminationDetected = False
    li.loginFailed = False
    li._authenticated_user_id = _OWN_SPOTIFY_ID
    li._last_user_validation_time = None
    li._last_user_validation_result = lastKnownResult
    li._last_validation_error_time = None
    li.run = False
    li._stop_event = threading.Event()
    return li


class TestIsNoVerdictErrorPredicate(unittest.TestCase):
    def test_a_transport_failure_is_no_verdict(self):
        self.assertTrue(_isNoVerdictError(realTransportRequestError()))

    def test_a_spotify_5xx_is_no_verdict(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertTrue(_isNoVerdictError(realProfileStatusError(status)))

    def test_a_closed_session_is_a_verdict(self):
        """The transport is dead for good - the one RequestError that must keep
        reading as a refusal instead of being retried against forever."""
        self.assertFalse(_isNoVerdictError(_sessionClosedRequestError()))

    def test_a_4xx_is_a_verdict(self):
        """401/403 are refusals; a 429 is the transient branch's business and
        must not be swallowed here into a silent True."""
        for status in (400, 401, 403, 429):
            with self.subTest(status=status):
                self.assertFalse(_isNoVerdictError(realProfileStatusError(status)))

    def test_a_plain_auth_message_is_a_verdict(self):
        self.assertFalse(_isNoVerdictError(RuntimeError("401 Unauthorized")))


class TestIsLoggedInKeepsTheCookieVerdictWithoutOne(unittest.TestCase):
    def _isLoggedInWith(self, exc):
        li = _listener()
        li.sp.current_user.side_effect = exc
        return li.isLoggedIn()

    def test_a_transport_failure_stays_logged_in(self):
        li = _listener()
        li.sp.current_user.side_effect = realTransportRequestError()
        with self.assertLogs(_LOGGER, level="WARNING") as cm:
            self.assertTrue(li.isLoggedIn())
        self.assertIn("keeping the cookie-level verdict", "\n".join(cm.output))

    def test_a_spotify_5xx_stays_logged_in(self):
        for status in (503, 504):
            with self.subTest(status=status):
                self.assertTrue(self._isLoggedInWith(realProfileStatusError(status)))

    def test_a_closed_session_still_reads_as_logged_out(self):
        self.assertFalse(self._isLoggedInWith(_sessionClosedRequestError()))

    def test_a_refusal_still_reads_as_logged_out(self):
        self.assertFalse(self._isLoggedInWith(realProfileStatusError(403)))
        self.assertFalse(self._isLoggedInWith(RuntimeError("401 Unauthorized")))

    def test_the_cookie_level_verdict_still_wins(self):
        """The no-verdict branch only ever runs AFTER sp.isLoggedIn() said yes."""
        li = _listener()
        li.sp.isLoggedIn.return_value = False
        li.sp.current_user.side_effect = realTransportRequestError()
        self.assertFalse(li.isLoggedIn())
        li.sp.current_user.assert_not_called()


class TestValidateCurrentUserKeepsTheLastAnswerWithoutOne(unittest.TestCase):
    def test_a_spotify_5xx_does_not_fail_validation(self):
        """The 32-rebuild case: False here is what makes _checkOnce hand the
        listener to onStale."""
        li = _listener()
        li.sp.current_user.side_effect = realProfileStatusError(503)
        with patch(_MONOTONIC, return_value=_T0):
            with self.assertLogs(_LOGGER, level="WARNING") as cm:
                self.assertTrue(li._validateCurrentUser())
        self.assertIn("keeping the last known answer", "\n".join(cm.output))

    def test_a_transport_failure_does_not_raise_into_the_poll_loop(self):
        li = _listener()
        li.sp.current_user.side_effect = realTransportRequestError()
        with patch(_MONOTONIC, return_value=_T0):
            self.assertTrue(li._validateCurrentUser())

    def test_the_last_known_answer_is_what_stands_not_true(self):
        li = _listener(lastKnownResult=False)
        li.sp.current_user.side_effect = realTransportRequestError()
        with patch(_MONOTONIC, return_value=_T0):
            self.assertFalse(li._validateCurrentUser())

    def test_no_verdict_starts_the_refusal_cooldown(self):
        """The next poll must not walk straight back into the same endpoint:
        no second current_user() call inside USER_VALIDATION_ERROR_COOLDOWN_SECONDS,
        and a fresh one once it has passed."""
        li = _listener()
        li.sp.current_user.side_effect = realProfileStatusError(503)
        with patch(_MONOTONIC, return_value=_T0):
            li._validateCurrentUser()
        li.sp.current_user.side_effect = AssertionError("re-asked inside the refusal cooldown")
        with patch(_MONOTONIC, return_value=_T0 + USER_VALIDATION_ERROR_COOLDOWN_SECONDS - _ONE_SECOND):
            self.assertTrue(li._validateCurrentUser())
        li.sp.current_user.side_effect = None
        li.sp.current_user.return_value = {"id": _OWN_SPOTIFY_ID, "email": _OWN_SPOTIFY_ID}
        with patch(_MONOTONIC, return_value=_T0 + USER_VALIDATION_ERROR_COOLDOWN_SECONDS + _ONE_SECOND):
            self.assertTrue(li._validateCurrentUser())
        self.assertEqual(li.sp.current_user.call_count, 2)

    def test_a_closed_session_still_raises(self):
        """Not a no-verdict: it propagates to startListener like before, whose
        generic branch waits and retries - and the rebuild that follows is the
        only thing that gets a live transport back."""
        from spotapi.exceptions.errors import RequestError
        li = _listener()
        li.sp.current_user.side_effect = _sessionClosedRequestError()
        with patch(_MONOTONIC, return_value=_T0):
            with self.assertRaises(RequestError):
                li._validateCurrentUser()

    def test_a_refusal_still_fails_validation(self):
        li = _listener()
        li.sp.current_user.side_effect = realProfileStatusError(403)
        with patch(_MONOTONIC, return_value=_T0):
            self.assertFalse(li._validateCurrentUser())


class TestClassifierIsNotWidened(unittest.TestCase):
    """Anti-regression: the fix lives in _isNoVerdictError, not in the pair
    that drives startListener's precedence."""

    def test_the_real_transport_error_classifies_as_neither(self):
        self.assertEqual(classifyListenerError(realTransportRequestError()), (False, False))

    def test_start_listener_does_not_open_the_shared_backoff_for_a_transport_failure(self):
        li = _listener()
        li._checkConnectStateForMissedTracks = MagicMock()
        li._checkWebApiBackfill = MagicMock()
        li._checkOnce = MagicMock(side_effect=[realTransportRequestError(), False])
        onStale = MagicMock()
        with patch.object(spotifyListener.SPOTIFY_LIMITER, "applyBackoff") as applyBackoff, \
                patch.object(li._stop_event, "wait", return_value=False), \
                self.assertLogs(_LOGGER, level="ERROR") as cm:
            li.startListener(callback=MagicMock(), onStale=onStale)
        applyBackoff.assert_not_called()
        onStale.assert_not_called()
        self.assertIn("Error in listener", "\n".join(cm.output))   #< the generic wait-and-retry branch
        self.assertEqual(li._checkOnce.call_count, 2)


if __name__ == "__main__":
    unittest.main()
