"""After logging in, the user should land back on the page that redirected
them to /login (?next=...) instead of always on the dashboard - and a POST to
/login with a tampered/unknown `step` must redirect rather than fall through
with no return value (which Flask turns into a 500).
"""
import unittest
from unittest.mock import patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class TestLoginNextRedirect(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _postLogin(self, dash, next_url, email="alice@example.com", cookies="sp_dc=abc"):
        client = dash.app.test_client()
        data = {"step": "2", "email": email, "cookies": cookies}
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

    def test_next_survives_a_failed_step_1_submission(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        resp = client.post("/login", data={"step": "1", "email": "", "next": "/top-albums"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'name="next" value="/top-albums"', resp.data)


class TestLoginUnknownStep(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def test_unrecognized_step_redirects_instead_of_500(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        resp = client.post("/login", data={"step": "99"})

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/login"))


if __name__ == "__main__":
    unittest.main()
