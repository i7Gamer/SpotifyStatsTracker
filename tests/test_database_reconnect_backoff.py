"""Tests for Database listener reconnection with exponential backoff.

When a listener's onStale callback is triggered (due to stale feed or auth error),
the reconnection should retry with exponential backoff before giving up. This tests
that backoff behavior and proper error logging.
"""
import sys
import os
import unittest
import threading
import time
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.database import Database


class TestReconnectBackoff(unittest.TestCase):
    def _makeTestDb(self):
        """Create a Database instance with mocked repository and file operations."""
        with patch('Database.database.Repository'), \
             patch('Database.database.AutoImporter'), \
             patch('Database.database.Path.exists', return_value=False), \
             patch.dict(os.environ, {}, clear=False):
            db = Database(user="TestUser", email="test@example.com")
        self.addCleanup(db.stop)
        return db

    def test_an_instance_stop_interrupts_the_backoff(self):
        """Stopping ONE user's Database must abort its reconnect wait, not leave
        the thread asleep until the backoff expires.

        The wait used to be on `shutdown_event`, which is the APP-WIDE exit
        signal shared by every user. An app shutdown sets it, so that case
        aborted promptly - but a per-instance stop (a logout, a listener
        rebuild, db.stop()) only sets `_stopping`, which was checked AFTER the
        wait returned. With RECONNECT_MAX_DELAY at 300s that is a five-minute
        zombie thread holding a session for a user who is already gone, and it
        reconnects once more on the way out.

        Deterministic rather than timed: the backoff is pushed far beyond any
        plausible test runtime, so a thread that is still alive after the stop
        can only be one that waited on the wrong event."""
        db = self._makeTestDb()
        attempted = threading.Event()

        def failOnce(*args, **kwargs):
            attempted.set()
            raise RuntimeError("reconnect failed, forcing a backoff")

        with patch.object(type(db), "RECONNECT_INITIAL_DELAY", 3600), \
             patch.object(type(db), "RECONNECT_MAX_DELAY", 3600), \
             patch.object(db, "startListener", side_effect=failOnce):
            callback = db._makeOnStaleCallback()
            worker = threading.Thread(target=callback, daemon=True)
            worker.start()

            #< attempt 0 runs with no backoff; the wait we care about is the one
            #  before attempt 1
            self.assertTrue(attempted.wait(timeout=5), "the first reconnect never ran")

            db.signalStop()
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive(),
                             "the reconnect backoff ignored this instance's stop - it is waiting on "
                             "the app-wide shutdown_event, so only a whole-app exit can interrupt it")

    def test_a_backoff_superseded_by_a_fresh_listener_gives_up(self):
        """The re-login path that an instance stop does NOT cover.

        _refresh_user_session (dashboard/user_registry.py) handles a user
        re-logging in with fresh cookies: it stops the listener and starts a
        new one. It deliberately does not mark the instance stopped - signalStop
        sets _stopping, which is never cleared, so that would refuse this user a
        listener for the rest of the process's life.

        So a backoff parked in _waitForStop saw no stop requested, slept out its
        full RECONNECT_MAX_DELAY, woke up, and rebuilt on top of the healthy
        session the re-login had just built - a second spotapi login, a second
        TLS client, and a listener_session_builds bump that the /admin ledger
        then reports as unexplained churn.

        A generation counter is what distinguishes the two: a stop is permanent,
        being superseded is not.

        Deterministic rather than timed - the backoff is pushed far beyond any
        plausible test runtime, so a thread that ends can only have been woken."""
        db = self._makeTestDb()
        attempted = threading.Event()

        def failOnce(*args, **kwargs):
            attempted.set()
            raise RuntimeError("reconnect failed, forcing a backoff")

        with patch.object(type(db), "RECONNECT_INITIAL_DELAY", 3600), \
             patch.object(type(db), "RECONNECT_MAX_DELAY", 3600), \
             patch.object(db, "startListener", side_effect=failOnce) as mockStart:
            callback = db._makeOnStaleCallback()
            worker = threading.Thread(target=callback, daemon=True)
            worker.start()

            self.assertTrue(attempted.wait(timeout=5), "the first reconnect never ran")
            attemptsBeforeRelogin = mockStart.call_count

            #< exactly what a successful startListener does at its swap
            db.noteListenerSuperseded()

            worker.join(timeout=5)

            self.assertFalse(worker.is_alive(),
                             "a superseded reconnect backoff is still asleep - a re-login cannot "
                             "signalStop (that is permanent), so nothing else will wake it")
            self.assertEqual(attemptsBeforeRelogin, mockStart.call_count,
                             "the superseded backoff rebuilt a listener on top of the fresh one")

    def test_being_superseded_does_not_stop_the_instance(self):
        """The whole point of not reusing signalStop: this user must still be
        able to start listeners afterwards."""
        db = self._makeTestDb()

        db.noteListenerSuperseded()

        self.assertFalse(db._stopRequested())

    def test_a_later_backoff_still_waits_after_an_earlier_one_was_superseded(self):
        """The wake is an event, and an event left set would make every
        subsequent wait return instantly - turning the backoff into a spin that
        burns RECONNECT_MAX_RETRIES against Spotify with no delay at all."""
        db = self._makeTestDb()
        db.noteListenerSuperseded()

        #< a fresh reconnect run, captured after the supersede
        generation = db._listenerGeneration
        started = time.monotonic()
        interrupted = db._waitForStop(0.25, generation)

        self.assertFalse(interrupted)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_a_signalled_instance_does_not_start_an_auto_importer(self):
        """The same rule startListener follows: a signalled instance never
        starts a thread again.

        A watchdog begun after shutdown() took its snapshot of who to join is a
        thread nothing will stop - it outlives the phase meant to end it, and
        the process then waits out its grace period on a worker that started
        after the exit began."""
        db = self._makeTestDb()
        db.autoImporter = MagicMock()

        db.signalStop()
        db.startAutoImporter()

        db.autoImporter.start.assert_not_called()

    def test_the_auto_importer_starts_normally_when_nothing_is_stopping(self):
        """The negative control: without it the guard above passes even if
        startAutoImporter became an unconditional no-op."""
        db = self._makeTestDb()
        db.autoImporter = MagicMock()

        db.startAutoImporter()

        db.autoImporter.start.assert_called_once()

    def test_exponential_backoff_calculation(self):
        """Verify exponential backoff delay calculation is correct."""
        db = self._makeTestDb()

        # Verify delay calculation logic without actually sleeping
        for attempt in range(db.RECONNECT_MAX_RETRIES):
            delay = min(
                db.RECONNECT_INITIAL_DELAY * (2 ** attempt),
                db.RECONNECT_MAX_DELAY
            )
            # First attempt should be 1s, then 2s, 4s, 8s, etc, capped at 300s
            if attempt == 0:
                self.assertEqual(delay, 1)
            elif attempt == 1:
                self.assertEqual(delay, 2)
            elif attempt == 2:
                self.assertEqual(delay, 4)
            # Later attempts should be capped
            self.assertLessEqual(delay, db.RECONNECT_MAX_DELAY)

    def test_startListener_uses_onStale_callback_with_backoff(self):
        """startListener should use the wrapped onStale callback with backoff."""
        db = self._makeTestDb()

        with patch.object(db, '_withCookiesFile') as mock_cookies, \
             patch('Database.database.Listener') as MockListener:
            mock_listener = MagicMock()
            mock_listener.contaminationDetected = False  #< a bare MagicMock's auto-attribute is truthy = contaminated
            mock_listener.loginFailed = False  #< same trap - see above
            MockListener.return_value = mock_listener
            mock_cookies.return_value = mock_listener

            db.startListener(email="test@example.com")

            # Verify startListener_thread was called with onStale callback
            mock_listener.startListener_thread.assert_called_once()
            call_kwargs = mock_listener.startListener_thread.call_args[1]
            self.assertIn('onStale', call_kwargs)

            # The onStale callback should be callable (it's the result of _makeOnStaleCallback)
            onStale_callback = call_kwargs['onStale']
            self.assertTrue(callable(onStale_callback))


