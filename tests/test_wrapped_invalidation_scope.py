"""WHICH cached Wrapped years a merge or a split is allowed to drop.

That it must drop some was already pinned (test_track_merge_toggle); that the
rebuilt snapshot is correct, and that a torn one cannot outlive the
invalidation, in test_track_merge_wrapped. This file pins the third question -
the scope - which was the whole of the cost.

The old answer was "all of them, for everyone". Correct, and unaffordable: the
ISRC backfiller merged a handful of tracks every few minutes for as long as new
music arrived, and each batch dropped every account's entire history. A live
instance spent 147 recalculations a day rebuilding years no merge had touched
(2026-08-12/13 app.log), against ~20-40 before the toggle went on. Every one of
275 cache hits in that log was the current year; 2015-2025 never hit once.

The scope that is both correct and cheap: a merge can only move numbers in a
year where one of the group's own tracks was played. Two halves matter, and
both are load-bearing rather than defensive -

  - the GROUP, not the tracks the matcher happened to write. A joining track
    drags the group's all-time first listen backwards, which pulls the group
    OUT of the discovery lists of the year that first listen used to fall in -
    a year that can belong to a member nobody touched this run. That is the
    test_a_joining_track_moves_a_pre_existing_members_year case below, and it
    fails against a scope built only from the ids passed in.
  - every USER who played it, not the one who ran the merge. Sameness is a
    property of the recording, so bob's frozen top_songs goes as stale as
    alice's the moment it runs.

What it deliberately does NOT narrow: the invalidation generation still moves
on every merge, so an in-flight recalculation of ANY year discards its save.
That is the torn-read guard, it is instance-wide by nature, and one wasted
recomputation is the price of never caching a snapshot that straddles a merge.
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, RecordingConnection

ISRC = "USRC12345678"
OTHER_ISRC = "USRC87654321"


def _ts(year, month=6, day=15, hour=12):
    """Mid-year by default, so a year label is unambiguous in every timezone."""
    return datetime.datetime(year, month, day, hour,
                             tzinfo=datetime.timezone.utc).timestamp()


class ScopeTestCase(DatabaseTestCase):
    def _db(self):
        return self._makeDb({}, [])

    def _user(self, db, username):
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO users (username, created_at) VALUES (?, 0)",
                         (username,))

    def _track(self, db, trackId, isrc=None, name="Song"):
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES ('alb', 'A', '')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id, isrc, duration_ms) "
                         "VALUES (?, ?, '', 'alb', ?, 200000)", (trackId, name, isrc))

    def _plays(self, db, username, trackId, *timestamps):
        self._user(db, username)
        conn = db.repo._conn()
        with conn:
            for ts in timestamps:
                conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                             "VALUES (?, ?, ?, 200000)", (username, trackId, ts))

    def _cacheYears(self, db, username, *years):
        self._user(db, username)
        conn = db.repo._conn()
        with conn:
            for year in years:
                conn.execute(
                    "INSERT INTO user_wrapped (username, year, calculated_at, max_played_at,"
                    " total_plays, total_ms, longest_streak, unique_songs, unique_artists,"
                    " discovered_songs, discovered_artists, time_series_day, time_series_week,"
                    " time_series_month, top_songs, top_artists, top_albums,"
                    " discovered_songs_list, discovered_artists_list, discovered_albums_list)"
                    " VALUES (?, ?, 1, 1, 1, 1, 1, 1, 1, 1, 1, '[]', '[]', '[]', '[]', '[]',"
                    " '[]', '[]', '[]', '[]')",
                    (username, year))

    def _survivingYears(self, db):
        return {(row["username"], row["year"]) for row in
                db.repo._conn().execute("SELECT username, year FROM user_wrapped")}


class TestTheMergeScope(ScopeTestCase):
    def test_it_drops_only_the_years_its_tracks_were_played_in(self):
        """The reported bug, stated as a contract: a 2022-only merge must not
        cost a rebuild of 2019 and 2026."""
        db = self._db()
        self._track(db, "A" * 22, ISRC)
        self._track(db, "B" * 22, ISRC)
        self._plays(db, "alice", "A" * 22, _ts(2022), _ts(2022, 7))
        self._plays(db, "alice", "B" * 22, _ts(2022, 8))
        self._cacheYears(db, "alice", 2019, 2022, 2026)

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._survivingYears(db), {("alice", 2019), ("alice", 2026)})

    def test_it_drops_that_year_for_every_user_who_played_it(self):
        """Global by nature: the merge is a fact about the recording, so it
        reaches bob's frozen snapshot without bob doing anything."""
        db = self._db()
        self._track(db, "A" * 22, ISRC)
        self._track(db, "B" * 22, ISRC)
        self._plays(db, "alice", "A" * 22, _ts(2022), _ts(2022, 7))
        self._plays(db, "bob", "B" * 22, _ts(2022, 8))
        self._cacheYears(db, "alice", 2019, 2022)
        self._cacheYears(db, "bob", 2019, 2022)

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._survivingYears(db), {("alice", 2019), ("bob", 2019)})

    def test_a_user_who_never_played_them_keeps_every_year(self):
        """The other half of narrowing: carol's history cannot have moved."""
        db = self._db()
        self._track(db, "A" * 22, ISRC)
        self._track(db, "B" * 22, ISRC)
        self._plays(db, "alice", "A" * 22, _ts(2022), _ts(2022, 7))
        self._plays(db, "alice", "B" * 22, _ts(2022, 8))
        self._cacheYears(db, "carol", 2019, 2022, 2026)

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._survivingYears(db),
                         {("carol", 2019), ("carol", 2022), ("carol", 2026)})

    def test_it_drops_every_year_the_group_spans(self):
        """Both members' years, not just the winner's: the loser stops being
        its own row in the year it was played, and the group's first listen
        moves to the earlier of the two."""
        db = self._db()
        self._track(db, "A" * 22, ISRC)
        self._track(db, "B" * 22, ISRC)
        self._plays(db, "alice", "A" * 22, _ts(2019), _ts(2019, 7))
        self._plays(db, "alice", "B" * 22, _ts(2022))
        self._cacheYears(db, "alice", 2019, 2022, 2026)

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._survivingYears(db), {("alice", 2026)})

    def test_a_joining_track_moves_a_pre_existing_members_year(self):
        """The case a scope built from the matcher's own writes gets wrong.

        C was hand-merged into A long ago and is not touched by this run. The
        group's all-time first listen is C's, in 2019, so the group counts as a
        2019 discovery. B joining drags that anchor back to 2015 - and 2019's
        discovery list silently loses an entry, in a year whose own play count
        and max_played_at never moved. Nothing would ever notice."""
        db = self._db()
        self._track(db, "A" * 22, ISRC)
        self._track(db, "B" * 22, ISRC)
        self._track(db, "C" * 22, None, name="Hand Merged")
        self._plays(db, "alice", "A" * 22, _ts(2026), _ts(2026, 7))
        self._plays(db, "alice", "C" * 22, _ts(2019))
        self._plays(db, "alice", "B" * 22, _ts(2015))
        db.repo.mergeTrackManually("C" * 22, "A" * 22, decidedBy="timorzipa")
        self._cacheYears(db, "alice", 2015, 2019, 2022, 2026)

        db.repo.mergeTracksByIsrc()

        #< 2022 is the only year no member of the resulting group ever touched
        self.assertEqual(self._survivingYears(db), {("alice", 2022)})

    def test_a_run_that_merges_nothing_leaves_every_year_alone(self):
        """A daily backfiller pass re-runs the matcher whether or not there is
        anything left to merge, and the steady state is that there is not."""
        db = self._db()
        self._cacheYears(db, "alice", 2019, 2022, 2026)

        db.repo.mergeTracksByIsrc()

        self.assertEqual(len(self._survivingYears(db)), 3)

    def test_an_untouched_group_costs_its_years_nothing(self):
        """Two independent pairs: merging one must not drop the other's year,
        even though both run inside the same pass."""
        db = self._db()
        self._track(db, "A" * 22, ISRC)
        self._track(db, "B" * 22, ISRC)
        self._track(db, "C" * 22, OTHER_ISRC, name="Other")
        self._plays(db, "alice", "A" * 22, _ts(2022))
        self._plays(db, "alice", "B" * 22, _ts(2022, 7))
        self._plays(db, "alice", "C" * 22, _ts(2019))
        self._cacheYears(db, "alice", 2019, 2022)

        db.repo.mergeTracksByIsrc()

        #< C is alone under its ISRC, so nothing about it merged
        self.assertEqual(self._survivingYears(db), {("alice", 2019)})


