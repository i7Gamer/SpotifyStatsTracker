"""Three upstream-error log lines in Database/Listeners/spotifyListener.py used
to carry resp.text whole: the access-token refresh's failure line (capped by
8f7a0b7), and two siblings in the Web API readers used by the backfill/
reconciliation path - _fetch_recently_played_from_web_api's scope-error arm
(:582) and its generic-failure arm (:601), and _get_current_user_from_web_api's
generic-failure arm (:636). Spotify's own answer is a short JSON object; a
gateway in front of it answers with an HTML page, and a bad hour of those is
what the cap is for."""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.Listeners.spotifyListener import (
    _refresh_spotify_access_token,
    _fetch_recently_played_from_web_api,
    _get_current_user_from_web_api,
    _SCOPE_ERROR,
)
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


class TestWebApiFailureLogsAreCapped(unittest.TestCase):
    """_fetch_recently_played_from_web_api and _get_current_user_from_web_api
    both call requests.get (not requests.post, imported inside each function
    body), and each has its own log site to cap: :582 and :601 in the first,
    :636 in the second."""

    def test_the_scope_error_body_is_capped_and_the_sentinel_still_returns(self):
        """:582 - the 403 arm only fires when "insufficient" is in the body
        (Spotify's actual scope-rejection wording), so the control text must
        contain it. A huge body must not stop the _SCOPE_ERROR sentinel from
        coming back - that's what tells the caller to prompt for
        re-authorization, so a generic non-2xx failure could not stand in for
        this test."""
        body = '{"error":{"message":"Insufficient client scope"}}' + "x" * 10000
        response = MagicMock(status_code=403, text=body)

        with patch("requests.get", return_value=response), \
                self.assertLogs(LISTENER_LOGGER, level="ERROR") as logs:
            result = _fetch_recently_played_from_web_api("token", logUser="alice")

        self.assertIs(result, _SCOPE_ERROR)
        line = next(l for l in logs.output if "lacks" in l)
        self.assertIn(f"[{len(body)} chars total]", line)
        #< this message's own fixed wording is longer than the other two
        #  sites', hence the wider margin than LOG_BODY_MAX_CHARS + 200 below
        self.assertLess(len(line), LOG_BODY_MAX_CHARS + 300)

    def test_a_recently_played_gateway_page_is_capped(self):
        """:601 - the generic non-2xx arm (a 502 rather than the 403/429
        arms above it)."""
        body = "<html>gateway error</html>" * 400
        response = MagicMock(status_code=GATEWAY_STATUS, text=body)

        with patch("requests.get", return_value=response), \
                self.assertLogs(LISTENER_LOGGER, level="ERROR") as logs:
            result = _fetch_recently_played_from_web_api("token", logUser="alice")

        self.assertIsNone(result)
        line = next(l for l in logs.output if "Failed to fetch recently played" in l)
        self.assertIn(f"[{len(body)} chars total]", line)
        self.assertLess(len(line), LOG_BODY_MAX_CHARS + 200)

    def test_a_current_user_gateway_page_is_capped(self):
        """:636 - _get_current_user_from_web_api's own generic non-2xx arm."""
        body = "<html>gateway error</html>" * 400
        response = MagicMock(status_code=GATEWAY_STATUS, text=body)

        with patch("requests.get", return_value=response), \
                self.assertLogs(LISTENER_LOGGER, level="ERROR") as logs:
            result = _get_current_user_from_web_api("token", logUser="alice")

        self.assertIsNone(result)
        line = next(l for l in logs.output if "Failed to fetch current user" in l)
        self.assertIn(f"[{len(body)} chars total]", line)
        self.assertLess(len(line), LOG_BODY_MAX_CHARS + 200)

    def test_a_short_body_is_still_logged_whole(self):
        """Control: an ordinary short error body is well under the cap and
        must not be truncated - the fix only bounds the worst case."""
        body = '{"error": {"status": 400, "message": "Bad request"}}'
        response = MagicMock(status_code=400, text=body)

        with patch("requests.get", return_value=response), \
                self.assertLogs(LISTENER_LOGGER, level="ERROR") as logs:
            _fetch_recently_played_from_web_api("token", logUser="alice")

        self.assertTrue(any(body in l for l in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
