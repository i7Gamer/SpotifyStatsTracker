"""Tests for Database._reconcileWithWebApiHistory.

Deletion is anchored on PROVABLE duplication only: a local play is only
removed when another local row exists for the exact same track within
DUPLICATE_RECORDING_TOLERANCE_SECONDS of it (a track can't legitimately
restart within a few seconds of itself). Absence from the Web API response
is NEVER by itself grounds for deletion - Spotify's recently-played endpoint
isn't a complete log (item-count cap, its own play-duration threshold, track
relinking), so a lone play with no same-track sibling is always left alone.
The API response is used only to decide which of two duplicate rows to keep.

Motivated in part by a real prior cross-user contamination incident (see
CONTAMINATION_FIX.md) that left bad plays recorded for users who have since
configured API credentials, and by repeated back-and-forth in this file's
history over how aggressive deletion should be (see git log) - this test
suite exists specifically to lock in the "never delete a non-duplicate"
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
        db.repo.deletePlay.assert_not_called()

    def test_items_with_no_played_at_is_noop(self):
        db = _bareDatabase()

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}}])

        db.repo.getPlaysInRange.assert_not_called()

    def test_items_with_no_track_id_is_noop(self):
        db = _bareDatabase()

        db._reconcileWithWebApiHistory([{"played_at": API_PLAYED_AT}])

        db.repo.getPlaysInRange.assert_not_called()

    def test_no_local_plays_in_window_is_noop(self):
        db = _bareDatabase()
        db.repo.getPlaysInRange.return_value = []

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()

    def test_single_local_play_is_never_deleted_even_if_absent_from_api(self):
        """Core safety guarantee: a lone play with no same-track sibling is
        never deleted, regardless of whether the API corroborates it."""
        db = _bareDatabase()
        # Local play for a track that never appears in the API response at all.
        db.repo.getPlaysInRange.return_value = [{"id": "t_missing", "playedAt": API_TS + 30}]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()

    def test_two_far_apart_same_track_plays_are_both_kept(self):
        """Two genuinely separate listens of the same track (gap far larger
        than the duplicate-recording tolerance) must both survive - this is
        a real repeat, not a double-recording of one event."""
        db = _bareDatabase()
        gap = TOLERANCE * 20
        db.repo.getPlaysInRange.return_value = [
            {"id": "t1", "playedAt": API_TS},
            {"id": "t1", "playedAt": API_TS + gap},
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()

    def test_two_close_together_same_track_plays_worse_api_match_is_deleted(self):
        """Two local rows for the same track within tolerance can only be the
        same real listen recorded twice - keep whichever matches an actual
        API-reported time most closely, delete the other."""
        db = _bareDatabase()
        goodMatch = {"id": "t1", "playedAt": API_TS}          # exact API match
        worseMatch = {"id": "t1", "playedAt": API_TS + TOLERANCE}  # within tolerance of goodMatch, but not of API time
        db.repo.getPlaysInRange.return_value = [worseMatch, goodMatch]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_called_once_with("alice", "t1", API_TS + TOLERANCE)
        db.repo.commit.assert_called_once()

    def test_two_close_together_plays_with_no_api_signal_keeps_earliest(self):
        """If the track has no API-reported time at all to break the tie,
        fall back to keeping the earliest recorded copy deterministically."""
        db = _bareDatabase()
        earliest = {"id": "t_unseen", "playedAt": API_TS}
        later = {"id": "t_unseen", "playedAt": API_TS + 2}
        db.repo.getPlaysInRange.return_value = [later, earliest]

        # API response only reports a different track, so "t_unseen" gets no
        # tie-breaking signal at all.
        db._reconcileWithWebApiHistory([{"track": {"id": "t_other"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_called_once_with("alice", "t_unseen", API_TS + 2)

    def test_commit_is_not_called_when_nothing_is_deleted(self):
        db = _bareDatabase()
        db.repo.getPlaysInRange.return_value = [{"id": "t1", "playedAt": API_TS}]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

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
        db.repo.getPlaysInRange.return_value = [
            {"id": "t1", "playedAt": API_TS},
            {"id": "t1", "playedAt": API_TS + 1},
        ]
        db.repo.deletePlay.return_value = False

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.commit.assert_not_called()

    def test_different_tracks_are_never_compared_to_each_other(self):
        """Two different tracks that happen to be played close together must
        never be treated as duplicates of one another."""
        db = _bareDatabase()
        db.repo.getPlaysInRange.return_value = [
            {"id": "t1", "playedAt": API_TS},
            {"id": "t2", "playedAt": API_TS + 1},
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()


if __name__ == "__main__":
    unittest.main()
