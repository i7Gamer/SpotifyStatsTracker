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

from Database.Listeners.spotifyListener import Listener, LISTENER_STALE_TIMEOUT_SECONDS


def _bareListener(recentlyPlayed=None):
    listener = Listener.__new__(Listener)
    listener.run = True
    listener.sp = MagicMock()
    listener.recentlyPlayed_Z1 = recentlyPlayed if recentlyPlayed is not None else []
    listener.sp.current_user_recently_played.return_value = listener.recentlyPlayed_Z1
    listener._lastChangeTime = 0.0
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


if __name__ == "__main__":
    unittest.main()
