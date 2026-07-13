"""Tests for graceful shutdown of the Listener's background threads.

Left running, spotapi's own LastPlayed background thread can hit a rate-limited
or malformed response while the interpreter is shutting down, producing spurious
errors (KeyError: 'devices') and daemon-thread-join noise on close. stop() must
also stop that thread, bounded by a timeout so shutdown can't hang forever.
"""
import threading
import unittest
from unittest.mock import MagicMock

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.database import Database
from Database.Listeners.spotifyListener import Listener


def _bareListener():
    listener = Listener.__new__(Listener)
    listener.run = True
    listener.sp = MagicMock()
    return listener


class TestListenerStop(unittest.TestCase):
    def test_stop_sets_run_false(self):
        listener = _bareListener()
        listener.sp.lastPlayedManager = None
        listener.stop()
        self.assertFalse(listener.run)

    def test_stop_is_safe_when_last_played_manager_missing(self):
        """No underlying listener was ever started (e.g. sp is a bare mock)."""
        listener = _bareListener()
        del listener.sp.lastPlayedManager  # getattr(..., None) must handle this
        listener.stop()  # should not raise

    def test_stop_stops_and_joins_last_played_manager_thread(self):
        listener = _bareListener()
        lastPlayedManager = MagicMock()
        lastPlayedManager.run = True
        mockThread = MagicMock(spec=threading.Thread)
        mockThread.is_alive.return_value = True
        lastPlayedManager.thread = mockThread
        listener.sp.lastPlayedManager = lastPlayedManager

        listener.stop()

        self.assertFalse(lastPlayedManager.run)
        mockThread.join.assert_called_once()
        self.assertIn("timeout", mockThread.join.call_args.kwargs)

    def test_stop_does_not_join_dead_thread(self):
        listener = _bareListener()
        lastPlayedManager = MagicMock()
        lastPlayedManager.run = True
        mockThread = MagicMock(spec=threading.Thread)
        mockThread.is_alive.return_value = False
        lastPlayedManager.thread = mockThread
        listener.sp.lastPlayedManager = lastPlayedManager

        listener.stop()

        mockThread.join.assert_not_called()


def _bareDatabase():
    db = Database.__new__(Database)
    db.autoImporter = MagicMock()
    return db


class TestDatabaseStop(unittest.TestCase):
    def test_stop_stops_listener_and_auto_importer(self):
        db = _bareDatabase()
        db.listener = MagicMock()

        db.stop()

        db.listener.stop.assert_called_once()
        db.autoImporter.wd.stop.assert_called_once()

    def test_stop_is_safe_when_listener_never_started(self):
        db = _bareDatabase()
        db.listener = None

        db.stop()  # should not raise

        db.autoImporter.wd.stop.assert_called_once()

    def test_startListener_stops_existing_listener(self):
        db = _bareDatabase()
        db.cookiesFile = "test_cookies.json"
        db.email = "test@example.com"
        db.user = "testuser"
        db.getUserSpotifyCredentials = MagicMock(return_value=None)

        # Mock _withCookiesFile to just return a dummy mock listener instead of instantiating real Listener
        mock_new_listener = MagicMock()
        db._withCookiesFile = MagicMock(return_value=mock_new_listener)

        # Set an existing mocked listener
        mock_old_listener = MagicMock()
        db.listener = mock_old_listener

        # Mock startListener_thread so we don't try to spawn a real thread
        mock_new_listener.startListener_thread = MagicMock()

        # Mock _health_lock and internal attributes startListener modifies
        db._health_lock = MagicMock()
        db._addToDatabaseFromListener = MagicMock()
        db._makeOnStaleCallback = MagicMock()
        db._reconcileWithWebApiHistory = MagicMock()

        # Invoke startListener
        db.startListener()

        # Verify old listener was stopped
        mock_old_listener.stop.assert_called_once()
        self.assertIs(db.listener, mock_new_listener)


if __name__ == "__main__":
    unittest.main()
