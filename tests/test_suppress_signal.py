"""Tests for the _suppress_signal_in_thread context manager in spotifyListener."""
import signal
import threading
import unittest
from unittest.mock import patch, MagicMock

from Database.Listeners.spotifyListener import _suppress_signal_in_thread


class TestSuppressSignalInThread(unittest.TestCase):
    """Verify that _suppress_signal_in_thread correctly patches signal.signal
    when running on a non-main thread and is a no-op on the main thread."""

    def test_main_thread_does_not_patch(self):
        """On the main thread the context manager should not alter signal.signal."""
        original = signal.signal
        with _suppress_signal_in_thread():
            self.assertIs(signal.signal, original)
        self.assertIs(signal.signal, original)

    def test_worker_thread_suppresses_sigint(self):
        """On a worker thread, SIGINT registration should be silently skipped."""
        results = {}

        def _run():
            with _suppress_signal_in_thread():
                # Attempt to register a SIGINT handler – should NOT raise
                try:
                    ret = signal.signal(signal.SIGINT, signal.SIG_DFL)
                    results["raised"] = False
                    results["returned"] = ret
                except ValueError:
                    results["raised"] = True

        t = threading.Thread(target=_run)
        t.start()
        t.join()

        self.assertFalse(results.get("raised", True),
                         "signal.signal(SIGINT, ...) should not raise in the patched context")

    def test_worker_thread_suppresses_non_sigint(self):
        """On a worker thread, non-SIGINT signals should also be suppressed and not raise ValueError."""
        results = {}

        def _run():
            with _suppress_signal_in_thread():
                try:
                    ret = signal.signal(signal.SIGTERM, signal.SIG_DFL)
                    results["raised"] = False
                    results["returned"] = ret
                except ValueError:
                    results["raised"] = True

        t = threading.Thread(target=_run)
        t.start()
        t.join()

        self.assertFalse(results.get("raised", True),
                         "non-SIGINT signals should also be suppressed in the patched context")

    def test_signal_restored_after_context(self):
        """signal.signal should be restored to its original value after the
        context manager exits, even on a worker thread."""
        results = {}

        def _run():
            original = signal.signal
            with _suppress_signal_in_thread():
                pass
            results["restored"] = signal.signal is original

        t = threading.Thread(target=_run)
        t.start()
        t.join()

        self.assertTrue(results.get("restored", False),
                        "signal.signal should be restored after context exit")

    def test_overlapping_worker_contexts_never_leave_the_stub_installed(self):
        """Two Listener constructions can overlap (a reconnect thread and a
        login thread - the module's whole shutdown design exists because they
        do). The old capture-and-restore let thread B capture thread A's stub
        as 'original' (B's probe called the stub, didn't raise, so B installed
        nothing) and then B's finally re-installed that stub AFTER A had
        restored the real function - permanently replacing signal.signal with
        the no-op for the life of the process. Event-ordered to force exactly
        that interleaving: A enter, B enter, A exit, B exit."""
        realSignal = signal.signal
        aEntered, aMayExit, aDone = threading.Event(), threading.Event(), threading.Event()

        def workerA():
            with _suppress_signal_in_thread():
                aEntered.set()
                aMayExit.wait(5)
            aDone.set()

        def workerB():
            aEntered.wait(5)
            with _suppress_signal_in_thread():   #< entered while A holds the patch
                aMayExit.set()
                aDone.wait(5)                    #< A restores while B is still inside

        a = threading.Thread(target=workerA)
        b = threading.Thread(target=workerB)
        a.start()
        b.start()
        a.join(5)
        b.join(5)

        self.assertFalse(a.is_alive() or b.is_alive(), "test threads deadlocked")
        self.assertIs(signal.signal, realSignal,
                      "the suppression stub survived past every context exit")

    def test_main_thread_registration_still_works_while_a_worker_suppresses(self):
        """The patch is process-global but the reason for it ('this thread
        cannot register handlers') is thread-local: while a worker holds the
        suppression, a genuine main-thread signal.signal must still register,
        not be silently swallowed."""
        entered, release = threading.Event(), threading.Event()

        def worker():
            with _suppress_signal_in_thread():
                entered.set()
                release.wait(5)

        def marker(signum, frame):   # pragma: no cover - never invoked
            return None

        t = threading.Thread(target=worker)
        t.start()
        self.assertTrue(entered.wait(5))
        previous = signal.getsignal(signal.SIGTERM)
        try:
            signal.signal(signal.SIGTERM, marker)   #< on the MAIN thread, mid-suppression
            self.assertIs(signal.getsignal(signal.SIGTERM), marker,
                          "a main-thread registration was swallowed by the worker's stub")
        finally:
            signal.signal(signal.SIGTERM, previous)
            release.set()
            t.join(5)

    def test_signal_restored_on_exception(self):
        """signal.signal should be restored even if an exception occurs inside
        the context manager."""
        results = {}

        def _run():
            original = signal.signal
            try:
                with _suppress_signal_in_thread():
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            results["restored"] = signal.signal is original

        t = threading.Thread(target=_run)
        t.start()
        t.join()

        self.assertTrue(results.get("restored", False),
                        "signal.signal should be restored after exception")


if __name__ == "__main__":
    unittest.main()
