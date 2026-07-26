"""The shared periodic-worker lifecycle (Database/workers/periodic.py).

Five workers had five copies of start/stop, and they had drifted: the three
Last.fm ones were hardened to hold a lock across the check-and-assign, and the
Spotify metadata and Wrapped workers never got that fix. They all run one
implementation now, so these pin what that implementation guarantees - the
locking especially, since that is the part the other two gained.

The concurrency tests here hold the lock for as long as they like and assert an
ordering invariant, rather than racing two starts and hoping the interleaving
shows up. A test that waits on a clock to catch a race catches it sometimes.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase


class PeriodicWorkerTestCase(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.db = self._makeDb({}, [])   #< startWorkers=False: nothing running yet

    def _idleLoop(self, stopEvent):
        stopEvent.wait(30)


class TestAStoppingInstanceStartsNothing(PeriodicWorkerTestCase):
    """startListener refuses permanently once signalStop has run ("a signaled
    instance never starts a listener again"), but the periodic lifecycle checked
    neither flag. Shutdown is two phases over a snapshot of user_databases:
    signal every user, then join every user. A start landing after phase 1 -
    a /profile key save, or _checkLoginLoop constructing a whole new Database
    after the snapshot was taken - created its worker on a FRESH, unset event,
    which nothing would ever set and nothing would ever join. The result is
    Last.fm HTTP and SQLite writes continuing from daemon threads through
    container teardown, which is the neighbourhood of the 2026-07-17 hang."""

    def test_a_start_after_signal_stop_is_a_noop(self):
        self.db.signalStop()

        self.db._startPeriodicWorker("wrapped_thread", "wrapped_stop_event",
                                      self._idleLoop, "test-worker")

        self.assertIsNone(self.db.wrapped_thread)

    def test_a_start_after_the_app_wide_shutdown_event_is_a_noop(self):
        """The other half of _stopRequested: this instance was never stopped
        itself, but the app is going down."""
        self.db.shutdown_event.set()

        self.db._startPeriodicWorker("wrapped_thread", "wrapped_stop_event",
                                      self._idleLoop, "test-worker")

        self.assertIsNone(self.db.wrapped_thread)

    def test_a_normal_start_still_works(self):
        self.db._startPeriodicWorker("wrapped_thread", "wrapped_stop_event",
                                      self._idleLoop, "test-worker")
        self.addCleanup(self.db._stopPeriodicWorker, "wrapped_thread", "wrapped_stop_event")

        self.assertIsNotNone(self.db.wrapped_thread)

    def test_the_named_worker_starts_are_gated_too(self):
        """The gate belongs in the shared helper, not in each of the five
        callers - this pins that they all inherit it."""
        self.db.repo.updateUserLastfmApiKey(self.db.user, "key123")
        self.db.signalStop()

        self.db.startLastfmGenreBackfiller()
        self.db.startLastfmBiographyBackfiller()
        self.db.startLastfmAlbumBiographyBackfiller()
        self.db.startWrappedCalculationsWorker()
        self.db.startMetadataBackfiller()

        for attr in ("lastfm_thread", "lastfm_biography_thread",
                     "lastfm_album_biography_thread", "wrapped_thread", "backfiller_thread"):
            self.assertIsNone(getattr(self.db, attr), f"{attr} was started during shutdown")


class TestTheLockCoversCheckAndAssign(PeriodicWorkerTestCase):
    """The race the Last.fm workers were fixed for, now covering all of them:
    two concurrent starts could both see no live thread, and the second's
    assignment would overwrite the first's - orphaning a running thread on an
    event no reference survives for, so no stop path could ever reach it."""

    def test_the_precondition_is_consulted_with_the_lock_held(self):
        """The precondition runs between the check and the assign, so if it
        sees the lock held, so does everything around it."""
        heldDuringPrecondition = []

        def precondition():
            heldDuringPrecondition.append(self.db._worker_lock.locked())
            return True

        self.db._startPeriodicWorker("wrapped_thread", "wrapped_stop_event",
                                      self._idleLoop, "test-worker", precondition=precondition)
        self.addCleanup(self.db._stopPeriodicWorker, "wrapped_thread", "wrapped_stop_event")

        self.assertEqual(heldDuringPrecondition, [True])

    def test_a_start_cannot_assign_while_another_holds_the_lock(self):
        """Holding the lock stands in for a start already in flight. The
        blocked start must not have created or assigned anything - and must
        complete normally once the lock is free."""
        entered = threading.Event()
        finished = threading.Event()

        def attempt():
            entered.set()
            self.db.startWrappedCalculationsWorker()
            finished.set()

        contender = threading.Thread(target=attempt, daemon=True)
        with self.db._worker_lock:
            contender.start()
            self.assertTrue(entered.wait(timeout=5))
            # It can never get past the lock while we hold it, however long we
            # wait - so this assertion cannot fail spuriously.
            self.assertFalse(finished.wait(timeout=0.3))
            self.assertIsNone(self.db.wrapped_thread)

        self.assertTrue(finished.wait(timeout=5))
        self.assertIsNotNone(self.db.wrapped_thread)
        self.db.stopWrappedCalculationsWorker()

    def test_the_lock_is_released_before_the_join(self):
        """A join can block for seconds; nothing else should have to wait on
        it. If stop held the lock across the join, this would deadlock."""
        self.db.startWrappedCalculationsWorker()
        self.db.stopWrappedCalculationsWorker()

        self.assertTrue(self.db._worker_lock.acquire(blocking=False))
        self.db._worker_lock.release()


class TestStartStopContract(PeriodicWorkerTestCase):
    def test_a_second_start_leaves_the_running_thread_alone(self):
        self.db.startWrappedCalculationsWorker()
        self.addCleanup(self.db.stopWrappedCalculationsWorker)
        running = self.db.wrapped_thread

        self.db.startWrappedCalculationsWorker()

        self.assertIs(self.db.wrapped_thread, running)

    def test_a_vetoed_start_creates_nothing(self):
        """What keeps a keyless user from getting an idle Last.fm thread."""
        self.db._startPeriodicWorker("wrapped_thread", "wrapped_stop_event",
                                      self._idleLoop, "test-worker",
                                      precondition=lambda: False)

        self.assertIsNone(self.db.wrapped_thread)

    def test_each_run_gets_its_own_event(self):
        """stop() joins with a timeout, so a worker blocked in slow I/O can
        outlive it. Clearing a shared event would revive that zombie alongside
        the new thread; its own event stays set, so it exits instead."""
        self.db.startWrappedCalculationsWorker()
        firstEvent = self.db.wrapped_stop_event
        self.db.stopWrappedCalculationsWorker()

        self.db.startWrappedCalculationsWorker()
        self.addCleanup(self.db.stopWrappedCalculationsWorker)

        self.assertIsNot(self.db.wrapped_stop_event, firstEvent)
        self.assertTrue(firstEvent.is_set())
        self.assertFalse(self.db.wrapped_stop_event.is_set())

    def test_stop_clears_the_thread_so_a_restart_is_possible(self):
        self.db.startWrappedCalculationsWorker()

        self.db.stopWrappedCalculationsWorker()

        self.assertIsNone(self.db.wrapped_thread)

    def test_stopping_something_never_started_is_a_no_op(self):
        self.db.stopWrappedCalculationsWorker()   #< must not raise

        self.assertIsNone(self.db.wrapped_thread)

    def test_a_database_without_the_attributes_is_a_silent_no_op(self):
        """A partially built instance must not crash __init__ - every copy of
        this behaved that way, and Database starts workers from its own
        constructor."""
        del self.db.wrapped_thread

        self.db.startWrappedCalculationsWorker()   #< must not raise
        self.db._stopPeriodicWorker("wrapped_thread", "wrapped_stop_event")

        self.assertFalse(hasattr(self.db, "wrapped_thread"))


class TestWorkerStatus(PeriodicWorkerTestCase):
    def test_running_tracks_the_thread(self):
        self.assertFalse(self.db.getWrappedWorkerStatus()["running"])

        self.db.startWrappedCalculationsWorker()
        self.addCleanup(self.db.stopWrappedCalculationsWorker)

        self.assertTrue(self.db.getWrappedWorkerStatus()["running"])

    def test_configured_is_the_workers_own_question(self):
        """Wrapped needs no credentials, so it is always configured; the
        Spotify backfiller's answer follows the stored credentials."""
        self.assertTrue(self.db.getWrappedWorkerStatus()["configured"])
        self.assertEqual(
            self.db.getSpotifyApiWorkerStatus()["configured"],
            bool(self.db.repo.getUserSpotifyCredentials(self.db.user)))

    def test_the_status_carries_the_telemetry_the_overview_renders(self):
        status = self.db.getWrappedWorkerStatus()

        self.assertIn("running", status)
        self.assertIn("configured", status)
        self.assertIn("lastCycleAt", {**status, "lastCycleAt": None})   #< shape, not value
        self.assertEqual(set(status) - {"running", "configured"},
                         set(self.db._getWorkerTelemetry("wrapped")))


if __name__ == "__main__":
    unittest.main()
