import unittest
from unittest.mock import MagicMock, call
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# See tests/test_database_images.py for why this guard is needed.
if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.database import Database


import threading


def _bareDatabase():
    """A Database with only what _addToDatabaseFromListener touches."""
    db = Database.__new__(Database)
    db.appendTrackData = MagicMock()
    db.appendSkipData = MagicMock()
    db._health_lock = threading.RLock()
    db.listener_health = "HEALTHY"
    db.listener_error_count = 0
    return db


class TestAddToDatabaseFromListener(unittest.TestCase):
    def _items(self):
        return [
            {"track": {"id": "t1"}, "played_at": 100, "ms_played": 60000, "context": None},
            {"track": {"id": "t2"}, "played_at": 200, "ms_played": 120000, "context": None},
        ]

    def test_all_items_are_appended(self):
        db = _bareDatabase()
        db._addToDatabaseFromListener(self._items())
        self.assertEqual(db.appendTrackData.call_count, 2)

    def test_one_bad_item_does_not_block_the_rest(self):
        """The listener retries the whole batch forever if the callback raises, so a
        single malformed item must not prevent the remaining items from being
        recorded (or crash out of the loop)."""
        db = _bareDatabase()
        db.appendTrackData.side_effect = [Exception("malformed item"), None]

        db._addToDatabaseFromListener(self._items())

        self.assertEqual(db.appendTrackData.call_count, 2)
        db.appendTrackData.assert_has_calls([
            call(100, {"id": "t1"}, 60000, context=None, source="listener"),
            call(200, {"id": "t2"}, 120000, context=None, source="listener"),
        ])

    def test_sub_threshold_items_route_to_skip_recorder(self):
        """Events under SKIP_THRESHOLD_MS (including 0ms, previously dropped)
        are recorded as skip events, not plays."""
        from Database.db import SKIP_THRESHOLD_MS
        db = _bareDatabase()
        items = [
            {"track": {"id": "t1"}, "played_at": 100, "ms_played": 0, "context": None},
            {"track": {"id": "t2"}, "played_at": 200, "ms_played": SKIP_THRESHOLD_MS - 1, "context": None},
            {"track": {"id": "t3"}, "played_at": 300, "ms_played": SKIP_THRESHOLD_MS, "context": None},
        ]

        db._addToDatabaseFromListener(items)

        self.assertEqual(db.appendSkipData.call_count, 2)
        db.appendSkipData.assert_has_calls([
            call(100, {"id": "t1"}, 0, source="listener"),
            call(200, {"id": "t2"}, SKIP_THRESHOLD_MS - 1, source="listener"),
        ])
        db.appendTrackData.assert_called_once_with(
            300, {"id": "t3"}, SKIP_THRESHOLD_MS, context=None, source="listener")

    def test_failed_skip_recording_does_not_block_the_rest(self):
        db = _bareDatabase()
        db.appendSkipData.side_effect = Exception("db locked")
        items = [
            {"track": {"id": "t1"}, "played_at": 100, "ms_played": 400, "context": None},
            {"track": {"id": "t2"}, "played_at": 200, "ms_played": 60000, "context": None},
        ]

        db._addToDatabaseFromListener(items)

        db.appendTrackData.assert_called_once_with(
            200, {"id": "t2"}, 60000, context=None, source="listener")

    def test_handles_empty_and_none_input(self):
        db = _bareDatabase()
        db._addToDatabaseFromListener(None)
        db._addToDatabaseFromListener([])
        db.appendTrackData.assert_not_called()

    def test_corrupt_duration_is_clamped_to_track_length_not_skipped(self):
        """SpotipyFree sometimes reports an absurd play duration (e.g. time
        since the previous track change measured across a reconnect). The
        played_at timestamp is still good - the play must be recorded with the
        track's actual length (what the Web API backfill would store), not
        dropped: the recently-played feed doesn't always contain the track
        later, so a skip can lose the play for good (2026-07-17, timorzipa)."""
        db = _bareDatabase()
        trackDuration = 150508
        corruptDuration = trackDuration * (Database.LISTENER_DURATION_CORRUPTION_FACTOR + 5)
        items = [{
            "track": {"id": "t1", "duration_ms": trackDuration},
            "played_at": "2026-07-17T10:35:00Z",
            "ms_played": corruptDuration,
            "context": None,
        }]

        with self.assertLogs("Database.database", level="WARNING") as cm:
            db._addToDatabaseFromListener(items)

        db.appendTrackData.assert_called_once_with(
            "2026-07-17T10:35:00Z", {"id": "t1", "duration_ms": trackDuration},
            trackDuration, context=None, source="listener")
        self.assertTrue(any("corruption" in message for message in cm.output))

    def test_plausible_long_duration_is_kept_unchanged(self):
        """Below the corruption factor the reported duration passes through -
        only clearly-corrupt values get clamped."""
        db = _bareDatabase()
        trackDuration = 150508
        longButPlausible = trackDuration * (Database.LISTENER_DURATION_CORRUPTION_FACTOR - 1)
        items = [{
            "track": {"id": "t1", "duration_ms": trackDuration},
            "played_at": "2026-07-17T10:35:00Z",
            "ms_played": longButPlausible,
            "context": None,
        }]

        db._addToDatabaseFromListener(items)

        db.appendTrackData.assert_called_once_with(
            "2026-07-17T10:35:00Z", {"id": "t1", "duration_ms": trackDuration},
            longButPlausible, context=None, source="listener")

    def test_corrupt_duration_does_not_mark_listener_errored(self):
        """A clamped play is handled, not an error - it must not push the
        listener toward DEGRADED."""
        db = _bareDatabase()
        trackDuration = 150508
        items = [{
            "track": {"id": "t1", "duration_ms": trackDuration},
            "played_at": "2026-07-17T10:35:00Z",
            "ms_played": trackDuration * (Database.LISTENER_DURATION_CORRUPTION_FACTOR + 1),
            "context": None,
        }]
        db.listener_error_count = 3

        with self.assertLogs("Database.database", level="WARNING"):
            db._addToDatabaseFromListener(items)

        self.assertEqual(db.listener_error_count, 0)  #< error-free poll resets the count

    def test_skips_future_played_at_and_handles_string_timestamps(self):
        db = _bareDatabase()
        import time
        future_time = time.time() + 100000  # More than 1 day in the future
        items = [
            {"track": {"id": "t1"}, "played_at": str(future_time), "ms_played": 60000, "context": None},
            {"track": {"id": "t2"}, "played_at": "2026-07-13T10:05:00Z", "ms_played": 120000, "context": None},
        ]
        db._addToDatabaseFromListener(items)
        # Should skip the future one (t1) and successfully append t2
        self.assertEqual(db.appendTrackData.call_count, 1)
        db.appendTrackData.assert_called_once_with("2026-07-13T10:05:00Z", {"id": "t2"}, 120000, context=None, source="listener")


if __name__ == "__main__":
    unittest.main()