class TestTheYearBoundaryIsNotSharp(ScopeTestCase):
    """A year is bucketed in the USER's timezone (the worker builds its bounds
    from datetime.now(tz=self.tz)), and this delete runs in the shared repo,
    which has no user's tz to hand. So a play close enough to a UTC boundary to
    land in either calendar year for SOME offset drops both. Erring wide costs
    one rebuild; erring narrow strands a permanently wrong year."""

    def test_a_play_just_before_new_year_drops_both_years(self):
        db = self._db()
        self._track(db, "A" * 22, ISRC)
        self._track(db, "B" * 22, ISRC)
        self._plays(db, "alice", "A" * 22, _ts(2022, 12, 31, 23), _ts(2022, 12, 31, 22))
        self._plays(db, "alice", "B" * 22, _ts(2022, 12, 31, 21))
        self._cacheYears(db, "alice", 2021, 2022, 2023)

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._survivingYears(db), {("alice", 2021)})

    def test_a_play_just_after_new_year_drops_both_years(self):
        db = self._db()
        self._track(db, "A" * 22, ISRC)
        self._track(db, "B" * 22, ISRC)
        self._plays(db, "alice", "A" * 22, _ts(2023, 1, 1, 1), _ts(2023, 1, 1, 2))
        self._plays(db, "alice", "B" * 22, _ts(2023, 1, 1, 3))
        self._cacheYears(db, "alice", 2021, 2022, 2023)

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._survivingYears(db), {("alice", 2021)})


