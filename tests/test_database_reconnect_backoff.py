"""Tests for Database listener reconnection with exponential backoff.

When a listener's onStale callback is triggered (due to stale feed or auth error),
the reconnection should retry with exponential backoff before giving up. This tests
that backoff behavior and proper error logging.
"""
import sys
import os
import unittest
import time
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.database import Database


class TestReconnectBackoff(unittest.TestCase):
    def _makeTestDb(self):
        """Create a Database instance with mocked repository and file operations."""
        with patch('Database.database.Repository'), \
             patch('Database.database.AutoImporter'), \
             patch('Database.database.Path.exists', return_value=False), \
             patch.dict(os.environ, {}, clear=False):
            db = Database(user="TestUser", email="test@example.com")
        self.addCleanup(db.stop)
        return db

    def test_exponential_backoff_calculation(self):
        """Verify exponential backoff delay calculation is correct."""
        db = self._makeTestDb()

        # Verify delay calculation logic without actually sleeping
        for attempt in range(db.RECONNECT_MAX_RETRIES):
            delay = min(
                db.RECONNECT_INITIAL_DELAY * (2 ** attempt),
                db.RECONNECT_MAX_DELAY
            )
            # First attempt should be 1s, then 2s, 4s, 8s, etc, capped at 300s
            if attempt == 0:
                self.assertEqual(delay, 1)
            elif attempt == 1:
                self.assertEqual(delay, 2)
            elif attempt == 2:
                self.assertEqual(delay, 4)
            # Later attempts should be capped
            self.assertLessEqual(delay, db.RECONNECT_MAX_DELAY)

    def test_startListener_uses_onStale_callback_with_backoff(self):
        """startListener should use the wrapped onStale callback with backoff."""
        db = self._makeTestDb()

        with patch.object(db, '_withCookiesFile') as mock_cookies, \
             patch('Database.database.Listener') as MockListener:
            mock_listener = MagicMock()
            mock_listener.contaminationDetected = False  #< a bare MagicMock's auto-attribute is truthy = contaminated
            mock_listener.loginFailed = False  #< same trap - see above
            MockListener.return_value = mock_listener
            mock_cookies.return_value = mock_listener

            db.startListener(email="test@example.com")

            # Verify startListener_thread was called with onStale callback
            mock_listener.startListener_thread.assert_called_once()
            call_kwargs = mock_listener.startListener_thread.call_args[1]
            self.assertIn('onStale', call_kwargs)

            # The onStale callback should be callable (it's the result of _makeOnStaleCallback)
            onStale_callback = call_kwargs['onStale']
            self.assertTrue(callable(onStale_callback))


class TestReconnectShutdownGate(unittest.TestCase):
    """A stale-feed reconnect racing shutdown used to resurrect a listener
    nothing could reach (the 2026-07-17 hang) - onStale must abandon
    reconnection as soon as stop/shutdown is requested."""

    def _makeTestDb(self):
        with patch('Database.database.Repository'), \
             patch('Database.database.AutoImporter'), \
             patch('Database.database.Path.exists', return_value=False), \
             patch.dict(os.environ, {}, clear=False):
            db = Database(user="TestUser", email="test@example.com")
        self.addCleanup(db.stop)
        return db

    def test_onstale_aborts_immediately_when_shutting_down(self):
        db = self._makeTestDb()
        db.shutdown_event.set()

        with patch.object(db, "startListener") as mockStart:
            db._makeOnStaleCallback()()

        mockStart.assert_not_called()

    def test_onstale_aborts_when_stopping(self):
        db = self._makeTestDb()
        db._stopping = True

        with patch.object(db, "startListener") as mockStart:
            db._makeOnStaleCallback()()

        mockStart.assert_not_called()

    def test_onstale_abandons_when_startListener_reports_stop(self):
        """startListener returning False means 'stop requested' - no retries."""
        db = self._makeTestDb()

        with patch.object(db, "startListener", return_value=False) as mockStart:
            db._makeOnStaleCallback()()

        mockStart.assert_called_once()

    def test_onstale_backoff_waits_on_shutdown_event(self):
        """The between-attempt backoff must wait on shutdown_event
        (interruptible) and abandon reconnection when it fires - not sleep out
        up to RECONNECT_MAX_DELAY and reconnect anyway."""
        db = self._makeTestDb()
        db.shutdown_event = MagicMock()
        db.shutdown_event.is_set.return_value = False
        db.shutdown_event.wait.return_value = True  #< "shutdown arrived mid-wait"

        with patch.object(db, "startListener",
                          side_effect=RuntimeError("still down")) as mockStart, \
             patch("Database.database.time.sleep") as mockSleep:
            db._makeOnStaleCallback()()

        mockStart.assert_called_once()               # attempt 1 failed...
        db.shutdown_event.wait.assert_called_once()  # ...the backoff waited on the event...
        mockSleep.assert_not_called()                # ...never via a blind sleep


if __name__ == "__main__":
    unittest.main()
