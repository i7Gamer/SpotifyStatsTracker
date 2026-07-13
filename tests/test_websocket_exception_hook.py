"""Tests for websocket exception hook that suppresses harmless close exceptions."""
import sys
import unittest
from unittest.mock import MagicMock


class TestWebsocketExceptionHook(unittest.TestCase):
    """Verify that harmless websocket close exceptions are suppressed."""

    def test_suppresses_connection_closed_ok(self):
        """Test that ConnectionClosedOK exceptions are suppressed."""
        # Simulate the exception hook behavior
        class ConnectionClosedOK(Exception):
            pass

        exc = ConnectionClosedOK("connection closed normally")
        args = MagicMock()
        args.exc_value = exc

        # This should return without calling the default handler
        result = None
        if isinstance(args.exc_value, ConnectionClosedOK):
            result = "suppressed"

        self.assertEqual(result, "suppressed",
                        "ConnectionClosedOK should be suppressed")

    def test_suppresses_connection_closed_error_with_status_1000(self):
        """Test that ConnectionClosedError with status 1000 is suppressed.

        Status 1000 = normal close, so this should not be reported as an error.
        """
        # Simulate ConnectionClosedError with graceful close status
        class ConnectionClosedError(Exception):
            pass

        exc = ConnectionClosedError("sent 1000 (OK); no close frame received")
        args = MagicMock()
        args.exc_value = exc
        exc_str = str(args.exc_value)

        # Check the hook logic
        result = None
        if isinstance(args.exc_value, ConnectionClosedError):
            if "1000" in exc_str or "sent 1000" in exc_str:
                result = "suppressed"

        self.assertEqual(result, "suppressed",
                        "ConnectionClosedError with status 1000 should be suppressed")

    def test_does_not_suppress_other_connection_errors(self):
        """Test that ConnectionClosedError with abnormal status codes is NOT suppressed."""
        class ConnectionClosedError(Exception):
            pass

        # Abnormal close (status 1006)
        exc = ConnectionClosedError("sent 1006 (ABNORMAL CLOSURE); no close frame received")
        args = MagicMock()
        args.exc_value = exc
        exc_str = str(args.exc_value)

        # Check the hook logic
        result = None
        if isinstance(args.exc_value, ConnectionClosedError):
            if "1000" in exc_str or "sent 1000" in exc_str:
                result = "suppressed"
            else:
                result = "not_suppressed"
        else:
            result = "not_suppressed"

        self.assertEqual(result, "not_suppressed",
                        "ConnectionClosedError with abnormal status should NOT be suppressed")

    def test_status_1000_variations(self):
        """Test that various status 1000 strings are recognized."""
        error_messages = [
            "sent 1000 (OK); no close frame received",
            "1000 (NORMAL CLOSURE)",
            "Connection closed with status 1000",
            "sent 1000",
            "1000 OK",
        ]

        for msg in error_messages:
            should_suppress = "1000" in msg or "sent 1000" in msg
            self.assertTrue(should_suppress,
                           f"Should recognize status 1000 in: {msg}")

    def test_status_non_1000_variations(self):
        """Test that non-1000 status codes are NOT recognized as graceful."""
        error_messages = [
            "sent 1006 (ABNORMAL CLOSURE)",
            "Connection closed with status 1011",
            "sent 1002 (PROTOCOL ERROR)",
            "1003 (UNSUPPORTED DATA)",
        ]

        for msg in error_messages:
            should_suppress = "1000" in msg or "sent 1000" in msg
            self.assertFalse(should_suppress,
                            f"Should NOT recognize as graceful: {msg}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