class TestTheSplitScope(ScopeTestCase):
    def test_a_single_track_undo_drops_the_whole_dissolving_groups_years(self):
        """Taking B back out moves numbers in A's years too - the group it
        leaves loses B's plays, and its first listen may move forward."""
        db = self._db()
        self._track(db, "A" * 22, ISRC)
        self._track(db, "B" * 22, ISRC)
        self._plays(db, "alice", "A" * 22, _ts(2019), _ts(2019, 7))
        self._plays(db, "alice", "B" * 22, _ts(2022))
        db.repo.mergeTracksByIsrc()
        self._cacheYears(db, "alice", 2019, 2022, 2026)

        db.repo.unmergeTrack("B" * 22, decidedBy="timorzipa")

        self.assertEqual(self._survivingYears(db), {("alice", 2026)})

    def test_a_manual_merge_drops_only_the_touched_years(self):
        db = self._db()
        self._track(db, "A" * 22, None)
        self._track(db, "B" * 22, None)
        self._plays(db, "alice", "A" * 22, _ts(2022), _ts(2022, 7))
        self._plays(db, "alice", "B" * 22, _ts(2022, 8))
        self._cacheYears(db, "alice", 2019, 2022, 2026)

        db.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="timorzipa")

        self.assertEqual(self._survivingYears(db), {("alice", 2019), ("alice", 2026)})

    def test_a_manual_merge_also_drops_the_years_of_the_group_it_LEAVES(self):
        """mergeTrackManually is the one verb that dissolves a group and forms
        one in the same call, so it needs BOTH snapshots - the rule
        _mergeGroupTrackIds states and unmergeTrack already follows.

        C leaves the group headed by A and joins D. A's remaining year is what
        the AFTER-only scope misses: the group A heads has just lost its
        earliest listen, so A's own discovery anchor moves forward and 2019
        must be rebuilt - yet 2019's total_plays and max_played_at did not
        change, which is all _wrappedCacheNeedsRecalc compares. Nothing would
        ever notice.

        Reachable through the review queue's merge verb, which unlike its
        reject sibling has no stale-page guard: getMergeReviewCandidates never
        PROPOSES a merged member, but a queue open in a second tab (or the
        daily matcher landing between render and click) offers exactly one."""
        db = self._db()
        self._track(db, "A" * 22, None)
        self._track(db, "C" * 22, None)
        self._track(db, "D" * 22, None)
        self._plays(db, "alice", "A" * 22, _ts(2019))
        self._plays(db, "alice", "C" * 22, _ts(2022))
        self._plays(db, "alice", "D" * 22, _ts(2023))
        db.repo.mergeTrackManually("C" * 22, "A" * 22, decidedBy="timorzipa")
        self._cacheYears(db, "alice", 2019, 2022, 2023, 2026)

        #< C is a MEMBER of A's group, not its head - so A stays behind
        db.repo.mergeTrackManually("C" * 22, "D" * 22, decidedBy="timorzipa")

        self.assertEqual(self._survivingYears(db), {("alice", 2026)})


