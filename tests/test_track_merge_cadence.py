"""HOW OFTEN the ISRC matcher is allowed to run.

The matcher used to run on every metadata-backfill cycle - every few minutes,
per user, so three times over - because it is idempotent and a run that merges
nothing invalidates nothing. What that reasoning missed is the run that DOES
merge something: it drops the cached Wrapped years its groups touch, and the
backfiller keeps completing pairs for as long as new ISRCs arrive. The live
instance merged 137 tracks in 23 batches across two days and paid for it with
147 Wrapped rebuilds a day, against ~20-40 before the toggle went on.

Narrowing the invalidation (test_wrapped_invalidation_scope) took the cost of
one batch down but not the number of batches: a 15-18 track batch still reached
most of the cache. This is the other half - the matcher gets one slot a day, so
the merges accumulate into it instead of trickling.

What that trades away, on purpose: a duplicate completed by a newly-arrived
ISRC now waits up to a day to fold. Nothing is wrong in the meantime - both
sides still count, they just count separately, which is the same thing the
instance showed for the years before the toggle existed.

The slot is claimed in app_settings rather than held in memory, for two
reasons: the three per-user backfiller threads share one instance-wide matcher
(so an in-process flag would let each thread run its own daily pass), and a
stamp on the class would not survive a restart - the trap
_catalogBackoffUntil already walked into, where a 1374-minute stand-down was
re-armed from scratch by the restart that walked back into it.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
from Database.queries._base import (TRACK_MERGE_LAST_RUN_KEY,
                                    TRACK_MERGE_MIN_INTERVAL_SECONDS)

NOW = 1_800_000_000.0   #< a fixed clock: nothing here should depend on the real one
WORKERS = 3             #< one metadata backfiller per live user


class CadenceTestCase(DatabaseTestCase):
    def _db(self):
        return self._makeDb({}, [])


class TestTheDailySlot(CadenceTestCase):
    def test_the_first_claim_is_granted(self):
        db = self._db()

        self.assertTrue(db.repo.claimTrackMergeRun(now=NOW))

    def test_a_second_claim_the_same_day_is_refused(self):
        db = self._db()
        db.repo.claimTrackMergeRun(now=NOW)

        self.assertFalse(db.repo.claimTrackMergeRun(now=NOW + 60))

    def test_a_claim_a_second_short_of_the_interval_is_refused(self):
        db = self._db()
        db.repo.claimTrackMergeRun(now=NOW)

        self.assertFalse(db.repo.claimTrackMergeRun(
            now=NOW + TRACK_MERGE_MIN_INTERVAL_SECONDS - 1))

    def test_a_claim_once_the_interval_has_passed_is_granted(self):
        db = self._db()
        db.repo.claimTrackMergeRun(now=NOW)

        self.assertTrue(db.repo.claimTrackMergeRun(
            now=NOW + TRACK_MERGE_MIN_INTERVAL_SECONDS))

    def test_a_granted_claim_restarts_the_clock(self):
        """The stamp is the moment of the claim, not of the first one ever -
        otherwise the slot would drift to a fixed time of day and a run that
        started late would shorten the next gap."""
        db = self._db()
        db.repo.claimTrackMergeRun(now=NOW)
        db.repo.claimTrackMergeRun(now=NOW + TRACK_MERGE_MIN_INTERVAL_SECONDS)

        self.assertFalse(db.repo.claimTrackMergeRun(
            now=NOW + TRACK_MERGE_MIN_INTERVAL_SECONDS + 60))


class TestTheSlotIsSharedAndDurable(CadenceTestCase):
    def test_only_one_of_three_concurrent_claims_wins(self):
        """The three per-user backfillers reach the same instance-wide matcher.
        The claim is one conditional UPDATE, so the database settles it - a
        read-then-write would let all three through."""
        db = self._db()
        results = []
        guard = threading.Lock()
        barrier = threading.Barrier(WORKERS)

        def claim():
            try:
                barrier.wait()
                granted = db.repo.claimTrackMergeRun(now=NOW)
                with guard:
                    results.append(granted)
            finally:
                #< ConnectionManager keeps one connection per THREAD, so each
                #  worker has to hand its own back - an open handle here keeps
                #  the temp database file locked and fails the teardown, not
                #  the assertion
                db.repo.connectionManager.close()

        threads = [threading.Thread(target=claim) for _ in range(WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results.count(True), 1, results)
        self.assertEqual(len(results), WORKERS)

    def test_the_stamp_lives_in_the_database_not_the_process(self):
        """A restart must not hand back a slot that was already spent."""
        db = self._db()
        db.repo.claimTrackMergeRun(now=NOW)

        self.assertEqual(float(db.repo.getAppSetting(TRACK_MERGE_LAST_RUN_KEY)), NOW)

    def test_an_unreadable_stamp_does_not_wedge_the_matcher(self):
        """Failing open: a value that is not a number reads as "never ran", so
        the worst case is one extra pass rather than a feature that is silently
        off forever."""
        db = self._db()
        db.repo.setAppSetting(TRACK_MERGE_LAST_RUN_KEY, "not-a-timestamp")

        self.assertTrue(db.repo.claimTrackMergeRun(now=NOW))


class TestTheAdminRunOwnsTheSlotItUses(CadenceTestCase):
    """Turning the checkbox on runs the matcher then and there (routes/admin.py)
    - that is what makes the toggle feel like a switch. It takes the day's slot
    with it, so the backfiller does not repeat a pass that just happened."""

    def test_a_stamped_run_refuses_the_next_claim(self):
        db = self._db()
        db.repo.stampTrackMergeRun(now=NOW)

        self.assertFalse(db.repo.claimTrackMergeRun(now=NOW + 60))

    def test_a_stamp_overrides_an_older_one(self):
        db = self._db()
        db.repo.claimTrackMergeRun(now=NOW)
        db.repo.stampTrackMergeRun(now=NOW + TRACK_MERGE_MIN_INTERVAL_SECONDS)

        self.assertFalse(db.repo.claimTrackMergeRun(
            now=NOW + TRACK_MERGE_MIN_INTERVAL_SECONDS + 60))


class TestTheLoopIsWiredToIt(CadenceTestCase):
    def test_the_loop_gates_the_matcher_on_the_claim(self):
        """Same shape as the toggle's own source assertion: the cadence is
        worth nothing if the loop stops asking."""
        import inspect
        from Database.workers.metadata_backfiller import MetadataBackfillMixin
        source = inspect.getsource(MetadataBackfillMixin._metadataBackfillLoop)

        self.assertIn("isTrackMergeEnabled", source)
        self.assertIn("claimTrackMergeRun", source)
        self.assertIn("mergeTracksByIsrc", source)

    def test_the_admin_enable_path_stamps_the_run(self):
        import inspect
        import routes.admin
        source = inspect.getsource(routes.admin)

        self.assertIn("stampTrackMergeRun", source)


if __name__ == "__main__":
    unittest.main()
