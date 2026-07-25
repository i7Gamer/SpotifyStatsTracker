"""After logging in, the user should land back on the page that redirected
them to /login (?next=...) instead of always on the dashboard.
"""
import unittest
from unittest.mock import patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp
from _app_factory import AppTestCase

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class TestLoginNextRedirect(AppTestCase):
    def _postLogin(self, dash, next_url, email="alice@example.com", cookies="sp_dc=abc"):
        client = dash.app.test_client()
        data = {"email": email, "cookies": cookies}
        if next_url is not None:
            data["next"] = next_url
        resp = client.post("/login", data=data)
        return resp, client

    def test_successful_login_redirects_to_next(self):
        dash = self._makeApp()
        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch.object(dash, 'get_or_create_user', return_value='alice'), \
             patch.object(dash, 'get_user_db'), \
             patch.object(dash.repo, 'setUserCookies'):
            resp, client = self._postLogin(dash, next_url="/top-songs")

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/top-songs"))

    def test_successful_login_without_next_redirects_to_dashboard(self):
        dash = self._makeApp()
        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch.object(dash, 'get_or_create_user', return_value='alice'), \
             patch.object(dash, 'get_user_db'), \
             patch.object(dash.repo, 'setUserCookies'):
            resp, client = self._postLogin(dash, next_url=None)

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/"))

    def test_absolute_url_next_is_rejected_as_an_open_redirect(self):
        """A `next` value pointing off-site must never be honored - it would
        otherwise send a freshly authenticated session to an attacker's site."""
        dash = self._makeApp()
        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch.object(dash, 'get_or_create_user', return_value='alice'), \
             patch.object(dash, 'get_user_db'), \
             patch.object(dash.repo, 'setUserCookies'):
            resp, client = self._postLogin(dash, next_url="https://evil.example.com/steal")

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.example.com", resp.headers["Location"])
        self.assertTrue(resp.headers["Location"].endswith("/"))

    def test_protocol_relative_next_is_rejected_as_an_open_redirect(self):
        dash = self._makeApp()
        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch.object(dash, 'get_or_create_user', return_value='alice'), \
             patch.object(dash, 'get_user_db'), \
             patch.object(dash.repo, 'setUserCookies'):
            resp, client = self._postLogin(dash, next_url="//evil.example.com/steal")

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.example.com", resp.headers["Location"])

    def test_bare_slash_next_is_still_a_valid_same_origin_redirect(self):
        """A single "/" is a legitimate same-origin target and must not be
        swept up by the "/\\" / "//" open-redirect guard."""
        dash = self._makeApp()
        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch.object(dash, 'get_or_create_user', return_value='alice'), \
             patch.object(dash, 'get_user_db'), \
             patch.object(dash.repo, 'setUserCookies'):
            resp, client = self._postLogin(dash, next_url="/")

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/"))

    def test_backslash_next_is_rejected_as_an_open_redirect(self):
        """Browsers normalize a leading "/\\" to "//" in a Location header,
        turning it into a protocol-relative URL - "/\\evil.example.com" must be
        rejected exactly like "//evil.example.com"."""
        dash = self._makeApp()
        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch.object(dash, 'get_or_create_user', return_value='alice'), \
             patch.object(dash, 'get_user_db'), \
             patch.object(dash.repo, 'setUserCookies'):
            resp, client = self._postLogin(dash, next_url="/\\evil.example.com/steal")

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.example.com", resp.headers["Location"])

    def test_tab_in_next_is_rejected_as_an_open_redirect(self):
        """The WHATWG URL parser removes every ASCII tab/newline from a URL
        before resolving it, so "/\\t/evil.example.com" arrives at the browser
        as "//evil.example.com" - protocol-relative, exactly what the "//"
        guard exists to stop. Werkzeug only refuses CR/LF in a header value, so
        a tab is the one control character that survives all the way out."""
        dash = self._makeApp()
        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch.object(dash, 'get_or_create_user', return_value='alice'), \
             patch.object(dash, 'get_user_db'), \
             patch.object(dash.repo, 'setUserCookies'):
            resp, client = self._postLogin(dash, next_url="/\t/evil.example.com/steal")

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.example.com", resp.headers["Location"])

    def test_control_characters_in_next_are_rejected(self):
        """Anything in the C0/DEL range is meaningless in a legitimate `next`
        and only ever shows up in normalization-bypass attempts, so the guard
        rejects the whole class rather than blacklisting tab alone."""
        dash = self._makeApp()
        for raw in ("/\x00/evil.example.com", "/\x1f/evil.example.com",
                    "/\x7f/evil.example.com", "/ok\tpath"):
            with self.subTest(raw=raw):
                with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
                     patch.object(dash, 'get_or_create_user', return_value='alice'), \
                     patch.object(dash, 'get_user_db'), \
                     patch.object(dash.repo, 'setUserCookies'):
                    resp, client = self._postLogin(dash, next_url=raw)

                self.assertEqual(resp.status_code, 302)
                self.assertTrue(resp.headers["Location"].endswith("/"))

    def test_ordinary_next_paths_still_survive_the_control_char_guard(self):
        """The control-character guard must not sweep up the query strings and
        encoded values real `next` targets carry."""
        dash = self._makeApp()
        for raw in ("/charts?interval=week", "/top-songs", "/artist/abc%20def"):
            with self.subTest(raw=raw):
                with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
                     patch.object(dash, 'get_or_create_user', return_value='alice'), \
                     patch.object(dash, 'get_user_db'), \
                     patch.object(dash.repo, 'setUserCookies'):
                    resp, client = self._postLogin(dash, next_url=raw)

                self.assertEqual(resp.status_code, 302)
                self.assertTrue(resp.headers["Location"].endswith(raw))

    def test_next_survives_a_failed_submission(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        resp = client.post("/login", data={"email": "", "cookies": "", "next": "/top-albums"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'name="next" value="/top-albums"', resp.data)

    def test_missing_email_or_cookies_shows_error_instead_of_crashing(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        resp = client.post("/login", data={"email": "alice@example.com", "cookies": ""})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"required", resp.data)

    def test_login_page_shows_both_email_and_cookies_fields_at_once(self):
        """Login is a single page/form now - both fields are always present,
        not split across a two-step email-then-cookies flow."""
        dash = self._makeApp()
        client = dash.app.test_client()
        resp = client.get("/login")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'name="email"', resp.data)
        self.assertIn(b'name="cookies"', resp.data)

    def test_skip_email_verification_env_var_bypasses_the_check(self):
        """SKIP_EMAIL_VERIFICATION lets a self-hoster turn off the
        cookies-belong-to-this-email check entirely, e.g. if Spotify starts
        blocking the verification request for their account."""
        with patch.dict(os.environ, {"SKIP_EMAIL_VERIFICATION": "1"}):
            dash = self._makeApp()
        with patch.object(dash, '_verifyCookiesMatchEmail') as mock_verify, \
             patch.object(dash, 'get_or_create_user', return_value='alice'), \
             patch.object(dash, 'get_user_db'), \
             patch.object(dash.repo, 'setUserCookies'):
            resp, client = self._postLogin(dash, next_url=None)

        mock_verify.assert_not_called()
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/"))

    def test_email_verification_runs_by_default(self):
        dash = self._makeApp()
        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=False) as mock_verify, \
             patch.object(dash, 'get_or_create_user', return_value='alice'), \
             patch.object(dash, 'get_user_db'), \
             patch.object(dash.repo, 'setUserCookies'):
            resp, client = self._postLogin(dash, next_url=None)

        mock_verify.assert_called_once()
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Couldn&#39;t verify", resp.data)


if __name__ == "__main__":
    unittest.main()
