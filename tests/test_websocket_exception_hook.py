"""Tests for _shutdown_exception_hook, the threading.excepthook that logs
expected websocket-close exceptions from background threads (e.g. spotapi's
keep_alive ping) as one clean line instead of letting Python print a raw
traceback. Exercises the real hook, not a re-implementation of its logic."""
import contextlib
import io
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import websockets.exceptions
import websockets.frames

import Database.Listeners.spotifyListener as listenerModule
from Database.Listeners.spotifyListener import _shutdown_exception_hook


def _makeArgs(exc, threadName="worker-thread"):
    return SimpleNamespace(exc_type=type(exc), exc_value=exc, exc_traceback=None,
                            thread=SimpleNamespace(name=threadName))


class TestWebsocketExceptionHook(unittest.TestCase):
    """Verify harmless/expected websocket-close exceptions are logged instead
    of dumped as a raw traceback, while anything else still is."""

    def _invoke(self, exc):
        with patch("Database.Listeners.spotifyListener._PREVIOUS_EXCEPTHOOK") as mockDefaultHook, \
             self.assertLogs("Database.Listeners.spotifyListener", level="WARNING") as logs:
            _shutdown_exception_hook(_makeArgs(exc))
        return mockDefaultHook, logs.output

    def test_suppresses_connection_closed_ok(self):
        exc = websockets.exceptions.ConnectionClosedOK(None, None)
        mockDefaultHook, logOutput = self._invoke(exc)
        mockDefaultHook.assert_not_called()
        self.assertTrue(any("worker-thread" in line for line in logOutput))

    def test_suppresses_connection_closed_error_graceful_close(self):
        close = websockets.frames.Close(1000, "OK")
        exc = websockets.exceptions.ConnectionClosedOK(close, close, rcvd_then_sent=True)
        mockDefaultHook, _ = self._invoke(exc)
        mockDefaultHook.assert_not_called()

    def test_suppresses_connection_closed_error_abnormal_close(self):
        """Previously only a status-1000 (graceful) close was suppressed; an
        abnormal close used to fall through to a raw traceback even though the
        same reconnect/stale-feed recovery handles either case."""
        close = websockets.frames.Close(1006, "ABNORMAL CLOSURE")
        exc = websockets.exceptions.ConnectionClosedError(close, None)
        mockDefaultHook, _ = self._invoke(exc)
        mockDefaultHook.assert_not_called()

    def test_suppresses_real_world_no_close_frame_error(self):
        """Regression test for the production traceback: a keep-alive ping hit
        ConnectionAbortedError, which surfaced as this exact ConnectionClosedError
        ('no close frame received or sent') and used to print a raw traceback."""
        exc = websockets.exceptions.ConnectionClosedError(None, None)
        self.assertEqual(str(exc), "no close frame received or sent")
        mockDefaultHook, logOutput = self._invoke(exc)
        mockDefaultHook.assert_not_called()
        self.assertTrue(any("no close frame received or sent" in line for line in logOutput))

    def test_suppresses_connection_aborted_error(self):
        exc = ConnectionAbortedError("[Errno 10053] An established connection was aborted")
        mockDefaultHook, _ = self._invoke(exc)
        mockDefaultHook.assert_not_called()

    def test_does_not_suppress_unrelated_exceptions(self):
        """A genuine bug (anything other than a websocket close) must still
        surface loudly - forwarded whole, so the receiving hook keeps the
        thread it came from."""
        exc = TypeError("unexpected")
        args = _makeArgs(exc)
        with patch("Database.Listeners.spotifyListener._PREVIOUS_EXCEPTHOOK") as mockDefaultHook:
            _shutdown_exception_hook(args)
        mockDefaultHook.assert_called_once_with(args)

    def test_handles_missing_thread_info(self):
        """args.thread can be None; the hook must not crash formatting the log message."""
        exc = websockets.exceptions.ConnectionClosedError(None, None)
        args = SimpleNamespace(exc_type=type(exc), exc_value=exc, exc_traceback=None, thread=None)
        with patch("Database.Listeners.spotifyListener._PREVIOUS_EXCEPTHOOK") as mockDefaultHook, \
             self.assertLogs("Database.Listeners.spotifyListener", level="WARNING"):
            _shutdown_exception_hook(args)
        mockDefaultHook.assert_not_called()


class TestHookForwardsToThreadingsHandler(unittest.TestCase):
    """Importing this module replaces threading.excepthook process-wide, for
    every thread - so what it does with an exception it does NOT recognize is
    what decides whether unrelated threads' bugs stay readable.

    It used to hand those to sys.__excepthook__, which prints a bare traceback:
    the "Exception in thread <name>:" header that threading's own handler
    writes was lost, so a crash in any worker read like a main-thread crash.
    (And a thread's SystemExit, which threading deliberately ignores, printed a
    traceback.) Forwarding to whatever hook was installed before this module
    loaded restores both, and leaves a host that installed its own hook - the
    embedding app, pytest's thread-exception plugin - still holding it."""

    def test_forwards_to_a_real_handler_not_our_own_hook(self):
        self.assertIsNot(listenerModule._PREVIOUS_EXCEPTHOOK, _shutdown_exception_hook,
                         "the hook would forward to itself - an infinite loop")

    def test_forwarding_keeps_the_thread_name_threadings_handler_prints(self):
        """Against threading's own default, standing in for whatever the host
        installed. Also pins the calling convention: threading.excepthook takes
        one args object where sys.__excepthook__ takes three positional
        arguments, so forwarding to a handler of the wrong shape would raise
        inside the handler - the one place nothing can report it."""
        exc = TypeError("unexpected")
        #< the real structseq threading passes: _thread.excepthook rejects
        #  anything else ("argument type must be ExceptHookArgs")
        args = threading.ExceptHookArgs(
            [type(exc), exc, None, threading.Thread(name="attribution-probe")])

        stderr = io.StringIO()
        with patch.object(listenerModule, "_PREVIOUS_EXCEPTHOOK", threading.__excepthook__), \
             contextlib.redirect_stderr(stderr):
            _shutdown_exception_hook(args)

        self.assertIn("attribution-probe", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
