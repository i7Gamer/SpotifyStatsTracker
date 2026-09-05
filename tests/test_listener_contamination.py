"""Cookie contamination must BLOCK recording, not just log.

If the stored cookies authenticate as a different Spotify account than the
one they're stored under, every play the listener records lands in the wrong
user's history (and that account's listening leaks to the wrong user). The
old behavior logged a CRITICAL line and kept recording anyway - worse, the
ongoing session validation baselined itself on the wrong account's id, so it
never caught the mismatch either. A contaminated listener must refuse to
record, report itself as not logged in (forcing the re-login flow, whose
cookie verification requires a matching account), and surface as DEAD in the
listener health shown to the user.
"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from conftest import DatabaseTestCase
from Database.Listeners.spotifyListener import (
    Listener,
    STALE_REASON_VALIDATION_FAILED,
    USER_VALIDATION_CACHE_SECONDS,
    WEB_API_POLL_INTERVAL_SECONDS,
)

_MONOTONIC = "Database.Listeners.spotifyListener.time.monotonic"
_T0 = 100_000.0
_ONE_SECOND = 1.0


def _makeListener(cookieAccountEmail, expectedEmail="expected@example.com"):
    """A real Listener constructed against a mocked Spotify client whose
    session authenticates as `cookieAccountEmail`."""
    sp = MagicMock()
    sp.current_user.return_value = {"id": "spotify-user-1", "email": cookieAccountEmail}
    sp.current_user_recently_played.return_value = []
    sp.isLoggedIn.return_value = True
    with patch("Database.Listeners.spotifyListener.Spotify", return_value=sp):
        listener = Listener(cookiesFile="unused.json", email=expectedEmail)
    return listener, sp


class TestContaminationDetection(unittest.TestCase):
    def test_matching_email_is_not_flagged(self):
        listener, _ = _makeListener("expected@example.com")
        self.assertFalse(listener.contaminationDetected)

    def test_email_comparison_is_case_insensitive(self):
        listener, _ = _makeListener("Expected@Example.com")
        self.assertFalse(listener.contaminationDetected)

    def test_mismatched_email_is_flagged(self):
        listener, _ = _makeListener("intruder@example.com")
        self.assertTrue(listener.contaminationDetected)

    def test_no_expected_email_is_not_flagged(self):
        """Manual/dev construction without an email has nothing to compare
        against - must not be treated as contaminated."""
        listener, _ = _makeListener("whoever@example.com", expectedEmail=None)
        self.assertFalse(listener.contaminationDetected)

    def test_profile_without_email_is_not_flagged(self):
        """A profile response missing the email field can't prove a mismatch."""
        listener, _ = _makeListener("")
        self.assertFalse(listener.contaminationDetected)

    def test_profile_fetch_failure_is_not_flagged(self):
        """A network error during init verification is not proof of
        contamination - the pre-fix behavior (log a warning, carry on) stays."""
        sp = MagicMock()
        sp.current_user.side_effect = RuntimeError("network down")
        sp.current_user_recently_played.return_value = []
        with patch("Database.Listeners.spotifyListener.Spotify", return_value=sp):
            listener = Listener(cookiesFile="unused.json", email="expected@example.com")
        self.assertFalse(listener.contaminationDetected)


class TestContaminationBlocksRecording(unittest.TestCase):
    def test_contaminated_listener_reports_not_logged_in(self):
        """isLoggedIn() False is what routes the user back through the login
        flow, whose cookie verification demands the matching account."""
        listener, _ = _makeListener("intruder@example.com")
        self.assertFalse(listener.isLoggedIn())

    def test_clean_listener_still_reports_logged_in(self):
        listener, _ = _makeListener("expected@example.com")
        self.assertTrue(listener.isLoggedIn())

    def test_contaminated_listener_refuses_to_poll(self):
        listener, sp = _makeListener("intruder@example.com")
        callback = MagicMock()

        listener.startListener(callback)

        callback.assert_not_called()
        sp.current_user_recently_played.assert_called_once()  #< only the __init__ snapshot
        self.assertFalse(listener.run)


class TestIsLoggedInTransientErrors(unittest.TestCase):
    """Spotify sometimes answers current_user() with a non-JSON fallback/
    bot-check page (e.g. an "Oh nein!" HTML error page) instead of the
    profile. isLoggedIn() must treat that like _validateCurrentUser/
    _checkOnce already do - as transient - rather than bouncing a validly
    logged-in user back through the login flow."""

    def test_transient_json_error_still_reports_logged_in(self):
        listener, sp = _makeListener("expected@example.com")
        sp.current_user.side_effect = RuntimeError(
            "Invalid JSON (Status: 200, Type: str, Response: <!DOCTYPE html>...)"
        )
        self.assertTrue(listener.isLoggedIn())

    def test_rate_limit_error_still_reports_logged_in(self):
        listener, sp = _makeListener("expected@example.com")
        sp.current_user.side_effect = RuntimeError("429 Too Many Requests")
        self.assertTrue(listener.isLoggedIn())

    def test_non_transient_error_reports_not_logged_in(self):
        listener, sp = _makeListener("expected@example.com")
        sp.current_user.side_effect = RuntimeError("401 Unauthorized")
        self.assertFalse(listener.isLoggedIn())


