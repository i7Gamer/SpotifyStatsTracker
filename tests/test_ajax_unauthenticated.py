"""An expired session used to dead-end the AJAX shell pages: the route answered
a 302 to /login, fetch() followed it transparently, and the loader tried to
parse the login page's HTML as JSON - so the user saw a generic "couldn't load"
with a Retry that failed identically forever, and never learned they were
logged out.

The routes now answer an ajax request with a 401 (the client turns it into a
real navigation - see AjaxStatus.redirectIfUnauthorized); non-ajax requests keep
redirecting as before.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase

# The pages whose content is loaded/refreshed by a fetch in static/js.
AJAX_PATHS = ("/", "/charts", "/genres", "/history", "/top-songs", "/top-artists", "/top-albums")


class UnauthenticatedAjaxTestCase(AppTestCase):
    def setUp(self):
        self.dash = self._makeApp()
        self.client = self.dash.app.test_client()
        # No session at all: every request below is unauthenticated.
        self.patcher = patch.object(self.dash, "is_user_logged_in", return_value=False)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)


class TestAjaxRequestsGet401(UnauthenticatedAjaxTestCase):
    def test_every_ajax_page_answers_401_json(self):
        for path in AJAX_PATHS:
            with self.subTest(path=path):
                resp = self.client.get(f"{path}?ajax=true")

                self.assertEqual(resp.status_code, 401)
                self.assertEqual(resp.mimetype, "application/json")

    def test_the_401_carries_a_login_url_pointing_back(self):
        resp = self.client.get("/charts?ajax=true&interval=week")

        payload = resp.get_json()
        self.assertIn("/login", payload["loginUrl"])
        self.assertIn("charts", payload["loginUrl"])

    def test_a_non_true_ajax_value_still_counts_as_ajax(self):
        """detail-history uses ?ajax=list, not ?ajax=true."""
        resp = self.client.get("/song/t1?ajax=list")

        self.assertEqual(resp.status_code, 401)


class TestNormalRequestsStillRedirect(UnauthenticatedAjaxTestCase):
    def test_page_loads_keep_redirecting_to_login(self):
        for path in AJAX_PATHS:
            with self.subTest(path=path):
                resp = self.client.get(path)

                self.assertEqual(resp.status_code, 302)
                self.assertIn("/login", resp.headers["Location"])

    def test_the_redirect_preserves_where_the_user_was_going(self):
        resp = self.client.get("/charts")

        self.assertIn("next=", resp.headers["Location"])
        self.assertIn("charts", resp.headers["Location"])


if __name__ == "__main__":
    unittest.main()
