# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""routes/auth.py's login() tells the password and cookie forms apart by
field presence, but never told login.html WHICH form failed - every
error= render looked identical to the template, so the error paragraph
always rendered above the (collapsed) password form while the cookie
<details> stayed closed, hiding the very form whose submission produced the
error along with the text describing it.

login() now passes cookieError=True on the cookie branch's two failure
returns; login.html opens the <details> and moves the error paragraph inside
it when that flag is set, leaving the password branch's three failure
returns untouched.

2026-09-02 review, UT-21."""
import os
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from tests._app_factory import AppTestCase


class CookieLoginErrorPlacementTestCase(AppTestCase):
    def test_empty_cookies_submission_opens_the_details_with_the_error_inside(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        resp = client.post("/login", data={"email": "alice@example.com", "cookies": ""})

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertRegex(body, r'<details class="cookie-login-fallback"[^>]*\bopen\b')
        detailsMatch = re.search(
            r'<details class="cookie-login-fallback"[^>]*>(.*)</details>', body, re.S)
        self.assertIsNotNone(detailsMatch)
        self.assertIn("Email and cookies are both required.", detailsMatch.group(1))
        #< rendered exactly once, and that one time is inside the <details> -
        #  not ALSO above the password form the error has nothing to do with
        self.assertEqual(body.count("Email and cookies are both required."), 1)
        self.assertGreater(body.index("Email and cookies are both required."), body.index("<details"))

    def test_unverifiable_cookies_submission_also_opens_the_details(self):
        """`_verifyCookiesMatchEmail` is patched rather than left to make a
        real request to Spotify, per this project's test-network rule."""
        dash = self._makeApp()
        client = dash.app.test_client()

        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=False):
            resp = client.post("/login", data={"email": "alice@example.com",
                                               "cookies": "sp_dc=not-a-real-cookie"})

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertRegex(body, r'<details class="cookie-login-fallback"[^>]*\bopen\b')

    def test_password_branch_failure_keeps_the_details_closed(self):
        """Negative control: the password branch's error returns are
        untouched - the details stays collapsed and the error stays at the
        top level, matching tests/test_login_password.py's assertions."""
        dash = self._makeApp()
        client = dash.app.test_client()

        resp = client.post("/login", data={"email": "alice@example.com", "password": ""})

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotRegex(body, r'<details class="cookie-login-fallback"[^>]*\bopen\b')
        self.assertRegex(body, r'<p class="error">.*required.*</p>\s*<form')

    def test_a_clean_get_also_keeps_the_details_closed(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        body = client.get("/login").get_data(as_text=True)

        self.assertNotRegex(body, r'<details class="cookie-login-fallback"[^>]*\bopen\b')


if __name__ == "__main__":
    unittest.main()
