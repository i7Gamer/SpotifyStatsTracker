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
from config import SPOTIFY_OAUTH_STATE_SESSION_KEY
from _app_factory import AppTestCase

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'

VALID_PASSWORD = "Correct-Horse1"


class TestRegisterRoute(AppTestCase):
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

    def test_register_drops_the_previous_users_leftovers(self):
        """Registering logs the new account in, so it is a user switch like any
        other - see TestLoginStartsACleanSession in tests/test_login_password.py
        for what crosses over and why the clear() has to precede `permanent`."""
        dash = self._makeApp()
        client = dash.app.test_client()
        with client.session_transaction() as sess:
            sess[SPOTIFY_OAUTH_STATE_SESSION_KEY] = "abandoned-by-the-previous-user"

        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch.object(dash, 'get_user_db'):
            resp = client.post("/register", data={
                "email": "alice@example.com", "password": VALID_PASSWORD,
                "confirm_password": VALID_PASSWORD, "cookies": "sp_dc=abc"})

        self.assertEqual(resp.status_code, 302)
        with client.session_transaction() as sess:
            self.assertNotIn(SPOTIFY_OAUTH_STATE_SESSION_KEY, sess)
            self.assertEqual(sess["email"], "alice@example.com")
            self.assertTrue(sess.permanent, "clear() must not wipe _permanent")

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


class TestCaseInsensitiveEmailAcrossAuthFlows(AppTestCase):
    """A registered email must be recognized by every auth path regardless of
    the case it's typed in later - getUsernameForEmail now folds case on the
    stored side (COLLATE NOCASE). Before this fix, registering as
    "Alice@example.com" and later logging in (password or cookies) as
    "alice@example.com" read as an unknown email: password login failed,
    and cookie login/refresh minted a second users row (a second listener on
    the same Spotify account, no way to reach the original account's data)."""

    def _register(self, dash, email, password=VALID_PASSWORD):
        client = dash.app.test_client()
        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch.object(dash, 'get_user_db'):
            resp = client.post("/register", data={
                "email": email, "password": password, "confirm_password": password,
                "cookies": "sp_dc=abc"})
        return resp, client

    def test_password_login_with_different_case_resolves_the_same_account(self):
        dash = self._makeApp()
        self._register(dash, "Alice@example.com")
        originalUsername = dash.repo.getUsernameForEmail("Alice@example.com")

        with patch.object(dash, 'get_user_db'):
            resp = dash.app.test_client().post("/login", data={
                "email": "alice@example.com", "password": VALID_PASSWORD})

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/"))
        # No sibling account was minted for the lowercase spelling.
        self.assertEqual(dash.repo.getUsernameForEmail("alice@example.com"), originalUsername)

    def test_cookie_login_with_different_case_does_not_create_a_second_account(self):
        dash = self._makeApp()
        self._register(dash, "Alice@example.com")
        originalUsername = dash.repo.getUsernameForEmail("Alice@example.com")

        # get_or_create_user is what /login's cookies branch calls to resolve
        # (or mint) the account for the submitted email - exercised directly
        # here the same way tests/test_multi_user.py's
        # test_get_or_create_user_still_suffixes_on_a_real_email_collision
        # does, rather than re-mocking cookie ownership verification.
        resolvedUsername = dash.get_or_create_user("alice@example.com")

        self.assertEqual(resolvedUsername, originalUsername)
        # The old bug's tell: a second, suffixed row for the same account.
        self.assertFalse(dash.repo.usernameExists(f"{originalUsername}_1"))

    def test_registering_the_same_email_in_a_different_case_is_refused(self):
        dash = self._makeApp()
        self._register(dash, "Alice@example.com")

        resp, _ = self._register(dash, "alice@example.com")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"already exists", resp.data)


if __name__ == "__main__":
    unittest.main()