class TestReconnectShutdownGate(unittest.TestCase):
    """A stale-feed reconnect racing shutdown used to resurrect a listener
    nothing could reach (the 2026-07-17 hang) - onStale must abandon
    reconnection as soon as stop/shutdown is requested."""

    def _makeTestDb(self):
        with patch('Database.database.Repository'), \
             patch('Database.database.AutoImporter'), \
             patch('Database.database.Path.exists', return_value=False), \
             patch.dict(os.environ, {}, clear=False):
            db = Database(user="TestUser", email="test@example.com")
        self.addCleanup(db.stop)
        return db

    def test_onstale_aborts_immediately_when_shutting_down(self):
        db = self._makeTestDb()
        db.shutdown_event.set()

        with patch.object(db, "startListener") as mockStart:
            db._makeOnStaleCallback()()

        mockStart.assert_not_called()

    def test_onstale_aborts_when_stopping(self):
        db = self._makeTestDb()
        db._stopping = True

        with patch.object(db, "startListener") as mockStart:
            db._makeOnStaleCallback()()

        mockStart.assert_not_called()

    def test_onstale_abandons_when_startListener_reports_stop(self):
        """startListener returning False means 'stop requested' - no retries."""
        db = self._makeTestDb()

        with patch.object(db, "startListener", return_value=False) as mockStart:
            db._makeOnStaleCallback()()

        mockStart.assert_called_once()

    def test_onstale_backoff_waits_on_an_interruptible_event(self):
        """The between-attempt backoff must wait on an EVENT (interruptible) and
        abandon reconnection when it fires - not sleep out up to
        RECONNECT_MAX_DELAY and reconnect anyway.

        The event is _stopEvent, not shutdown_event, and that distinction is the
        point: shutdown_event is shared app-wide, so waiting on it meant only a
        whole-app exit could interrupt the backoff and a single user's stop was
        ignored for up to five minutes. signalStop() sets _stopEvent, and app
        shutdown calls signalStop() on every user, so both paths still abort
        promptly - see test_an_instance_stop_interrupts_the_backoff."""
        db = self._makeTestDb()
        db.shutdown_event = MagicMock()
        db.shutdown_event.is_set.return_value = False
        db._stopEvent = MagicMock()

        #< the stop has to land DURING the wait, not before it: _waitForStop
        #  checks the flags up front too, and a stop already in place would
        #  abort at the top of the attempt without ever reaching the backoff
        def stopArrivesMidWait(timeout=None):
            db._stopping = True
        db._stopEvent.wait.side_effect = stopArrivesMidWait

        with patch.object(db, "startListener",
                          side_effect=RuntimeError("still down")) as mockStart, \
             patch("Database.database.time.sleep") as mockSleep:
            db._makeOnStaleCallback()()

        mockStart.assert_called_once()          # attempt 1 failed...
        db._stopEvent.wait.assert_called_once()  # ...the backoff waited on the event...
        mockSleep.assert_not_called()            # ...never via a blind sleep


