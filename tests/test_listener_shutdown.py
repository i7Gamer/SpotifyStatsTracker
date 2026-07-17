"""Tests for graceful shutdown of the Listener's background threads.

Left running, spotapi's own LastPlayed background thread can hit a rate-limited
or malformed response while the interpreter is shutting down, producing spurious
errors (KeyError: 'devices') and daemon-thread-join noise on close. stop() must
also stop that thread, bounded by a timeout so shutdown can't hang forever.
"""
import threading
import unittest
from unittest.mock import MagicMock, patch

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


class TestListenerSignalStop(unittest.TestCase):
    """signalStop() is the signal-only half of stop(): every stop flag flips,
    but nothing joins and no socket closes - shutdown's phase 1 calls it for
    every user before any join blocks."""

    def test_signal_stop_sets_flags_without_joining_or_closing(self):
        listener = _bareListener()
        listener._stop_event = threading.Event()
        lastPlayedManager = MagicMock()
        lastPlayedManager.run = True
        mockThread = MagicMock(spec=threading.Thread)
        mockThread.is_alive.return_value = True
        lastPlayedManager.thread = mockThread
        listener.sp.lastPlayedManager = lastPlayedManager

        listener.signalStop()

        self.assertFalse(listener.run)
        self.assertTrue(listener._stop_event.is_set())
        self.assertFalse(lastPlayedManager.run)
        self.assertTrue(lastPlayedManager.manager._deliberate_close)
        mockThread.join.assert_not_called()
        lastPlayedManager.manager.ws.close.assert_not_called()

    def test_signal_stop_safe_without_last_played_manager(self):
        listener = _bareListener()
        listener.sp.lastPlayedManager = None

        listener.signalStop()  # should not raise

        self.assertFalse(listener.run)

    def test_stop_still_closes_websocket_and_marks_deliberate(self):
        """stop() = signalStop() + the close/join half; the refactor must keep
        closing the websocket and flagging the close as deliberate."""
        listener = _bareListener()
        lastPlayedManager = MagicMock()
        lastPlayedManager.run = True
        mockThread = MagicMock(spec=threading.Thread)
        mockThread.is_alive.return_value = True
        lastPlayedManager.thread = mockThread
        listener.sp.lastPlayedManager = lastPlayedManager

        listener.stop()

        self.assertTrue(lastPlayedManager.manager._deliberate_close)
        lastPlayedManager.manager.ws.close.assert_called_once()
        mockThread.join.assert_called_once()


def _bareDatabase():
    db = Database.__new__(Database)
    db.user = "testuser"
    db.autoImporter = MagicMock()
    db.listener = None
    db._stopping = False
    db.shutdown_event = threading.Event()
    db._listener_lock = threading.Lock()
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
        result = db.startListener()

        # Verify old listener was stopped
        mock_old_listener.stop.assert_called_once()
        self.assertIs(db.listener, mock_new_listener)
        self.assertIs(result, True)


class TestDatabaseSignalStop(unittest.TestCase):
    """Database.signalStop() flips every stop flag/event for this user without
    joining anything - shutdown's phase 1."""

    def test_signal_stop_signals_everything_without_joining(self):
        db = _bareDatabase()
        db.listener = MagicMock()
        db.backfiller_stop_event = threading.Event()
        db.wrapped_stop_event = threading.Event()
        db.lastfm_stop_event = threading.Event()

        db.signalStop()

        self.assertTrue(db._stopping)
        db.listener.signalStop.assert_called_once()
        db.listener.stop.assert_not_called()
        db.autoImporter.wd.signalStop.assert_called_once()
        db.autoImporter.wd.stop.assert_not_called()
        self.assertTrue(db.backfiller_stop_event.is_set())
        self.assertTrue(db.wrapped_stop_event.is_set())
        self.assertTrue(db.lastfm_stop_event.is_set())

    def test_signal_stop_safe_without_listener_or_worker_events(self):
        db = _bareDatabase()  #< bare: no worker stop events, no listener

        db.signalStop()  # should not raise

        self.assertTrue(db._stopping)


