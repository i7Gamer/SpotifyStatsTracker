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
from Database.Listeners.spotifyListener import Listener


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

        db = self._startWithListener(listener)

        self.assertEqual(db.getListenerHealth()["status"], "HEALTHY")
        listener.startListener_thread.assert_called_once()


if __name__ == "__main__":
    unittest.main()
