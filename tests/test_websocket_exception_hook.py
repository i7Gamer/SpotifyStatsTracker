"""Tests for _shutdown_exception_hook, the threading.excepthook that logs
expected websocket-close exceptions from background threads (e.g. spotapi's
keep_alive ping) as one clean line instead of letting Python print a raw
traceback. Exercises the real hook, not a re-implementation of its logic."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import websockets.exceptions
import websockets.frames

from Database.Listeners.spotifyListener import _shutdown_exception_hook


def _makeArgs(exc, threadName="worker-thread"):
    return SimpleNamespace(exc_type=type(exc), exc_value=exc, exc_traceback=None,
                            thread=SimpleNamespace(name=threadName))


class TestWebsocketExceptionHook(unittest.TestCase):
    """Verify harmless/expected websocket-close exceptions are logged instead
    of dumped as a raw traceback, while anything else still is."""

    def _invoke(self, exc):
        with patch("Database.Listeners.spotifyListener.sys.__excepthook__") as mockDefaultHook, \
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
        surface loudly via the default exception handler."""
        exc = TypeError("unexpected")
        with patch("Database.Listeners.spotifyListener.sys.__excepthook__") as mockDefaultHook:
            _shutdown_exception_hook(_makeArgs(exc))
        mockDefaultHook.assert_called_once_with(TypeError, exc, None)

    def test_handles_missing_thread_info(self):
        """args.thread can be None; the hook must not crash formatting the log message."""
        exc = websockets.exceptions.ConnectionClosedError(None, None)
        args = SimpleNamespace(exc_type=type(exc), exc_value=exc, exc_traceback=None, thread=None)
        with patch("Database.Listeners.spotifyListener.sys.__excepthook__") as mockDefaultHook, \
             self.assertLogs("Database.Listeners.spotifyListener", level="WARNING"):
            _shutdown_exception_hook(args)
        mockDefaultHook.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
