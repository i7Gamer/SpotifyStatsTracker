"""Tests for Database._reconcileWithWebApiHistory - deletes locally-recorded
plays that fall inside the time window Spotify's authoritative (real OAuth,
account-wide) recently-played API response covers, but aren't corroborated by
it. Unlike the earlier connect-state-based idea (rejected - device/session
scoped, no completeness guarantee), this endpoint is documented as an
account-wide view of what was actually played, so absence within its window is
real evidence - motivated in part by a real prior cross-user contamination
incident (see CONTAMINATION_FIX.md) that left bad plays recorded for users who
have since configured API credentials.
"""
import sys
import os
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.database import Database
from Database.utils import timeToInt


def _bareDatabase():
    db = Database.__new__(Database)
    db.user = "alice"
    db.repo = MagicMock()
    return db


API_PLAYED_AT = "2026-07-13T10:00:00Z"
API_TS = timeToInt(API_PLAYED_AT)


class TestReconcileWithWebApiHistory(unittest.TestCase):
    def test_empty_items_is_noop(self):
        db = _bareDatabase()

        db._reconcileWithWebApiHistory([])

        db.repo.getPlaysInRange.assert_not_called()
        db.repo.deletePlay.assert_not_called()

    def test_none_items_is_noop(self):
        db = _bareDatabase()

        db._reconcileWithWebApiHistory(None)

        db.repo.getPlaysInRange.assert_not_called()

    def test_items_with_no_played_at_is_noop(self):
        db = _bareDatabase()

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}}])

        db.repo.getPlaysInRange.assert_not_called()

    def test_no_local_plays_in_window_is_noop(self):
        db = _bareDatabase()
        db.repo.getPlaysInRange.return_value = []

        db._reconcileWithWebApiHistory([{"played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()

    def test_local_play_matching_an_api_timestamp_is_kept(self):
        db = _bareDatabase()
        db.repo.getPlaysInRange.return_value = [{"id": "t1", "playedAt": API_TS}]

        db._reconcileWithWebApiHistory([{"played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()

    def test_local_play_within_tolerance_of_an_api_timestamp_is_kept(self):
        db = _bareDatabase()
        db.repo.getPlaysInRange.return_value = [{"id": "t1", "playedAt": API_TS + 1}]  # 1s off

        db._reconcileWithWebApiHistory([{"played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()

    def test_local_play_not_corroborated_by_any_api_timestamp_is_deleted(self):
        db = _bareDatabase()
        db.repo.getPlaysInRange.return_value = [
            {"id": "t1", "playedAt": API_TS},       # corroborated - kept
            {"id": "t2", "playedAt": API_TS + 50},  # not corroborated - deleted
        ]
        db.repo.deletePlay.return_value = True

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_called_once_with("alice", "t2", API_TS + 50)
        db.repo.commit.assert_called_once()

    def test_commit_is_not_called_when_nothing_is_deleted(self):
        db = _bareDatabase()
        db.repo.getPlaysInRange.return_value = [{"id": "t1", "playedAt": API_TS}]

        db._reconcileWithWebApiHistory([{"played_at": API_PLAYED_AT}])

        db.repo.commit.assert_not_called()

    def test_query_window_is_bounded_by_min_and_max_api_timestamps(self):
        """Never touches plays outside the exact span the API response covers."""
        db = _bareDatabase()
        db.repo.getPlaysInRange.return_value = []

        db._reconcileWithWebApiHistory([
            {"track": {"id": "t1"}, "played_at": "2026-07-13T10:00:00Z"},
            {"track": {"id": "t2"}, "played_at": "2026-07-13T12:00:00Z"},
        ])

        args, kwargs = db.repo.getPlaysInRange.call_args
        startTs, endTs = args[1], args[2]
        self.assertEqual(endTs - startTs, 2 * 60 * 60)

    def test_delete_failure_is_not_counted_and_does_not_raise(self):
        """deletePlay() returning False (row already gone) must not crash or
        be treated as a successful deletion."""
        db = _bareDatabase()
        db.repo.getPlaysInRange.return_value = [{"id": "t1", "playedAt": API_TS + 50}]
        db.repo.deletePlay.return_value = False

        db._reconcileWithWebApiHistory([{"played_at": API_PLAYED_AT}])

        db.repo.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
