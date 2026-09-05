"""The access-token refresh's failure line (Database/Listeners/spotifyListener.py)
is the highest-volume error line the listener can emit - once per poll while
accounts.spotify.com is unhappy - and it used to carry resp.text whole. Spotify's
own answer is a short JSON object; a gateway in front of it answers with an
HTML page, and a bad hour of those is what the cap is for."""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.Listeners.spotifyListener import _refresh_spotify_access_token
from Database.utils import LOG_BODY_MAX_CHARS

LISTENER_LOGGER = "Database.Listeners.spotifyListener"
GATEWAY_STATUS = 502


class TestRefreshFailureLogIsCapped(unittest.TestCase):

    def test_a_gateway_page_is_cut_and_the_refresh_reports_no_token(self):
        body = "<html>gateway error</html>" * 400
        response = MagicMock(status_code=GATEWAY_STATUS, text=body)

        with patch("requests.post", return_value=response), \
                self.assertLogs(LISTENER_LOGGER, level="ERROR") as logs:
            token = _refresh_spotify_access_token("id", "secret", "refresh", logUser="alice")

        self.assertIsNone(token)
        line = next(l for l in logs.output if "Failed to refresh Spotify access token" in l)
        self.assertIn("alice", line)
        self.assertIn(f"[{len(body)} chars total]", line)
        self.assertLess(len(line), LOG_BODY_MAX_CHARS + 200)   #< the cap plus the line's own words

    def test_a_short_spotify_error_is_logged_whole(self):
        body = '{"error": "invalid_grant", "error_description": "Refresh token revoked"}'
        response = MagicMock(status_code=400, text=body)

        with patch("requests.post", return_value=response), \
                self.assertLogs(LISTENER_LOGGER, level="ERROR") as logs:
            _refresh_spotify_access_token("id", "secret", "refresh", logUser="alice")

        self.assertTrue(any(body in l for l in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
