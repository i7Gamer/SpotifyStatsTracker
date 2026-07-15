"""Tests for Database._reconcileWithWebApiHistory.

Deletion requires TWO proofs, both anchored on provable double-recording:
- proximity: another local row for the exact same track within
  DUPLICATE_RECORDING_TOLERANCE_SECONDS, AND
- mixed sources: the cluster contains a Web API backfill row plus at least
  one row from another source (listener / import / legacy). Only the
  backfill copies are ever deleted - backfill is the only secondary
  recorder, so it can only ever re-capture a play some other source already
  recorded.

Same-source clusters are never touched: real exports genuinely contain a
short skip immediately followed by a restart of the same track seconds
later, so proximity alone is NOT proof of duplication. Absence from the Web
API response is NEVER by itself grounds for deletion either - Spotify's
recently-played endpoint isn't a complete log (item-count cap, its own
play-duration threshold, track relinking), so a lone play with no
same-track sibling is always left alone.

Motivated in part by a real prior cross-user contamination incident (see
CONTAMINATION_FIX.md) that left bad plays recorded for users who have since
configured API credentials, and by repeated back-and-forth in this file's
history over how aggressive deletion should be (see git log) - this test
suite exists specifically to lock in the "never delete without proof"
guarantee.
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
    db.repo.deletePlay.return_value = True
    return db


API_PLAYED_AT = "2026-07-13T10:00:00Z"
API_TS = timeToInt(API_PLAYED_AT)

TOLERANCE = Database.DUPLICATE_RECORDING_TOLERANCE_SECONDS


def _backfillRow(trackId, playedAt):
    return {"id": trackId, "playedAt": playedAt,
            "createdReason": "web_api_backfill_play (user: alice)"}


def _listenerRow(trackId, playedAt):
    return {"id": trackId, "playedAt": playedAt,
            "createdReason": "listener_play (user: alice)"}


def _importRow(trackId, playedAt):
    return {"id": trackId, "playedAt": playedAt,
            "createdReason": "history_import (user: alice)"}


def _legacyRow(trackId, playedAt):
    return {"id": trackId, "playedAt": playedAt, "createdReason": None}


class TestReconcileWithWebApiHistory(unittest.TestCase):
    def test_empty_items_is_noop(self):
        db = _bareDatabase()

        db._reconcileWithWebApiHistory([])

        db.repo.getPlaysWithSourceInRange.assert_not_called()
        db.repo.deletePlay.assert_not_called()

    def test_none_items_is_noop(self):
        db = _bareDatabase()

        db._reconcileWithWebApiHistory(None)

        db.repo.getPlaysWithSourceInRange.assert_not_called()
        db.repo.deletePlay.assert_not_called()

    def test_items_with_no_played_at_is_noop(self):
        db = _bareDatabase()

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}}])

        db.repo.getPlaysWithSourceInRange.assert_not_called()

    def test_items_with_no_track_id_is_noop(self):
        db = _bareDatabase()

        db._reconcileWithWebApiHistory([{"played_at": API_PLAYED_AT}])

        db.repo.getPlaysWithSourceInRange.assert_not_called()

    def test_no_local_plays_in_window_is_noop(self):
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = []

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()

    def test_single_local_play_is_never_deleted_even_if_absent_from_api(self):
        """Core safety guarantee: a lone play with no same-track sibling is
        never deleted, regardless of whether the API corroborates it."""
        db = _bareDatabase()
        # Local play for a track that never appears in the API response at all.
        db.repo.getPlaysWithSourceInRange.return_value = [_backfillRow("t_missing", API_TS + 30)]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()

    def test_two_far_apart_same_track_plays_are_both_kept(self):
        """Two genuinely separate listens of the same track (gap far larger
        than the duplicate-recording tolerance) must both survive - this is
        a real repeat, not a double-recording of one event."""
        db = _bareDatabase()
        gap = TOLERANCE * 20
        db.repo.getPlaysWithSourceInRange.return_value = [
            _listenerRow("t1", API_TS),
            _backfillRow("t1", API_TS + gap),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()

    def test_backfill_copy_of_listener_play_is_deleted(self):
        """listener + backfill rows within tolerance = the same real listen
        recorded twice. The backfill copy is deleted even when its timestamp
        matches the API exactly - the primary source's row always wins."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            _listenerRow("t1", API_TS + TOLERANCE),
            _backfillRow("t1", API_TS),  #< exact API-time match, still the copy
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_called_once_with("alice", "t1", API_TS)
        db.repo.commit.assert_called_once()

    def test_backfill_copy_of_imported_play_is_deleted(self):
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            _importRow("t1", API_TS),
            _backfillRow("t1", API_TS + 2),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_called_once_with("alice", "t1", API_TS + 2)

    def test_backfill_copy_of_legacy_row_is_deleted(self):
        """Rows predating created_reason (NULL) count as a non-backfill source."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            _legacyRow("t1", API_TS),
            _backfillRow("t1", API_TS + 2),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_called_once_with("alice", "t1", API_TS + 2)

    def test_two_imported_plays_within_tolerance_are_both_kept(self):
        """An imported skip immediately followed by a restart is two REAL
        plays seconds apart - same-source clusters are never touched."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            _importRow("t1", API_TS),
            _importRow("t1", API_TS + 4),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()

    def test_two_legacy_rows_within_tolerance_are_both_kept(self):
        """Without a backfill row in the cluster there is no proof of
        double-recording - unknown-source pairs stay untouched."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            _legacyRow("t1", API_TS),
            _legacyRow("t1", API_TS + 2),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()

    def test_two_backfill_rows_within_tolerance_are_both_kept(self):
        """An all-backfill cluster has no primary-source row proving which is
        the copy - never guess, never delete."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            _backfillRow("t1", API_TS),
            _backfillRow("t1", API_TS + 2),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()

    def test_multiple_backfill_copies_are_all_deleted(self):
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            _listenerRow("t1", API_TS),
            _backfillRow("t1", API_TS + 2),
            _backfillRow("t1", API_TS + 4),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        deletedTimes = sorted(call.args[2] for call in db.repo.deletePlay.call_args_list)
        self.assertEqual(deletedTimes, [API_TS + 2, API_TS + 4])

    def test_commit_is_not_called_when_nothing_is_deleted(self):
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [_listenerRow("t1", API_TS)]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.commit.assert_not_called()

    def test_query_window_is_bounded_by_min_and_max_api_timestamps(self):
        """Never touches plays outside the exact span the API response covers."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = []

        db._reconcileWithWebApiHistory([
            {"track": {"id": "t1"}, "played_at": "2026-07-13T10:00:00Z"},
            {"track": {"id": "t2"}, "played_at": "2026-07-13T12:00:00Z"},
        ])

        args, kwargs = db.repo.getPlaysWithSourceInRange.call_args
        startTs, endTs = args[1], args[2]
        self.assertEqual(endTs - startTs, 2 * 60 * 60)

    def test_delete_failure_is_not_counted_and_does_not_raise(self):
        """deletePlay() returning False (row already gone) must not crash or
        be treated as a successful deletion."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            _listenerRow("t1", API_TS),
            _backfillRow("t1", API_TS + 1),
        ]
        db.repo.deletePlay.return_value = False

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.commit.assert_not_called()

    def test_different_tracks_are_never_compared_to_each_other(self):
        """Two different tracks that happen to be played close together must
        never be treated as duplicates of one another."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            _listenerRow("t1", API_TS),
            _backfillRow("t2", API_TS + 1),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()


if __name__ == "__main__":
    unittest.main()
