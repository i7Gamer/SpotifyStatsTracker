"""CSRF protection is wired app-wide (CSRFProtect in SpotifyDashboardApp), but
the suite disables it for every other test - app.py turns WTF_CSRF_ENABLED off
whenever PYTEST_CURRENT_TEST is set, so a regression in that wiring (an init
line dropped, a stray @csrf.exempt, a config typo) would leave every
state-changing endpoint forgeable in production while the whole suite still
passed.

These tests re-enable it and drive the real endpoints, so the protection itself
is pinned rather than assumed.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase


class CsrfEnforcementTestCase(AppTestCase):
    EMAIL = "alice@example.com"
    USERNAME = "alice"

    def setUp(self):
        self.dash = self._makeApp()
        self.dash.app.config["WTF_CSRF_ENABLED"] = True
        self.dash.repo.upsertUser(self.USERNAME, self.EMAIL)

    def _client(self, loggedIn=True):
        patcher = patch.object(self.dash, "is_user_logged_in", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        namePatcher = patch.object(self.dash, "get_username_for_email", return_value=self.USERNAME)
        namePatcher.start()
        self.addCleanup(namePatcher.stop)
        # A stand-in Database over the real repo: the routes under test only
        # need repo access, and constructing a real one would start workers and
        # attempt a Spotify login.
        userDb = MagicMock()
        userDb.repo = self.dash.repo
        dbPatcher = patch.object(self.dash, "get_user_db", return_value=userDb)
        dbPatcher.start()
        self.addCleanup(dbPatcher.stop)

        client = self.dash.app.test_client()
        if loggedIn:
            with client.session_transaction() as sess:
                sess["email"] = self.EMAIL
                sess["username"] = self.USERNAME
        return client

    def _token(self, client):
        """A CSRF token bound to this client's session, the way a rendered form
        or the JS fetch helpers obtain one."""
        with client.session_transaction():
            pass
        with self.dash.app.test_request_context():
            from flask_wtf.csrf import generate_csrf
            from flask import session as flaskSession

            token = generate_csrf()
            raw = flaskSession.get("csrf_token")
        with client.session_transaction() as sess:
            sess["csrf_token"] = raw
        return token


class TestCsrfIsActuallyEnforced(CsrfEnforcementTestCase):
    def test_logout_without_a_token_is_rejected(self):
        client = self._client()

        resp = client.post("/logout")

        self.assertEqual(resp.status_code, 400)

    def test_logout_with_a_valid_token_succeeds(self):
        client = self._client()
        token = self._token(client)

        resp = client.post("/logout", data={"csrf_token": token})

        self.assertNotEqual(resp.status_code, 400)

    def test_tag_api_without_a_token_is_rejected(self):
        """The tag APIs mutate stored user data and are called by fetch(), which
        must send X-CSRFToken - the header path needs the same guard as forms."""
        client = self._client()

        resp = client.post("/api/tags",
                           json={"tag": "chill", "entity_type": "track", "entity_id": "t1"})

        self.assertEqual(resp.status_code, 400)

    def test_tag_api_with_a_valid_token_header_is_accepted(self):
        client = self._client()
        token = self._token(client)

        resp = client.post("/api/tags",
                           json={"tag": "chill", "entity_type": "track", "entity_id": "t1"},
                           headers={"X-CSRFToken": token})

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])

    def test_a_forged_token_is_rejected(self):
        client = self._client()
        self._token(client)

        resp = client.post("/logout", data={"csrf_token": "not-the-real-token"})

        self.assertEqual(resp.status_code, 400)

    def test_admin_post_without_a_token_is_rejected(self):
        client = self._client()

        resp = client.post("/admin/create_backup")

        self.assertEqual(resp.status_code, 400)

    def test_get_requests_are_unaffected(self):
        client = self._client()

        resp = client.get("/login")

        self.assertEqual(resp.status_code, 200)


class TestCsrfIsDisabledForTheRestOfTheSuite(AppTestCase):
    """Pins the deliberate escape hatch itself: every other test posts without a
    token, so this must stay off under pytest - and be off ONLY because of the
    test-environment check, not because CSRFProtect was never wired."""

    def test_pytest_runs_with_csrf_disabled(self):
        dash = self._makeApp()

        self.assertFalse(dash.app.config["WTF_CSRF_ENABLED"])

    def test_csrf_protection_is_registered_on_the_app(self):
        dash = self._makeApp()

        self.assertIn("csrf", dash.app.extensions)


if __name__ == "__main__":
    unittest.main()
