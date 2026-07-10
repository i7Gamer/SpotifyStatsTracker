import socket
import unittest

import pytest


class TestNetworkGuard(unittest.TestCase):
    """Meta-test: the autouse conftest fixture must turn any real network attempt
    into a loud failure instead of letting a missed mock silently hit Spotify."""

    def test_direct_socket_connect_is_blocked(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(RuntimeError, match="real network connection"):
                sock.connect(("93.184.216.34", 80))
        finally:
            sock.close()

    def test_requests_is_blocked(self):
        import requests
        with pytest.raises(Exception) as excinfo:
            requests.get("http://example.com", timeout=5)
        # requests wraps the RuntimeError in a ConnectionError; the guard message
        # must still be the root cause.
        self.assertIn("real network connection", str(excinfo.value))


if __name__ == "__main__":
    unittest.main()
