"""Tests for the connect-state cross-check that replaces the old REST-API
verification poll.

/v1/me/player/recently-played is a deprecated-for-third-parties public REST
endpoint - calling it with spotapi's web-player-scoped token 429s permanently,
no backoff recovers it (see git history for the removed _pollRestApiHistory*
code). spotapi's LastPlayedManger already re-fetches Spotify Connect state
(PlayerStatus.state, via connect_device()) every refreshInterval tick for its
own current-track detection, and that same state carries `prev_tracks` - the
local queue's play history - at no extra network cost. This cross-checks that
history against what we've already recorded, purely as a diagnostic signal for
catching plays the (known-fragile) websocket cache silently missed.
"""
import collections
import sys
import os
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.Listeners.spotifyListener import Listener, CONNECT_STATE_MISSED_TRACK_CACHE_SIZE


def _bareListener(recentlyPlayed=None):
    listener = Listener.__new__(Listener)
    listener.run = True
    listener.sp = MagicMock()
    listener.recentlyPlayed_Z1 = recentlyPlayed if recentlyPlayed is not None else []
    listener._warnedMissingTrackUris = collections.OrderedDict()
    return listener


def _withConnectState(listener, prevTracks):
    """Wire up listener.sp.lastPlayedManager.manager._state the way
    spotapi's PlayerStatus actually stores it (a raw dict, not the PlayerState
    dataclass - see spotapi/status.py's `_state`)."""
    manager = MagicMock()
    manager._state = {"prev_tracks": prevTracks}
    listener.sp.lastPlayedManager = MagicMock()
    listener.sp.lastPlayedManager.manager = manager


class TestGetRecentTrackUrisFromConnectState(unittest.TestCase):
    def test_no_last_played_manager_returns_none(self):
        listener = _bareListener()
        listener.sp.lastPlayedManager = None

        self.assertIsNone(listener._getRecentTrackUrisFromConnectState())

    def test_no_manager_on_last_played_manager_returns_none(self):
        listener = _bareListener()
        listener.sp.lastPlayedManager = MagicMock()
        listener.sp.lastPlayedManager.manager = None

        self.assertIsNone(listener._getRecentTrackUrisFromConnectState())

    def test_no_state_captured_yet_returns_none(self):
        listener = _bareListener()
        manager = MagicMock()
        manager._state = None
        listener.sp.lastPlayedManager = MagicMock()
        listener.sp.lastPlayedManager.manager = manager

        self.assertIsNone(listener._getRecentTrackUrisFromConnectState())

    def test_extracts_uris_from_prev_tracks(self):
        listener = _bareListener()
        _withConnectState(listener, [
            {"uri": "spotify:track:aaa"},
            {"uri": "spotify:track:bbb"},
        ])

        self.assertEqual(
            listener._getRecentTrackUrisFromConnectState(),
            ["spotify:track:aaa", "spotify:track:bbb"],
        )

    def test_tracks_without_uri_are_skipped(self):
        listener = _bareListener()
        _withConnectState(listener, [
            {"uri": "spotify:track:aaa"},
            {"uri": None},
            {},
        ])

        self.assertEqual(listener._getRecentTrackUrisFromConnectState(), ["spotify:track:aaa"])


class TestCheckConnectStateForMissedTracks(unittest.TestCase):
    def _recordedItem(self, trackId):
        return {"track": {"track_id": trackId, "id": trackId}, "played_at": "t"}

    def test_logs_warning_for_track_missing_from_recorded_history(self):
        listener = _bareListener(recentlyPlayed=[self._recordedItem("aaa")])
        _withConnectState(listener, [{"uri": "spotify:track:aaa"}, {"uri": "spotify:track:bbb"}])

        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm:
            listener._checkConnectStateForMissedTracks()

        self.assertTrue(any("bbb" in message for message in cm.output))

    def test_does_not_warn_when_all_tracks_already_recorded(self):
        listener = _bareListener(recentlyPlayed=[self._recordedItem("aaa"), self._recordedItem("bbb")])
        _withConnectState(listener, [{"uri": "spotify:track:aaa"}, {"uri": "spotify:track:bbb"}])

        # assertNoLogs isn't available on all supported Python versions - assert
        # directly on the dedup cache instead, which only grows on a warning.
        listener._checkConnectStateForMissedTracks()
        self.assertEqual(len(listener._warnedMissingTrackUris), 0)

    def test_does_not_warn_twice_for_the_same_missing_track(self):
        listener = _bareListener(recentlyPlayed=[])
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm:
            listener._checkConnectStateForMissedTracks()
        firstCallWarnings = len(cm.output)

        # Second call: same missing track, already warned about - must not log again.
        # (assertLogs requires at least one log record, so trigger a second,
        # unrelated warning to keep the context manager happy while asserting
        # the dedup behavior via call count instead of via assertLogs itself.)
        import logging
        logger = logging.getLogger("Database.Listeners.spotifyListener")
        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm2:
            listener._checkConnectStateForMissedTracks()
            logger.warning("sentinel")

        self.assertEqual(len(cm2.output), 1)  # only the sentinel, not a repeat warning
        self.assertEqual(firstCallWarnings, 1)

    def test_no_connect_state_available_does_not_raise(self):
        listener = _bareListener(recentlyPlayed=[])
        listener.sp.lastPlayedManager = None

        listener._checkConnectStateForMissedTracks()  # must not raise

    def test_internal_exception_is_swallowed(self):
        """This is a diagnostic side-channel - a bug here must never take down
        the primary polling loop."""
        class _RaisingLastPlayedManager:
            @property
            def manager(self):
                raise RuntimeError("boom")

        listener = _bareListener(recentlyPlayed=[])
        listener.sp.lastPlayedManager = _RaisingLastPlayedManager()

        listener._checkConnectStateForMissedTracks()  # must not raise

    def test_dedup_cache_is_bounded(self):
        listener = _bareListener(recentlyPlayed=[])
        listener._warnedMissingTrackUris = collections.OrderedDict.fromkeys(
            f"spotify:track:{i}" for i in range(CONNECT_STATE_MISSED_TRACK_CACHE_SIZE)
        )
        _withConnectState(listener, [{"uri": "spotify:track:new"}])

        listener._checkConnectStateForMissedTracks()

        self.assertLessEqual(len(listener._warnedMissingTrackUris), CONNECT_STATE_MISSED_TRACK_CACHE_SIZE)

    def test_dedup_cache_evicts_oldest_entry_first(self):
        """set.pop() would evict an arbitrary element; the OrderedDict-based
        cache must specifically evict the OLDEST entry (FIFO), so recently
        warned-about tracks are never forgotten ahead of older ones."""
        listener = _bareListener(recentlyPlayed=[])
        listener._warnedMissingTrackUris = collections.OrderedDict.fromkeys(
            f"spotify:track:{i}" for i in range(CONNECT_STATE_MISSED_TRACK_CACHE_SIZE)
        )
        _withConnectState(listener, [{"uri": "spotify:track:new"}])

        listener._checkConnectStateForMissedTracks()

        self.assertNotIn("spotify:track:0", listener._warnedMissingTrackUris)
        for i in range(1, CONNECT_STATE_MISSED_TRACK_CACHE_SIZE):
            self.assertIn(f"spotify:track:{i}", listener._warnedMissingTrackUris)
        self.assertIn("spotify:track:new", listener._warnedMissingTrackUris)


if __name__ == "__main__":
    unittest.main()
