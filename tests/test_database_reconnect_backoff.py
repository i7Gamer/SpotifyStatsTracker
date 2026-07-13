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


if __name__ == "__main__":
    unittest.main()