class TestStartListenerShutdownGate(unittest.TestCase):
    """startListener must refuse to (re)connect once stop was requested, and
    must tear down a freshly-built listener when stop arrived while the slow
    Spotify login was in flight - otherwise a stale-feed reconnect can
    resurrect a listener mid-shutdown that nothing can reach afterwards."""

    def test_stop_sets_stopping_and_refuses_future_startListener(self):
        db = _bareDatabase()

        db.stop()

        self.assertTrue(db._stopping)
        db._withCookiesFile = MagicMock()
        self.assertIs(db.startListener(), False)
        db._withCookiesFile.assert_not_called()

    def test_startListener_refuses_when_shutdown_event_set(self):
        db = _bareDatabase()
        db.shutdown_event.set()
        db._withCookiesFile = MagicMock()

        self.assertIs(db.startListener(), False)

        db._withCookiesFile.assert_not_called()

    def test_startListener_discards_fresh_listener_when_stop_arrives_mid_login(self):
        db = _bareDatabase()
        db.cookiesFile = "test_cookies.json"
        db.email = "test@example.com"
        db.getUserSpotifyCredentials = MagicMock(return_value=None)
        mock_new_listener = MagicMock()

        def buildListenerAndReceiveStop(_factory):
            db._stopping = True  #< stop() gave up on the lock while we were logging in
            return mock_new_listener

        db._withCookiesFile = MagicMock(side_effect=buildListenerAndReceiveStop)

        self.assertIs(db.startListener(), False)

        mock_new_listener.stop.assert_called_once()
        mock_new_listener.startListener_thread.assert_not_called()
        self.assertIsNone(db.listener)

    def test_stop_proceeds_when_listener_lock_is_held_elsewhere(self):
        """An in-flight startListener holds the listener lock through a live
        Spotify login (~15s) - stop() must give up on the lock after a bounded
        wait and still stop the current listener rather than deadlock."""
        db = _bareDatabase()
        db.listener = MagicMock()
        db.LISTENER_STOP_LOCK_TIMEOUT_SECONDS = 0.05  #< instance override keeps the test fast
        db._listener_lock.acquire()  #< simulate the in-flight startListener
        try:
            db.stop()  # must return, not deadlock
        finally:
            db._listener_lock.release()

        db.listener.stop.assert_called_once()


class TestAppShutdownTwoPhase(unittest.TestCase):
    """shutdown() must SIGNAL every user's stop flags before JOINING any user's
    threads - while user A's threads were being joined, user B's still-running
    listener used to hit its stale-feed check and resurrect itself
    mid-shutdown (the 2026-07-17 hang)."""

    def _bareApp(self):
        from app import SpotifyDashboardApp
        dash = SpotifyDashboardApp.__new__(SpotifyDashboardApp)
        dash._stop_event = threading.Event()
        dash.backupWorker = MagicMock()
        dash._db_lock = threading.Lock()
        dash.user_databases = {}
        dash._activatedUsers = set()
        return dash

    def test_signals_all_users_before_joining_any(self):
        dash = self._bareApp()
        order = []
        for name in ("alice", "bob"):
            db = MagicMock()
            db.user = name
            db.signalStop.side_effect = lambda n=name: order.append(("signal", n))
            db.stop.side_effect = lambda n=name: order.append(("stop", n))
            dash.user_databases[name] = db

        dash.shutdown()

        self.assertTrue(dash._stop_event.is_set())
        dash.backupWorker.stop.assert_called_once()
        self.assertEqual(order, [("signal", "alice"), ("signal", "bob"),
                                 ("stop", "alice"), ("stop", "bob")])

    def test_one_failing_user_does_not_block_the_rest(self):
        dash = self._bareApp()
        bad = MagicMock()
        bad.user = "bad"
        bad.signalStop.side_effect = RuntimeError("boom-signal")
        bad.stop.side_effect = RuntimeError("boom-stop")
        good = MagicMock()
        good.user = "good"
        dash.user_databases = {"bad": bad, "good": good}

        dash.shutdown()  # must not raise

        good.signalStop.assert_called_once()
        good.stop.assert_called_once()

    def test_get_user_db_shares_shutdown_event_with_app(self):
        """Databases must observe the app-wide stop event, or the reconnect
        gates never fire."""
        dash = self._bareApp()
        with patch("app.Database") as MockDatabase:
            MockDatabase.return_value = MagicMock()
            dash.get_user_db("alice", "alice@example.com")

        self.assertIs(MockDatabase.call_args.kwargs.get("shutdown_event"),
                      dash._stop_event)


if __name__ == "__main__":
    unittest.main()