class TestTheTornReadGuardIsUnchanged(ScopeTestCase):
    """Narrowing the DELETE must not narrow the generation stamp: it is what
    stops an in-flight recalculation saving a snapshot whose reads straddle the
    merge, and the recalculation in flight may be of any year."""

    def test_the_stamp_moves_on_a_narrowed_merge(self):
        db = self._db()
        self._track(db, "A" * 22, ISRC)
        self._track(db, "B" * 22, ISRC)
        self._plays(db, "alice", "A" * 22, _ts(2022))
        self._plays(db, "alice", "B" * 22, _ts(2022, 7))
        before = db.repo.getWrappedInvalidationGeneration()

        db.repo.mergeTracksByIsrc()

        self.assertGreater(db.repo.getWrappedInvalidationGeneration(), before)

    def test_the_stamp_moves_even_when_no_cached_year_matched(self):
        """Nothing to delete is not nothing to guard: a recalculation of a year
        this merge does not touch can still be mid-flight, and it read the
        track table before the merge landed."""
        db = self._db()
        self._track(db, "A" * 22, ISRC)
        self._track(db, "B" * 22, ISRC)
        self._plays(db, "alice", "A" * 22, _ts(2022))
        self._plays(db, "alice", "B" * 22, _ts(2022, 7))
        self._cacheYears(db, "carol", 2019)
        before = db.repo.getWrappedInvalidationGeneration()

        db.repo.mergeTracksByIsrc()

        self.assertGreater(db.repo.getWrappedInvalidationGeneration(), before)
        self.assertEqual(self._survivingYears(db), {("carol", 2019)})

    def test_a_run_that_merges_nothing_does_not_move_the_stamp(self):
        db = self._db()
        before = db.repo.getWrappedInvalidationGeneration()

        db.repo.mergeTracksByIsrc()

        self.assertEqual(db.repo.getWrappedInvalidationGeneration(), before)


def _wrappedPayload():
    """The minimum saveCachedWrapped accepts - mirrors test_wrapped_cache's
    _wrappedRow; the tests below care about the verdict, not the contents.
    Module-level because two unrelated classes need it."""
    return {
        "calculated_at": 1.0, "max_played_at": 1775000000,
        "total_plays": 2, "total_ms": 60000, "longest_streak": 1,
        "peak_day": "Monday", "peak_plays": 1,
        "unique_songs": 1, "unique_artists": 1,
        "discovered_songs": 1, "discovered_artists": 1,
        "time_series_day": "[]", "time_series_week": "[]",
        "time_series_month": "[]",
        "top_songs": "[]", "top_artists": "[]", "top_albums": "[]",
        "discovered_songs_list": "[]", "discovered_artists_list": "[]",
        "discovered_albums_list": "[]",
    }


class TestEveryInvalidationMovesTheStamp(ScopeTestCase):
    """The torn-read guard belongs to the CACHE, not to the merge.

    A recalculation reads for seconds and saves once; the generation is what
    tells it that the ground moved underneath it in between. The merge paths
    bump it. The three per-user drops did not, and each of them invalidates
    for exactly the reason a stale save is unrecoverable: the fields they
    invalidate (the discovery lists, every timezone-bucketed figure) are the
    ones total_plays and max_played_at cannot betray, so _wrappedCacheNeedsRecalc
    never marks the restored row stale again.

    Stated as the outcome rather than as "it calls the bump", so it holds
    whichever way the serialization is implemented."""

    def _saveAcross(self, db, invalidate):
        """Run `invalidate` in the middle of a recalculation of 2026 and
        return whether the recalculation's save was accepted."""
        self._user(db, "alice")
        startedUnder = db.repo.getWrappedInvalidationGeneration()
        invalidate()
        return db.repo.saveCachedWrapped("alice", 2026, _wrappedPayload(),
                                         expectedGeneration=startedUnder)

    def test_an_imports_from_year_drop_discards_a_recalculation_in_flight(self):
        """The import writes plays into 2019 and drops 2019 onwards; the
        worker is mid-2026, a year the import did not touch and therefore will
        never be asked to rebuild again."""
        db = self._db()
        stored = self._saveAcross(
            db, lambda: db.repo.deleteUserWrappedFromYear("alice", 2019))
        self.assertFalse(stored)

    def test_a_timezone_changes_full_drop_discards_a_recalculation_in_flight(self):
        """Its own comment says the drop exists so the old zone's figures are
        not served indefinitely - which is what restoring one does."""
        db = self._db()
        stored = self._saveAcross(
            db, lambda: db.repo.deleteAllUserWrapped("alice"))
        self.assertFalse(stored)

    def test_the_workers_single_year_drop_discards_a_recalculation_in_flight(self):
        """The third path: the wrapped worker's own "this year has no plays
        anymore" drop. Seeded with a cached row, because that is the case the
        worker's comment describes and the only one in which this drop
        invalidates anything - see the no-op test below."""
        db = self._db()
        self._cacheYears(db, "alice", 2024)
        stored = self._saveAcross(
            db, lambda: db.repo.deleteUserWrapped("alice", 2024))
        self.assertFalse(stored)

    def test_a_worker_drop_that_deletes_nothing_does_not_disturb_anyone(self):
        """The gate that keeps the fix above from costing more than the bug.

        The worker calls deleteUserWrapped on EVERY cycle for every play-less
        year, cached or not - the DELETE is simply a no-op when there is
        nothing there. Bumping unconditionally would therefore fire every 15
        minutes, per empty year, per user, and each bump discards whatever the
        other users' workers happen to have in flight; a year could stay
        uncacheable indefinitely. Safe to gate here and nowhere else because
        this call reacts to no data change of its own: the plays vanished
        earlier, through a path that invalidated for them then."""
        db = self._db()
        stored = self._saveAcross(
            db, lambda: db.repo.deleteUserWrapped("alice", 2026))
        self.assertTrue(stored)

    def test_a_save_with_no_invalidation_between_is_still_accepted(self):
        """The other half: bumping on every drop must not make every save
        fail. Without this the three above pass against a saveCachedWrapped
        that simply returns False."""
        db = self._db()
        stored = self._saveAcross(db, lambda: None)
        self.assertTrue(stored)


