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
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.database import Database
from Database.repository import Repository
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
END_TOLERANCE = Database.BACKFILL_END_TIME_MATCH_TOLERANCE_SECONDS


def _backfillRow(trackId, playedAt, canonicalId=None, isrc=None):
    #< canonicalId/isrc: the track's identity, which is what decides whether two
    #  DIFFERENT release ids describe the same recording (see _identityKeyOf)
    return {"id": trackId, "playedAt": playedAt, "createdAt": None,
            "canonicalId": canonicalId, "isrc": isrc,
            "createdReason": "web_api_backfill_play (user: alice)"}


def _listenerRow(trackId, playedAt, createdAt=None, canonicalId=None, isrc=None):
    #< createdAt: the row's insert-time stamp - for listener rows this is the
    #  observed END of the play (the listener inserts at the track-change
    #  moment). getPlaysWithSourceInRange returns it for listener rows only.
    return {"id": trackId, "playedAt": playedAt, "createdAt": createdAt,
            "canonicalId": canonicalId, "isrc": isrc,
            "createdReason": "listener_play (user: alice)"}


def _importRow(trackId, playedAt):
    return {"id": trackId, "playedAt": playedAt,
            "createdReason": "history_import (user: alice)"}


def _legacyRow(trackId, playedAt):
    return {"id": trackId, "playedAt": playedAt, "createdReason": None}


