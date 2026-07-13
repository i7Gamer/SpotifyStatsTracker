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
    db._health_lock = threading.RLock()
    db.listener_health = "HEALTHY"
    db.listener_error_count = 0
    return db


class TestAddToDatabaseFromListener(unittest.TestCase):
    def _items(self):
        return [
            {"track": {"id": "t1"}, "played_at": 100, "ms_played": 1000, "context": None},
            {"track": {"id": "t2"}, "played_at": 200, "ms_played": 2000, "context": None},
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
            call(100, {"id": "t1"}, 1000, context=None, source="listener"),
            call(200, {"id": "t2"}, 2000, context=None, source="listener"),
        ])

    def test_handles_empty_and_none_input(self):
        db = _bareDatabase()
        db._addToDatabaseFromListener(None)
        db._addToDatabaseFromListener([])
        db.appendTrackData.assert_not_called()

    def test_skips_future_played_at_and_handles_string_timestamps(self):
        db = _bareDatabase()
        import time
        future_time = time.time() + 100000  # More than 1 day in the future
        items = [
            {"track": {"id": "t1"}, "played_at": str(future_time), "ms_played": 1000, "context": None},
            {"track": {"id": "t2"}, "played_at": "2026-07-13T10:05:00Z", "ms_played": 2000, "context": None},
        ]
        db._addToDatabaseFromListener(items)
        # Should skip the future one (t1) and successfully append t2
        self.assertEqual(db.appendTrackData.call_count, 1)
        db.appendTrackData.assert_called_once_with("2026-07-13T10:05:00Z", {"id": "t2"}, 2000, context=None, source="listener")


if __name__ == "__main__":
    unittest.main()