class TestContaminatedListenerHealth(DatabaseTestCase):
    def _startWithListener(self, listener):
        db = self._makeDb({}, [])
        with patch("Database.database.Listener", return_value=listener):
            db.startListener(email="expected@example.com")
        return db

    def test_contaminated_listener_marks_health_dead(self):
        listener = MagicMock()
        listener.contaminationDetected = True

        db = self._startWithListener(listener)

        health = db.getListenerHealth()
        self.assertEqual(health["status"], "DEAD")
        self.assertIn("different Spotify account", health["last_error"])
        listener.startListener_thread.assert_not_called()

    def test_clean_listener_marks_health_healthy_and_starts(self):
        listener = MagicMock()
        listener.contaminationDetected = False
        listener.loginFailed = False

        db = self._startWithListener(listener)

        self.assertEqual(db.getListenerHealth()["status"], "HEALTHY")
        listener.startListener_thread.assert_called_once()


if __name__ == "__main__":
    unittest.main()


def _listenerWhoseBuildTimeCheckRaised(expectedEmail="expected@example.com"):
    """A real Listener whose __init__ identity check met a bot-check page:
    no baseline, no flag, and (before 2026-09-05) no guard for life."""
    sp = MagicMock()
    # The profile call raises the way it did on live, 13 times.
    sp.current_user.side_effect = RuntimeError("Invalid JSON (Status: 200, Type: str, Response: <html>Oh nein!</html>)")
    sp.current_user_recently_played.return_value = []
    sp.isLoggedIn.return_value = True
    with patch("Database.Listeners.spotifyListener.Spotify", return_value=sp):
        listener = Listener(cookiesFile="unused.json", email=expectedEmail)
    sp.current_user.reset_mock()   #< the build-time call is not the one under test
    sp.current_user.side_effect = None
    return listener, sp


class TestBaselineAdoptedByTheFirstValidation(unittest.TestCase):
    """When __init__'s identity check raised, _validateCurrentUser compared
    every later answer against None - i.e. passed all of them, whoever they
    named - for the listener's whole life (2026-09-05 review, L2). Its first
    successful answer now becomes the baseline, through the same email
    comparison __init__ makes."""

    def test_build_time_failure_leaves_no_baseline(self):
        listener, _ = _listenerWhoseBuildTimeCheckRaised()
        self.assertIsNone(listener._authenticated_user_id)
        self.assertFalse(listener.contaminationDetected)

    def test_a_mismatching_session_email_is_contamination(self):
        listener, sp = _listenerWhoseBuildTimeCheckRaised()
        sp.current_user.return_value = {"id": "intruder", "email": "someone-else@example.com"}
        with patch(_MONOTONIC, return_value=_T0):
            self.assertFalse(listener._validateCurrentUser())
        self.assertTrue(listener.contaminationDetected)
        self.assertFalse(listener.isLoggedIn())   #< the re-login flow takes it from here

    def test_a_mismatch_hands_the_loop_to_on_stale(self):
        """False from validation is what _checkOnce turns into the reconnect,
        whose rebuilt listener runs __init__'s own check and refuses to record."""
        listener, sp = _listenerWhoseBuildTimeCheckRaised()
        sp.current_user.return_value = {"id": "intruder", "email": "someone-else@example.com"}
        onStale = MagicMock()
        with patch(_MONOTONIC, return_value=_T0):
            self.assertFalse(listener._checkOnce(MagicMock(), onStale))
        onStale.assert_called_once_with(reason=STALE_REASON_VALIDATION_FAILED)

    def test_a_matching_answer_becomes_the_baseline_and_guards_from_then_on(self):
        listener, sp = _listenerWhoseBuildTimeCheckRaised()
        sp.current_user.return_value = {"id": "spotify-user-1", "email": "Expected@Example.com"}
        with patch(_MONOTONIC, return_value=_T0):
            self.assertTrue(listener._validateCurrentUser())
        self.assertEqual(listener._authenticated_user_id, "spotify-user-1")
        self.assertFalse(listener.contaminationDetected)

        sp.current_user.return_value = {"id": "someone-else", "email": "expected@example.com"}
        with patch(_MONOTONIC, return_value=_T0 + USER_VALIDATION_CACHE_SECONDS + _ONE_SECOND):
            self.assertFalse(listener._validateCurrentUser())   #< the id guard has a baseline now

    def test_a_session_without_an_email_is_adopted_without_a_verdict(self):
        """Spotify can return "email": null - the same rule __init__ applies:
        only a real, non-empty string email is proof of a mismatch."""
        for sessionEmail in (None, ""):
            with self.subTest(sessionEmail=sessionEmail):
                listener, sp = _listenerWhoseBuildTimeCheckRaised()
                sp.current_user.return_value = {"id": "spotify-user-1", "email": sessionEmail}
                with patch(_MONOTONIC, return_value=_T0):
                    self.assertTrue(listener._validateCurrentUser())
                self.assertEqual(listener._authenticated_user_id, "spotify-user-1")
                self.assertFalse(listener.contaminationDetected)

    def test_the_credentialed_path_still_asks_the_profile_endpoint_nothing(self):
        """Regression pin: the Web API /v1/me arm stamps the same cache
        (_recordExternalIdentityCheck) and polls more often than it expires,
        so a credentialed listener keeps not touching www.spotify.com - the
        adoption above runs only when the cookie check actually runs."""
        listener, sp = _listenerWhoseBuildTimeCheckRaised()
        sp.current_user.side_effect = AssertionError("profile endpoint asked while on cooldown")
        listener._recordExternalIdentityCheck(_T0)
        for poll in range(1, 4):
            now = _T0 + poll * WEB_API_POLL_INTERVAL_SECONDS
            with patch(_MONOTONIC, return_value=now):
                self.assertTrue(listener._validateCurrentUser())
            listener._recordExternalIdentityCheck(now)
        sp.current_user.assert_not_called()