def _track(trackId):
    """The minimum catalog row a real (non-mocked) play can reference. No isrc:
    an empty one is deliberately not a shared identity, so these group by their
    own id and the boundary tests below say nothing about identity matching."""
    return {"id": trackId, "name": f"Song {trackId}", "url": "", "duration": 200000,
            "artists": [{"id": "art1", "name": "Artist One", "url": "",
                         "imageUrl": "", "imageId": "art1"}],
            "album": {"id": "alb1", "name": "Album One", "url": "", "imageId": "alb1",
                      "imageUrl": "", "totalTracks": 1, "releaseDate": 0.0},
            "imageUrl": "", "imageId": "alb1", "explicit": False, "isrc": "",
            "discNumber": 1, "trackNumber": 1, "releaseDate": 0.0}


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

    def test_a_null_track_is_skipped_rather_than_crashing(self):
        """`.get("track", {})` returns the default only when the KEY is absent -
        Spotify sends it present-and-null, and `None.get` is an AttributeError.
        It escaped into _checkWebApiBackfill's catch-all, which had already
        inserted the backfill rows by then, so the run looked like a generic
        "Error during Web API backfill" while reconciliation - the step that
        removes the cross-source duplicates those inserts create - never ran, for
        as long as a null-track item sat in the last-50 window. The listener's
        own snapshot builder guards this exact shape one file over."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = []

        db._reconcileWithWebApiHistory([
            {"track": None, "played_at": API_PLAYED_AT},
            {"track": {"id": "t1"}, "played_at": API_PLAYED_AT},
        ])

        #< the good item still drove the query; the null one was simply skipped
        db.repo.getPlaysWithSourceInRange.assert_called_once()

    def test_every_item_null_is_a_noop(self):
        db = _bareDatabase()

        db._reconcileWithWebApiHistory([{"track": None, "played_at": API_PLAYED_AT}])

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

    def test_clustering_is_independent_of_row_return_order(self):
        """getPlaysWithSourceInRange() has no ORDER BY, so SQLite can return
        same-track plays in any order. The clustering below picks an "anchor"
        via list order (remaining.pop(0)) - it must process rows in a fixed
        (chronological) order internally so the same underlying data always
        produces the same deletions, regardless of what order the DB
        happened to hand the rows back in."""
        db = _bareDatabase()
        listenerRow = _listenerRow("t1", API_TS)
        closeBackfill = _backfillRow("t1", API_TS + 3)   #< within tolerance of the listener row
        farBackfill = _backfillRow("t1", API_TS + 6)     #< within tolerance of closeBackfill only, not of the listener row

        # Deliberately not in chronological order.
        db.repo.getPlaysWithSourceInRange.return_value = [farBackfill, listenerRow, closeBackfill]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        deletedTimes = [call.args[2] for call in db.repo.deletePlay.call_args_list]
        self.assertEqual(deletedTimes, [API_TS + 3])

    def test_commit_is_not_called_when_nothing_is_deleted(self):
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [_listenerRow("t1", API_TS)]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.commit.assert_not_called()

    def test_query_window_is_the_api_span_plus_one_pair_tolerance(self):
        """Never reaches past the span the API response covers, except by the
        width of a single duplicate PAIR at each end - see
        TestReconcileWindowBoundary for why that much is needed and no more.
        The bound is what stops this pass from touching older history."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = []

        db._reconcileWithWebApiHistory([
            {"track": {"id": "t1"}, "played_at": "2026-07-13T10:00:00Z"},
            {"track": {"id": "t2"}, "played_at": "2026-07-13T12:00:00Z"},
        ])

        args, kwargs = db.repo.getPlaysWithSourceInRange.call_args
        startTs, endTs = args[1], args[2]
        padding = max(TOLERANCE, END_TOLERANCE)
        self.assertEqual(startTs, timeToInt("2026-07-13T10:00:00Z") - padding)
        self.assertEqual(endTs, timeToInt("2026-07-13T12:00:00Z") + padding)
        self.assertEqual(endTs - startTs, 2 * 60 * 60 + 2 * padding)

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

    def test_pause_stretched_backfill_copy_is_deleted_via_the_recorded_end(self):
        """The 2026-08-04 incident: a ~3-minute mid-track pause put the
        backfill copy's played_at (Spotify's end-time reading) 474s after the
        listener row's start - far outside the proximity tolerance. The
        listener row's created_at IS the observed end (it was inserted at the
        track-change moment), so pairing the backfill's played_at against it
        recognises the double-recording regardless of how long the pause was."""
        db = _bareDatabase()
        pausedElapsed = 474
        db.repo.getPlaysWithSourceInRange.return_value = [
            _listenerRow("t1", API_TS - pausedElapsed, createdAt=API_TS + 1),
            _backfillRow("t1", API_TS),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_called_once_with("alice", "t1", API_TS)

    def test_end_pairing_outside_the_tolerance_deletes_nothing(self):
        """A recorded end well away from the backfill row's played_at is
        evidence of a different listen, not a copy."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            _listenerRow("t1", API_TS - 474, createdAt=API_TS - END_TOLERANCE - 1),
            _backfillRow("t1", API_TS),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()

    def test_end_pairing_requires_a_listener_created_at(self):
        """Legacy listener rows (created_at NULL) offer no observed end to pair
        against - far-apart rows then stay two separate plays, exactly as
        before the end-time pairing existed."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            _listenerRow("t1", API_TS - 474),
            _backfillRow("t1", API_TS),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()

    def test_end_pairing_never_pairs_two_backfill_rows(self):
        """Even if a backfill row somehow carried a created_at, an all-backfill
        pair has no primary-source row proving which is the copy."""
        db = _bareDatabase()
        rowWithCreatedAt = _backfillRow("t1", API_TS - 474)
        rowWithCreatedAt["createdAt"] = API_TS + 1
        db.repo.getPlaysWithSourceInRange.return_value = [
            rowWithCreatedAt,
            _backfillRow("t1", API_TS),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()


class TestReconcileAcrossReleases(unittest.TestCase):
    """Grouping by the RECORDING, not the release id.

    Spotify names the same recording differently in connect state and in the
    Web API - the live listener records one id, the backfill records another,
    and a pass that grouped by exact track_id saw two unrelated tracks and kept
    both. It was 9.6% of the backfill rows the 2026-08-17 sweep removed, and
    sweeping did nothing about the code that keeps producing them.

    Only IDENTITY proofs are honoured here (a merge group, or a shared ISRC),
    never the sweep's name+duration+artist fallback: that one is a heuristic,
    and this path DELETES from live history with nothing to recover from. A
    heuristic belongs where a human reads the dry run first.
    """

    def test_cross_release_backfill_copy_is_deleted_via_the_merge_group(self):
        """Both ids sit in one merge group, so they are one recording."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            _listenerRow("t_live", API_TS, canonicalId="t_live"),
            _backfillRow("t_api", API_TS + 1, canonicalId="t_live"),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t_api"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_called_once_with("alice", "t_api", API_TS + 1)

    def test_cross_release_backfill_copy_is_deleted_via_a_shared_isrc(self):
        """No merge has been made yet, but the ISRC names the same recording."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            _listenerRow("t_live", API_TS, isrc="GBAYE0601498"),
            _backfillRow("t_api", API_TS + 1, isrc="GBAYE0601498"),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t_api"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_called_once_with("alice", "t_api", API_TS + 1)

    def test_an_isrc_bridges_two_separate_merge_groups(self):
        """Identity is transitive: t_a and t_live are merged, t_live and t_api
        share an ISRC, so a copy recorded as t_api pairs with a play of t_a."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            _listenerRow("t_a", API_TS, canonicalId="t_live"),
            _listenerRow("t_live", API_TS + 900, isrc="GBAYE0601498"),
            _backfillRow("t_api", API_TS + 1, isrc="GBAYE0601498"),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t_api"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_called_once_with("alice", "t_api", API_TS + 1)

    def test_an_empty_isrc_groups_nothing(self):
        """The classic version of this bug: "" is not a shared identity, and
        treating it as one would fold every unstamped track in the window into
        a single group and delete across genuinely different songs."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            _listenerRow("t1", API_TS, isrc=""),
            _backfillRow("t2", API_TS + 1, isrc=""),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t2"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()

    def test_unrelated_tracks_still_never_pair(self):
        """The safety guarantee this widening must not cost: two different
        recordings played back to back share no identity and stay untouched."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            _listenerRow("t1", API_TS, canonicalId="t1", isrc="AAAAA0000001"),
            _backfillRow("t2", API_TS + 1, canonicalId="t2", isrc="BBBBB0000002"),
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t2"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_not_called()

    def test_rows_without_identity_columns_fall_back_to_their_own_id(self):
        """A row shape with no canonicalId/isrc at all (the pre-existing
        callers, and any row whose track went missing from the join) must
        behave exactly as it did before - grouped by its own track id."""
        db = _bareDatabase()
        db.repo.getPlaysWithSourceInRange.return_value = [
            {"id": "t1", "playedAt": API_TS, "createdAt": None,
             "createdReason": "listener_play (user: alice)"},
            {"id": "t1", "playedAt": API_TS + 1, "createdAt": None,
             "createdReason": "web_api_backfill_play (user: alice)"},
        ]

        db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        db.repo.deletePlay.assert_called_once_with("alice", "t1", API_TS + 1)


class TestReconcileWindowBoundary(unittest.TestCase):
    """A duplicate PAIR does not have to sit inside the API's own span.

    The window is built from the API items' played_at stamps, but the local row
    that proves a backfill copy is a copy is written by a different recorder
    with a different clock and a different idea of what played_at means (start
    vs end - spotify/web-api#1083). For the OLDEST and NEWEST item in a page,
    that sibling can land just outside the span, and a row the query never
    returns cannot join a cluster: the pair looks like one lonely play and the
    duplicate survives to be counted forever.

    Runs against a real Repository rather than a MagicMock, because the whole
    question is which rows the QUERY comes back with - a mock returns whatever
    it is handed regardless of the window and would pass either way."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        #< plays carry real foreign keys, so the user and the track have to
        #  exist before a play can reference them
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertTrack(_track("t1"))
        self.repo.upsertTrack(_track("t2"))
        self.repo.commit()
        self.db = Database.__new__(Database)
        self.db.user = "alice"
        self.db.repo = self.repo

    def tearDown(self):
        self.repo.connectionManager.close()
        self._tmpdir.cleanup()

    def _remainingPlayTimes(self):
        return sorted(play["playedAt"] for play in
                      self.repo.getPlaysWithSourceInRange("alice", 0, API_TS + 10000))

    def test_a_listener_row_just_after_the_newest_item_still_proves_a_duplicate(self):
        # The realistic shape: the API's newest stamp IS the play's start, and
        # the listener saw the same track change a few seconds later, so its row
        # sits past the end of the span the page covers.
        self.repo.insertPlay("alice", "t1", API_TS, 200000,
                             created_reason="web_api_backfill_play (user: alice)")
        self.repo.insertPlay("alice", "t1", API_TS + TOLERANCE - 1, 200000,
                             created_reason="listener_play (user: alice)")
        self.repo.commit()

        self.db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        #< the backfill copy goes, the listener row that proved it stays
        self.assertEqual(self._remainingPlayTimes(), [API_TS + TOLERANCE - 1])

    def test_a_listener_row_that_ended_just_before_the_oldest_item_still_proves_one(self):
        # The end-time arm at the low boundary: the listener row's created_at
        # (its observed end) is what pairs with the backfill copy's played_at,
        # and it can sit a few seconds before the oldest stamp in the page.
        with patch("Database.queries.plays.time") as mockTime:
            mockTime.time.return_value = API_TS - 2   #< the listener row's created_at
            self.repo.insertPlay("alice", "t1", API_TS - 300, 200000,
                                 created_reason="listener_play (user: alice)")
        self.repo.insertPlay("alice", "t1", API_TS, 200000,
                             created_reason="web_api_backfill_play (user: alice)")
        self.repo.commit()

        self.db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        self.assertEqual(self._remainingPlayTimes(), [API_TS - 300])

    def test_padding_does_not_delete_a_lone_play_outside_the_span(self):
        """Widening the candidate set must not widen what counts as proof: a
        play just outside the span with no sibling is still untouchable."""
        self.repo.insertPlay("alice", "t1", API_TS, 200000,
                             created_reason="web_api_backfill_play (user: alice)")
        self.repo.insertPlay("alice", "t2", API_TS + TOLERANCE - 1, 200000,
                             created_reason="web_api_backfill_play (user: alice)")
        self.repo.commit()

        self.db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        self.assertEqual(self._remainingPlayTimes(), [API_TS, API_TS + TOLERANCE - 1])

    def test_a_duplicate_far_outside_the_span_is_still_left_alone(self):
        """The bound is loosened by a tolerance, not removed: an hour-old pair
        the page does not cover is none of this pass's business."""
        anHourBefore = API_TS - 3600
        self.repo.insertPlay("alice", "t1", anHourBefore, 200000,
                             created_reason="web_api_backfill_play (user: alice)")
        self.repo.insertPlay("alice", "t1", anHourBefore + 1, 200000,
                             created_reason="listener_play (user: alice)")
        self.repo.commit()

        self.db._reconcileWithWebApiHistory([{"track": {"id": "t1"}, "played_at": API_PLAYED_AT}])

        self.assertEqual(self._remainingPlayTimes(), [anHourBefore, anHourBefore + 1])


if __name__ == "__main__":
    unittest.main()
