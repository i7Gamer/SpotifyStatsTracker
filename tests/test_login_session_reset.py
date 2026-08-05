"""The login page drops htmx's page-snapshot cache.

htmx keeps a snapshot of every history-managed page - the innerHTML of the
history element, which is document.body here - in sessionStorage, so a Back
navigation is restored without a request. Logging out is a server-side act and
sessionStorage is not, so nothing on the server can clear it.

That leaves a shared browser one Back keypress from the previous account's
dashboard: painted straight from storage, with no request made and therefore no
session checked. The snapshot is frozen rather than live - every htmx request
made from it now redirects to login - but it is still the last user's listening
history on screen after they logged out.

static/js/session-reset.js clears it, and the logic is unit-tested in plain node
(tests/test_session_reset.js). What is asserted here is the half node cannot
see: that the login page actually SERVES the file. A cleanup script nothing
loads is the same as no cleanup script, and it would keep passing its own tests
forever - which is the failure mode tests/test_inline_handler_targets.py and
tests/test_data_attribute_knobs.py exist for elsewhere.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_RELATIVE_PATH = "js/session-reset.js"


class TestLoginPageClearsTheHtmxSnapshotCache(AppTestCase):
    def setUp(self):
        self.dash = self._makeApp()
        self.client = self.dash.app.test_client()

    def test_the_login_page_loads_the_reset_script(self):
        body = self.client.get("/login").get_data(as_text=True)

        self.assertIn(SCRIPT_RELATIVE_PATH, body)

    def test_the_script_it_names_exists(self):
        """url_for would not have failed on a missing file, so the tag above can
        point at a 404 and the page still renders exactly the same."""
        self.assertTrue((REPO_ROOT / "static" / SCRIPT_RELATIVE_PATH).is_file())

    def test_the_script_is_served(self):
        """The end of the chain: the tag resolves to a real 200 response whose
        body is the cleanup, not an error page."""
        body = self.client.get("/login").get_data(as_text=True)
        start = body.index(SCRIPT_RELATIVE_PATH)
        url = body[body.rindex('"', 0, start) + 1:body.index('"', start)]

        resp = self.client.get(url)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("htmx-history-cache", resp.get_data(as_text=True))

    def test_a_logged_out_landing_is_where_this_runs(self):
        """Logout redirects here, which is what makes the login page the right
        place for it - the session has just ended and the next one has not
        started."""
        with self.client.session_transaction() as sess:
            sess["email"] = "someone@example.com"

        resp = self.client.post("/logout")

        self.assertIn("/login", resp.headers.get("Location", ""))


if __name__ == "__main__":
    unittest.main()