class TestReconnectLogVolume(unittest.TestCase):
    """A user who simply isn't listening triggers a full reconnect cycle every
    30 minutes by design (the stale-feed timeout). Across 3 users and 11 days
    that produced ~4,200 INFO/WARNING lines in app.log - "Recently-played feed
    unchanged", "Attempting to reconnect", "Listener initialized",
    "Reconnection succeeded" - all describing the system working correctly,
    while burying the failures worth reading.

    A clean reconnect is therefore silent at INFO. Anything that needed retries,
    or failed, still speaks up."""

    def _makeTestDb(self):
        with patch('Database.database.Repository'), \
             patch('Database.database.AutoImporter'), \
             patch('Database.database.Path.exists', return_value=False), \
             patch.dict(os.environ, {}, clear=False):
            db = Database(user="TestUser", email="test@example.com")
        self.addCleanup(db.stop)
        return db

    def test_clean_first_attempt_reconnect_logs_nothing_at_info(self):
        db = self._makeTestDb()

        with patch.object(db, "startListener", return_value=True):
            with self.assertNoLogs("Database.database", level="INFO"):
                db._makeOnStaleCallback()()

    def test_reconnect_that_needed_retries_is_reported_at_info(self):
        """The one line worth keeping: a reconnect that didn't work first time
        says so, so a degrading session is still visible without DEBUG."""
        db = self._makeTestDb()
        db.shutdown_event = MagicMock()
        db.shutdown_event.is_set.return_value = False
        #< the backoff waits on _stopEvent (see _waitForStop); mocked so the
        #  between-attempt delay elapses instantly instead of really sleeping
        db._stopEvent = MagicMock()  #< backoff elapses normally

        attempts = [RuntimeError("still down"), True]

        def flakyStart(*args, **kwargs):
            result = attempts.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(db, "startListener", side_effect=flakyStart):
            with self.assertLogs("Database.database", level="INFO") as cm:
                db._makeOnStaleCallback()()

        self.assertTrue(any("Reconnection succeeded on attempt 2" in m for m in cm.output))

    def test_total_reconnect_failure_still_reaches_error(self):
        db = self._makeTestDb()
        db.shutdown_event = MagicMock()
        db.shutdown_event.is_set.return_value = False
        #< the backoff waits on _stopEvent (see _waitForStop); mocked so the
        #  between-attempt delay elapses instantly instead of really sleeping
        db._stopEvent = MagicMock()

        with patch.object(db, "startListener", side_effect=RuntimeError("down")):
            with self.assertLogs("Database.database", level="ERROR") as cm:
                db._makeOnStaleCallback()()

        self.assertTrue(any("Reconnection failed after" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main()
