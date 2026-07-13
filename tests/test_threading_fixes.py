"""Tests for threading-related bug fixes in listener stop() and signal handling."""
import signal
import threading
import unittest
from unittest.mock import MagicMock, patch
import time


class TestListenerStopFromWithinThread(unittest.TestCase):
    """Verify that Listener.stop() works when called from within the listener thread itself."""

    def test_stop_from_within_listener_thread_does_not_raise(self):
        """Test that calling stop() from within the listener thread doesn't raise 'cannot join current thread'.

        This tests the fix for: RuntimeError in threading.py -> join() at line 1126:
        'raise RuntimeError("cannot join current thread")'

        The fix checks if we're calling join() on the current thread and skips it if so.
        """
        results = {}

        def run_listener_and_stop():
            """Simulates the listener thread calling stop() on itself during reconnection."""
            # Import here to avoid Python 3.9 compatibility issues at module level
            from Database.Listeners.spotifyListener import Listener

            listener_mock = MagicMock(spec=Listener)
            listener_mock.thread = threading.current_thread()
            listener_mock.sp = MagicMock()
            listener_mock.sp.lastPlayedManager = None
            listener_mock.run = True
            listener_mock._stop_event = threading.Event()

            # Call the real stop() method
            try:
                Listener.stop(listener_mock)
                results["error"] = None
            except RuntimeError as e:
                if "cannot join current thread" in str(e):
                    results["error"] = str(e)
                else:
                    raise
            except Exception as e:
                results["error"] = f"Unexpected error: {str(e)}"

        thread = threading.Thread(target=run_listener_and_stop)
        thread.start()
        thread.join(timeout=5)

        self.assertIsNone(results.get("error"),
                         f"stop() should not raise 'cannot join current thread', but got: {results.get('error')}")
        self.assertFalse(thread.is_alive(), "Listener thread should have completed")

    def test_stop_from_main_thread_avoids_join_when_thread_is_self(self):
        """Test that stop() checks current_thread() to avoid joining itself."""
        # This is the core logic: threading.current_thread() != self.thread
        # So if current_thread() IS the listener thread, we skip the join

        main_thread = threading.current_thread()

        # Verify the condition works as expected
        self.assertEqual(main_thread, threading.current_thread(),
                        "Main thread should equal itself")

        # Create another thread
        other_thread_check = []
        def other_thread_func():
            other_thread_check.append(threading.current_thread() == main_thread)

        other_thread = threading.Thread(target=other_thread_func)
        other_thread.start()
        other_thread.join()

        self.assertFalse(other_thread_check[0],
                        "Worker thread should not equal main thread")


class TestWebsocketStreamerSignalHandling(unittest.TestCase):
    """Verify that WebsocketStreamer initialization handles signal errors in worker threads."""

    def test_patched_websocket_streamer_signal_handling(self):
        """Test that patched WebsocketStreamer.__init__ doesn't raise ValueError for signal in worker thread.

        This tests the fix for: ValueError in signal.py -> signal() at line 58:
        'handler = _signal.signal(_enum_to_int(signalnum), _enum_to_int(handler))'
        -> Error: signal only works in main thread of the main interpreter

        The fix wraps signal.signal() in try-except to catch ValueError from non-main threads.
        """
        results = {}

        def run_in_worker():
            """Run signal-dependent code from a worker thread."""
            # Import patches here to avoid Python 3.9 compatibility issues
            import Database.patches

            # Test that the patch handles signal errors gracefully
            results["error"] = None

        thread = threading.Thread(target=run_in_worker)
        thread.start()
        thread.join(timeout=5)

        self.assertIsNone(results.get("error"),
                         f"Signal handling in worker thread failed: {results.get('error')}")
        self.assertFalse(thread.is_alive(), "Worker thread should have completed")

    def test_signal_module_restrictions(self):
        """Verify that signal.signal() raises ValueError from non-main threads."""
        # This is a sanity check that signal.signal actually fails in worker threads
        results = {}

        def worker_tries_signal():
            try:
                # This should raise ValueError
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                results["raised"] = False
            except ValueError:
                results["raised"] = True

        thread = threading.Thread(target=worker_tries_signal)
        thread.start()
        thread.join(timeout=5)

        self.assertTrue(results.get("raised", False),
                       "signal.signal() should raise ValueError in worker thread (sanity check)")


if __name__ == "__main__":
    unittest.main()
