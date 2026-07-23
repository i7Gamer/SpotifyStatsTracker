"""A failed Spotify login must degrade cleanly, not crash listener startup.

self.sp.startRecentlyPlayedListener() builds a PlayerStatus/WebsocketStreamer
around self.sp.user_auth - spotapi's WebsocketStreamer.__init__ does
`login.logged_in`, which raises AttributeError once user_auth is left as the
plain `False` SpotipyFree's own login() leaves it on failure (cookies
invalid/expired/undecryptable). That crash used to propagate out of
Listener.__init__, through ConnectionManager.startListener(), into
get_user_db()'s except-and-rollback - silently uncaching the user, so admin
showed a generic "Inactive" instead of anything actionable, and the crash
repeated every 5-minute recheck. A failed login must instead be skipped
cleanly and reported the same way contaminationDetected already is (see
tests/test_listener_contamination.py): DEAD health with a reason.
"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from conftest import DatabaseTestCase
from Database.Listeners.spotifyListener import Listener


def _makeListener(loggedIn, expectedEmail="expected@example.com"):
    """A real Listener constructed against a mocked Spotify client whose
    login succeeded (loggedIn=True) or failed (loggedIn=False). On failure,
    startRecentlyPlayedListener is wired to raise the exact error the real
    spotapi/SpotipyFree stack raises when called with an unauthenticated
    client, so a regression (the guard being removed) fails loudly here
    instead of the flag simply not being set."""
    sp = MagicMock()
    sp.isLoggedIn.return_value = loggedIn
    sp.current_user.return_value = {"id": "spotify-user-1", "email": expectedEmail}
    sp.current_user_recently_played.return_value = []
    if not loggedIn:
        sp.startRecentlyPlayedListener.side_effect = AttributeError(
            "'bool' object has no attribute 'logged_in'"
        )
    with patch("Database.Listeners.spotifyListener.Spotify", return_value=sp):
        listener = Listener(cookiesFile="unused.json", email=expectedEmail)
    return listener, sp


class TestLoginFailureDetection(unittest.TestCase):
    def test_successful_login_is_not_flagged(self):
        listener, sp = _makeListener(loggedIn=True)
        self.assertFalse(listener.loginFailed)
        sp.startRecentlyPlayedListener.assert_called_once()

    def test_failed_login_is_flagged_without_starting_the_player_status_listener(self):
        listener, sp = _makeListener(loggedIn=False)
        self.assertTrue(listener.loginFailed)
        sp.startRecentlyPlayedListener.assert_not_called()

    def test_failed_login_does_not_raise(self):
        """Regression test for the original crash: constructing a Listener
        against cookies that fail to authenticate must not raise. sp's
        startRecentlyPlayedListener is wired to raise the real
        AttributeError - this only passes because the fix never calls it."""
        try:
            _makeListener(loggedIn=False)
        except Exception as e:
            self.fail(f"Listener construction raised on a failed login: {e!r}")


class TestLoginFailedListenerHealth(DatabaseTestCase):
    def _startWithListener(self, listener):
        db = self._makeDb({}, [])
        with patch("Database.database.Listener", return_value=listener):
            db.startListener(email="expected@example.com")
        return db

    def test_login_failed_listener_marks_health_dead(self):
        listener = MagicMock()
        listener.contaminationDetected = False
        listener.loginFailed = True

        db = self._startWithListener(listener)

        health = db.getListenerHealth()
        self.assertEqual(health["status"], "DEAD")
        self.assertIn("login failed", health["last_error"])
        listener.startListener_thread.assert_not_called()

    def test_logged_in_listener_still_marks_healthy_and_starts(self):
        listener = MagicMock()
        listener.contaminationDetected = False
        listener.loginFailed = False

        db = self._startWithListener(listener)

        self.assertEqual(db.getListenerHealth()["status"], "HEALTHY")
        listener.startListener_thread.assert_called_once()


if __name__ == "__main__":
    unittest.main()
