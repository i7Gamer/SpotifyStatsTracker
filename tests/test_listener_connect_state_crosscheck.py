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


def _bareListener(recentlyPlayed=None, get_recorded_track_ids=None):
    listener = Listener.__new__(Listener)
    listener.run = True
    listener.sp = MagicMock()
    listener.recentlyPlayed_Z1 = recentlyPlayed if recentlyPlayed is not None else []
    listener._settledMissingTrackUris = collections.OrderedDict()
    listener.get_recorded_track_ids = get_recorded_track_ids
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

    def test_non_track_uris_are_filtered_out(self):
        """prev_tracks also carries queue markers (spotify:delimiter,
        spotify:meta:*) - they are not tracks and must not be returned."""
        listener = _bareListener()
        _withConnectState(listener, [
            {"uri": "spotify:track:aaa"},
            {"uri": "spotify:delimiter"},
            {"uri": "spotify:meta:node_rules_placeholder"},
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
        self.assertEqual(len(listener._settledMissingTrackUris), 0)

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

    def test_non_track_uris_never_warn(self):
        """Queue markers (spotify:delimiter, spotify:meta:*) can never match a
        recorded play - warning about them (as happened before the filter)
        just repeats forever across reconnects."""
        listener = _bareListener(recentlyPlayed=[])
        _withConnectState(listener, [
            {"uri": "spotify:delimiter"},
            {"uri": "spotify:meta:node_rules_placeholder"},
            {"uri": "spotify:track:bbb"},
        ])

        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm:
            listener._checkConnectStateForMissedTracks()

        self.assertEqual(len(cm.output), 1)
        self.assertIn("spotify:track:bbb", cm.output[0])
        self.assertNotIn("delimiter", cm.output[0])
        self.assertNotIn("spotify:meta", cm.output[0])

    def test_only_non_track_uris_produces_no_warning(self):
        listener = _bareListener(recentlyPlayed=[])
        _withConnectState(listener, [
            {"uri": "spotify:delimiter"},
            {"uri": "spotify:meta:node_rules_placeholder"},
        ])

        listener._checkConnectStateForMissedTracks()  # must not raise

        self.assertEqual(len(listener._settledMissingTrackUris), 0)

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
        listener._settledMissingTrackUris = collections.OrderedDict.fromkeys(
            f"spotify:track:{i}" for i in range(CONNECT_STATE_MISSED_TRACK_CACHE_SIZE)
        )
        _withConnectState(listener, [{"uri": "spotify:track:new"}])

        listener._checkConnectStateForMissedTracks()

        self.assertLessEqual(len(listener._settledMissingTrackUris), CONNECT_STATE_MISSED_TRACK_CACHE_SIZE)

    def test_db_is_asked_about_a_given_track_only_once(self):
        """The poll loop calls this every second (_stop_event.wait(1)), and a
        track the DB vouches for is a permanent miss against the in-memory
        cache - so without recording the answer it would be re-queried ~1,800
        times per listener per idle cycle, per user. The database answer has to
        settle the URI just as a warning does."""
        recorded = MagicMock(return_value={"bbb"})
        listener = _bareListener(recentlyPlayed=[], get_recorded_track_ids=recorded)
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        for _ in range(5):
            listener._checkConnectStateForMissedTracks()

        recorded.assert_called_once_with(["bbb"])

    def test_settled_cache_stays_bounded_for_db_confirmed_tracks(self):
        """Same FIFO bound as the warned entries - a long queue of
        already-recorded tracks must not grow the cache without limit."""
        recorded = MagicMock(side_effect=lambda ids: set(ids))
        listener = _bareListener(recentlyPlayed=[], get_recorded_track_ids=recorded)
        _withConnectState(listener, [
            {"uri": f"spotify:track:{i}"} for i in range(CONNECT_STATE_MISSED_TRACK_CACHE_SIZE + 10)
        ])

        listener._checkConnectStateForMissedTracks()

        self.assertLessEqual(len(listener._settledMissingTrackUris),
                             CONNECT_STATE_MISSED_TRACK_CACHE_SIZE)

    def test_failed_db_lookup_does_not_settle_the_uri(self):
        """A lookup that errored answered nothing - suppressing on it would
        turn a transient database problem into permanent silence about a
        possibly-missed play."""
        recorded = MagicMock(side_effect=RuntimeError("db gone"))
        listener = _bareListener(recentlyPlayed=[], get_recorded_track_ids=recorded)
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING"):
            listener._checkConnectStateForMissedTracks()
        # the warning itself settles it, but the lookup must be retried for a
        # track that is still unaccounted for after the cache is cleared
        listener._settledMissingTrackUris.clear()
        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING"):
            listener._checkConnectStateForMissedTracks()

        self.assertEqual(recorded.call_count, 2)

    def test_db_confirms_the_track_really_is_missing_before_warning(self):
        """The in-memory recentlyPlayed_Z1 cache only covers the CURRENT listener
        object's lifetime, and the listener is rebuilt on every reconnect (1,568
        times over 11 days in app.log). A play recorded before the last
        reconnect is therefore absent from the cache while sitting safely in the
        database - which is why this warning fired 1,627 times without any of
        them necessarily being real. The database is the source of truth."""
        recorded = MagicMock(return_value={"bbb"})
        listener = _bareListener(recentlyPlayed=[], get_recorded_track_ids=recorded)
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        with self.assertNoLogs("Database.Listeners.spotifyListener", level="WARNING"):
            listener._checkConnectStateForMissedTracks()

        recorded.assert_called_once_with(["bbb"])
        #< settled, not forgotten - see test_db_is_asked_about_a_given_track_only_once
        self.assertIn("spotify:track:bbb", listener._settledMissingTrackUris)

    def test_db_lookup_still_warns_for_a_genuinely_absent_play(self):
        recorded = MagicMock(return_value=set())
        listener = _bareListener(recentlyPlayed=[], get_recorded_track_ids=recorded)
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm:
            listener._checkConnectStateForMissedTracks()

        self.assertTrue(any("bbb" in message for message in cm.output))

    def test_db_lookup_only_asked_about_cache_misses(self):
        """The cheap in-memory check runs first - only what it can't vouch for
        reaches the database, so the common all-recorded case costs no query."""
        recorded = MagicMock(return_value=set())
        listener = _bareListener(recentlyPlayed=[self._recordedItem("aaa")],
                                 get_recorded_track_ids=recorded)
        _withConnectState(listener, [{"uri": "spotify:track:aaa"}, {"uri": "spotify:track:bbb"}])

        listener._checkConnectStateForMissedTracks()

        recorded.assert_called_once_with(["bbb"])

    def test_db_lookup_not_called_when_cache_vouches_for_everything(self):
        recorded = MagicMock(return_value=set())
        listener = _bareListener(recentlyPlayed=[self._recordedItem("aaa")],
                                 get_recorded_track_ids=recorded)
        _withConnectState(listener, [{"uri": "spotify:track:aaa"}])

        listener._checkConnectStateForMissedTracks()

        recorded.assert_not_called()

    def test_db_lookup_failure_falls_back_to_warning(self):
        """A broken lookup must not silence the diagnostic - degrade to the old
        cache-only behavior rather than swallowing a possible missed play."""
        recorded = MagicMock(side_effect=RuntimeError("db gone"))
        listener = _bareListener(recentlyPlayed=[], get_recorded_track_ids=recorded)
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm:
            listener._checkConnectStateForMissedTracks()

        self.assertTrue(any("bbb" in message for message in cm.output))

    def test_without_the_callback_behaviour_is_unchanged(self):
        """Callers that don't supply the lookup (tests, anything constructed
        before it existed) keep the cache-only behavior."""
        listener = _bareListener(recentlyPlayed=[], get_recorded_track_ids=None)
        _withConnectState(listener, [{"uri": "spotify:track:bbb"}])

        with self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as cm:
            listener._checkConnectStateForMissedTracks()

        self.assertTrue(any("bbb" in message for message in cm.output))

    def test_dedup_cache_evicts_oldest_entry_first(self):
        """set.pop() would evict an arbitrary element; the OrderedDict-based
        cache must specifically evict the OLDEST entry (FIFO), so recently
        warned-about tracks are never forgotten ahead of older ones."""
        listener = _bareListener(recentlyPlayed=[])
        listener._settledMissingTrackUris = collections.OrderedDict.fromkeys(
            f"spotify:track:{i}" for i in range(CONNECT_STATE_MISSED_TRACK_CACHE_SIZE)
        )
        _withConnectState(listener, [{"uri": "spotify:track:new"}])

        listener._checkConnectStateForMissedTracks()

        self.assertNotIn("spotify:track:0", listener._settledMissingTrackUris)
        for i in range(1, CONNECT_STATE_MISSED_TRACK_CACHE_SIZE):
            self.assertIn(f"spotify:track:{i}", listener._settledMissingTrackUris)
        self.assertIn("spotify:track:new", listener._settledMissingTrackUris)


if __name__ == "__main__":
    unittest.main()
