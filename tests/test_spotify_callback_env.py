"""Tests for the Spotify Developer API integration conditional display and route protection
based on the SPOTIFY_CALLBACK_URL environment variable.
"""
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp


class TestSpotifyCallbackEnv(unittest.TestCase):
    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        app_inst = SpotifyDashboardApp()
        app_inst.app.config["WTF_CSRF_ENABLED"] = False
        return app_inst

    def _login(self, dash, client):
        with client.session_transaction() as sess:
            sess["email"] = "alice@example.com"
            sess["username"] = "alice"
        dash.is_user_logged_in = MagicMock(return_value=True)
        dash.get_username_for_email = MagicMock(return_value="alice")

    @patch.dict(os.environ, {}, clear=True)
    def test_feature_disabled_when_env_var_not_set(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        self._login(dash, client)

        with patch.object(dash, 'get_user_db') as mock_get_db:
            mock_db = MagicMock()
            mock_db.getUserSpotifyCredentials.return_value = {}
            mock_get_db.return_value = mock_db

            # GET /profile
            resp = client.get("/profile")
            self.assertEqual(resp.status_code, 200)
            self.assertNotIn(b"Spotify Developer API Settings", resp.data)
            self.assertNotIn(b"Connection Status", resp.data)

            # POST /profile should return 404
            resp = client.post("/profile", data={"client_id": "id", "client_secret": "secret"})
            self.assertEqual(resp.status_code, 404)

            # GET /profile/disconnect should return 404
            resp = client.get("/profile/disconnect")
            self.assertEqual(resp.status_code, 404)

            # GET /spotify-authorize should return 404
            resp = client.get("/spotify-authorize")
            self.assertEqual(resp.status_code, 404)

            # GET /spotify-callback should return 404
            resp = client.get("/spotify-callback")
            self.assertEqual(resp.status_code, 404)

    @patch.dict(os.environ, {"SPOTIFY_CALLBACK_URL": "http://localhost:5000/spotify-callback"})
    def test_feature_enabled_when_env_var_is_set(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        self._login(dash, client)

        with patch.object(dash, 'get_user_db') as mock_get_db:
            mock_db = MagicMock()
            mock_db.getUserSpotifyCredentials.return_value = {"client_id": "my_id", "client_secret": "my_secret"}
            mock_get_db.return_value = mock_db

            # GET /profile
            resp = client.get("/profile")
            self.assertEqual(resp.status_code, 200)
            self.assertIn(b"Spotify Developer API Settings", resp.data)
            self.assertIn(b"Connection Status", resp.data)

            # GET /spotify-authorize should redirect
            resp = client.get("/spotify-authorize")
            self.assertEqual(resp.status_code, 302)
            self.assertIn("https://accounts.spotify.com/authorize", resp.headers["Location"])
            self.assertIn("redirect_uri=http://localhost:5000/spotify-callback", resp.headers["Location"])


if __name__ == "__main__":
    unittest.main()
