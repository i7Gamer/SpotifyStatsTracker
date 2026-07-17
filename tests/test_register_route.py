"""/register: create a new password-login account, or add a password to an
existing cookies-only (legacy) account - see app.py's register() route.
"""
import unittest
from unittest.mock import patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from werkzeug.security import generate_password_hash, check_password_hash

from app import SpotifyDashboardApp

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'

VALID_PASSWORD = "Correct-Horse1"


class TestRegisterRoute(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _postRegister(self, dash, email="alice@example.com", password=VALID_PASSWORD,
                       confirm=VALID_PASSWORD, cookies="sp_dc=abc"):
        client = dash.app.test_client()
        data = {"email": email, "password": password, "confirm_password": confirm, "cookies": cookies}
        resp = client.post("/register", data=data)
        return resp, client

    def test_register_creates_new_account_and_logs_in(self):
        dash = self._makeApp()
        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch.object(dash, 'get_user_db'):
            resp, client = self._postRegister(dash)

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/"))

        username = dash.repo.getUsernameForEmail("alice@example.com")
        self.assertIsNotNone(username)
        storedHash = dash.repo.getUserPasswordHash(username)
        self.assertTrue(check_password_hash(storedHash, VALID_PASSWORD))
        self.assertEqual(dash.repo.getUserCookies(username), {"sp_dc": "abc"})

    def test_disabled_registration_404s_on_get_and_post(self):
        dash = self._makeApp()
        dash.repo.setRegistrationEnabled(False)

        getResp = dash.app.test_client().get("/register")
        postResp, _ = self._postRegister(dash)

        self.assertEqual(getResp.status_code, 404)
        self.assertEqual(postResp.status_code, 404)
        self.assertIsNone(dash.repo.getUsernameForEmail("alice@example.com"))

    def test_disabled_registration_hides_the_login_page_link(self):
        dash = self._makeApp()
        dash.repo.setRegistrationEnabled(False)

        resp = dash.app.test_client().get("/login")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"Create an account", resp.data)

    def test_missing_fields_shows_error(self):
        dash = self._makeApp()
        resp, client = self._postRegister(dash, cookies="")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"required", resp.data)

    def test_password_confirmation_mismatch_is_rejected(self):
        dash = self._makeApp()
        with patch.object(dash, '_verifyCookiesMatchEmail') as mock_verify:
            resp, client = self._postRegister(dash, confirm="Different-Horse1")

        mock_verify.assert_not_called()
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"do not match", resp.data)

    def test_weak_password_is_rejected(self):
        dash = self._makeApp()
        with patch.object(dash, '_verifyCookiesMatchEmail') as mock_verify:
            resp, client = self._postRegister(dash, password="alllowercase1", confirm="alllowercase1")

        mock_verify.assert_not_called()
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"uppercase letter", resp.data)

    def test_short_password_is_rejected(self):
        dash = self._makeApp()
        resp, client = self._postRegister(dash, password="Ab1", confirm="Ab1")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"at least 8 characters", resp.data)

    def test_password_without_digit_or_special_char_is_rejected(self):
        dash = self._makeApp()
        resp, client = self._postRegister(dash, password="OnlyLetters", confirm="OnlyLetters")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"number or special character", resp.data)

    def test_cookies_not_matching_email_is_rejected(self):
        dash = self._makeApp()
        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=False) as mock_verify:
            resp, client = self._postRegister(dash)

        mock_verify.assert_called_once()
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Couldn&#39;t verify", resp.data)

    def test_duplicate_email_with_existing_password_is_rejected(self):
        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")
        dash.repo.setUserPassword("alice", generate_password_hash("Some-Other-1"))

        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True):
            resp, client = self._postRegister(dash)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"already exists", resp.data)

    def test_registering_with_a_legacy_passwordless_account_claims_it(self):
        """An account that only ever logged in via cookies (no password_hash
        set) shouldn't be treated as a duplicate - registering with its email
        adds a password to it instead."""
        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")
        dash.repo.setUserCookies("alice", {"sp_dc": "old-cookie"})

        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch.object(dash, 'get_user_db'):
            resp, client = self._postRegister(dash, cookies="sp_dc=fresh-cookie")

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/"))

        storedHash = dash.repo.getUserPasswordHash("alice")
        self.assertTrue(check_password_hash(storedHash, VALID_PASSWORD))
        self.assertEqual(dash.repo.getUserCookies("alice"), {"sp_dc": "fresh-cookie"})
        # No sibling account was created for the same email.
        self.assertEqual(dash.repo.getUsernameForEmail("alice@example.com"), "alice")


if __name__ == "__main__":
    unittest.main()
