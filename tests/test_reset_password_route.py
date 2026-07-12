"""/reset-password: prove ownership with valid, matching Spotify cookies (no
old password required) to set a new password - see app.py's resetPassword()
route.
"""
import unittest
from unittest.mock import patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from werkzeug.security import generate_password_hash, check_password_hash

from app import SpotifyDashboardApp

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'

NEW_PASSWORD = "New-Correct-Horse1"


class TestResetPasswordRoute(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _postReset(self, dash, email="alice@example.com", password=NEW_PASSWORD,
                   confirm=NEW_PASSWORD, cookies="sp_dc=fresh"):
        client = dash.app.test_client()
        data = {"email": email, "password": password, "confirm_password": confirm, "cookies": cookies}
        resp = client.post("/reset-password", data=data)
        return resp, client

    def test_reset_updates_password_and_cookies_then_logs_in(self):
        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")
        dash.repo.setUserCookies("alice", {"sp_dc": "old"})
        dash.repo.setUserPassword("alice", generate_password_hash("Old-Password1"))

        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch.object(dash, 'get_user_db'):
            resp, client = self._postReset(dash)

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/"))

        storedHash = dash.repo.getUserPasswordHash("alice")
        self.assertTrue(check_password_hash(storedHash, NEW_PASSWORD))
        self.assertEqual(dash.repo.getUserCookies("alice"), {"sp_dc": "fresh"})

    def test_unknown_email_is_rejected(self):
        dash = self._makeApp()
        resp, client = self._postReset(dash, email="nobody@example.com")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"No account found", resp.data)

    def test_missing_fields_shows_error(self):
        dash = self._makeApp()
        resp, client = self._postReset(dash, cookies="")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"required", resp.data)

    def test_password_confirmation_mismatch_is_rejected(self):
        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")

        resp, client = self._postReset(dash, confirm="Different-Horse1")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"do not match", resp.data)

    def test_weak_password_is_rejected(self):
        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")

        resp, client = self._postReset(dash, password="alllowercase1", confirm="alllowercase1")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"uppercase letter", resp.data)

    def test_cookies_not_matching_email_is_rejected(self):
        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")
        dash.repo.setUserPassword("alice", generate_password_hash("Old-Password1"))

        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=False) as mock_verify:
            resp, client = self._postReset(dash)

        mock_verify.assert_called_once()
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Couldn&#39;t verify", resp.data)

        # Old password must still be intact - a failed verification must not
        # overwrite anything.
        self.assertTrue(check_password_hash(dash.repo.getUserPasswordHash("alice"), "Old-Password1"))

    def test_reset_works_for_a_legacy_account_with_no_password_yet(self):
        """Reset-password can also be how a legacy (cookies-only) account
        gets its first password, same as /register."""
        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")
        dash.repo.setUserCookies("alice", {"sp_dc": "old"})

        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch.object(dash, 'get_user_db'):
            resp, client = self._postReset(dash)

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(check_password_hash(dash.repo.getUserPasswordHash("alice"), NEW_PASSWORD))


if __name__ == "__main__":
    unittest.main()
