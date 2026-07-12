"""Password login: an alternate to pasting cookies every time, valid only for
as long as the account's last-saved cookies are still live (see app.py's
/login password branch).
"""
import unittest
from unittest.mock import patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from werkzeug.security import generate_password_hash

from app import SpotifyDashboardApp

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class TestLoginPassword(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _registerAccount(self, dash, email="alice@example.com", username="alice",
                          password="Correct-Horse1", cookies=True):
        dash.repo.upsertUser(username, email)
        if cookies:
            dash.repo.setUserCookies(username, {"sp_dc": "abc"})
        if password is not None:
            dash.repo.setUserPassword(username, generate_password_hash(password))
        return username

    def _postPasswordLogin(self, dash, email, password):
        client = dash.app.test_client()
        resp = client.post("/login", data={"email": email, "password": password})
        return resp, client

    def test_password_login_happy_path_redirects_to_dashboard(self):
        dash = self._makeApp()
        self._registerAccount(dash)

        with patch.object(dash, 'get_user_db'):
            resp, client = self._postPasswordLogin(dash, "alice@example.com", "Correct-Horse1")

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/"))

    def test_wrong_password_is_rejected(self):
        dash = self._makeApp()
        self._registerAccount(dash)

        with patch.object(dash, 'get_user_db'):
            resp, client = self._postPasswordLogin(dash, "alice@example.com", "totally-wrong-1A")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Invalid email or password", resp.data)

    def test_unknown_email_is_rejected_with_generic_message(self):
        dash = self._makeApp()

        with patch.object(dash, 'get_user_db'):
            resp, client = self._postPasswordLogin(dash, "nobody@example.com", "whatever-1A")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Invalid email or password", resp.data)

    def test_legacy_account_without_password_gets_specific_message(self):
        """An account created before this feature (cookies-only, no password
        set yet) shouldn't get a generic invalid-credentials message - it
        should be pointed at /register to add a password."""
        dash = self._makeApp()
        self._registerAccount(dash, password=None)

        with patch.object(dash, 'get_user_db'):
            resp, client = self._postPasswordLogin(dash, "alice@example.com", "whatever-1A")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"doesn&#39;t have a password yet", resp.data)

    def test_expired_cookies_block_password_login(self):
        """Password login is only valid as long as the stored cookies are
        still live - if they've expired, correct password isn't enough."""
        dash = self._makeApp()
        self._registerAccount(dash)

        with patch.object(dash, 'get_user_db'), \
             patch.object(dash, 'is_user_logged_in', return_value=False):
            resp, client = self._postPasswordLogin(dash, "alice@example.com", "Correct-Horse1")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"session has expired", resp.data)

    def test_missing_password_shows_error_instead_of_crashing(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        resp = client.post("/login", data={"email": "alice@example.com", "password": ""})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"required", resp.data)

    def test_cookie_login_branch_is_unaffected_by_password_branch(self):
        """Submitting the (fallback) cookies form - no password field at all -
        must still take the original cookie-verification path."""
        dash = self._makeApp()
        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch.object(dash, 'get_or_create_user', return_value='alice'), \
             patch.object(dash, 'get_user_db'), \
             patch.object(dash.repo, 'setUserCookies'):
            client = dash.app.test_client()
            resp = client.post("/login", data={"email": "alice@example.com", "cookies": "sp_dc=abc"})

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/"))


if __name__ == "__main__":
    unittest.main()