class TestTheGenerationCheckIsAtomicWithItsWrite(ScopeTestCase):
    """A guard that reads in one statement and acts in another is not a guard.

    Python's sqlite3 under legacy transaction control does not BEGIN for a
    SELECT - only DML opens the transaction - so a check-then-write inside
    `with conn:` holds no lock at the moment it decides. The whole duration of
    a concurrent invalidation's transaction is then the window: the check reads
    the pre-bump value (the other transaction is uncommitted and invisible),
    passes, and the following INSERT blocks on the lock and lands the moment
    the invalidation commits - undoing the DELETE it was supposed to yield to.

    Pinned structurally rather than by racing two threads: the property is
    "the decision and the write are one unit", and a test that has to lose a
    race to fail is a test that passes by luck. Deliberately satisfied by
    EITHER remedy - holding the lock across the check (BEGIN IMMEDIATE) or
    folding the check into the write's own WHERE - so it constrains the
    guarantee and not the spelling."""

    GENERATION_KEY = "wrapped_invalidation_generation"

    def _statementsFor(self, db, call):
        log = []
        proxy = RecordingConnection(db.repo._conn(), log)
        #< instance attribute shadowing the class method; `del` restores the
        #  class lookup exactly, rather than freezing a bound method back on
        db.repo._conn = lambda: proxy
        try:
            call()
        finally:
            del db.repo._conn
        return log

    def _assertGuardIsAtomic(self, statements):
        reads = [(sql, inTx) for sql, inTx in statements
                 if self.GENERATION_KEY in sql or "app_settings" in sql]
        self.assertTrue(reads, "the guard never consulted the generation at all")
        for sql, inTx in reads:
            isWrite = sql.upper().startswith(("INSERT", "UPDATE", "DELETE"))
            self.assertTrue(
                inTx or isWrite,
                f"the generation is consulted with no transaction open and no "
                f"write of its own, so nothing serializes it: {sql[:90]}")

    def test_saveCachedWrapped_decides_and_writes_as_one_unit(self):
        db = self._db()
        self._user(db, "alice")
        payload = _wrappedPayload()
        generation = db.repo.getWrappedInvalidationGeneration()

        statements = self._statementsFor(
            db, lambda: db.repo.saveCachedWrapped(
                "alice", 2026, payload, expectedGeneration=generation))

        self._assertGuardIsAtomic(statements)

    def test_dismissMergeCandidate_decides_and_writes_as_one_unit(self):
        """The same shape, one file over: its comment claims the merged-state
        check happens "inside the write's own transaction". A matcher pass
        landing in the gap produces exactly the state the docstring says is
        refused - a manual reject standing under a merged track, which
        unmergeAllIsrcMerges spares and _clearContradictedRejection never
        reaches."""
        db = self._db()
        self._track(db, "A" * 22, None)

        statements = self._statementsFor(
            db, lambda: db.repo.dismissMergeCandidate(
                "A" * 22, decidedBy="timorzipa"))

        reads = [(sql, inTx) for sql, inTx in statements
                 if "FROM tracks" in sql]
        self.assertTrue(reads, "the guard never consulted the merged state")
        for sql, inTx in reads:
            self.assertTrue(
                inTx,
                f"the merged-state check runs with no transaction open, so a "
                f"merge can land between it and the INSERT: {sql[:90]}")


if __name__ == "__main__":
    unittest.main()
