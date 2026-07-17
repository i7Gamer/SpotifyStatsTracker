"""Tests for the Spotify Developer API integration conditional display and route protection
based on the SPOTIFY_CALLBACK_URL environment variable.
"""
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp, SPOTIFY_OAUTH_STATE_SESSION_KEY


class SpotifyEnvTestCase(unittest.TestCase):
    """Shared app/login scaffolding for the Spotify Developer API route tests."""

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


class TestSpotifyCallbackEnv(SpotifyEnvTestCase):
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


@patch.dict(os.environ, {"SPOTIFY_CALLBACK_URL": "http://localhost:5000/spotify-callback"})
class TestSpotifyOAuthState(SpotifyEnvTestCase):
    """The OAuth CSRF `state` round-trip (RFC 6749 §10.12): /spotify-authorize
    stores a one-shot random state in the session and sends it to Spotify;
    /spotify-callback only exchanges a code if the request echoes that exact
    state back - otherwise an attacker could complete the consent themselves
    and trick a logged-in victim into storing the attacker's refresh token."""

    def _makeLoggedInClient(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        self._login(dash, client)
        return dash, client

    def _mockDb(self, mock_get_db):
        mock_db = MagicMock()
        mock_db.getUserSpotifyCredentials.return_value = {
            "client_id": "my_id", "client_secret": "my_secret"}
        mock_get_db.return_value = mock_db
        return mock_db

    def test_authorize_stores_a_state_and_sends_it_to_spotify(self):
        dash, client = self._makeLoggedInClient()
        with patch.object(dash, 'get_user_db') as mock_get_db:
            self._mockDb(mock_get_db)
            resp = client.get("/spotify-authorize")

        self.assertEqual(resp.status_code, 302)
        with client.session_transaction() as sess:
            storedState = sess.get(SPOTIFY_OAUTH_STATE_SESSION_KEY)
        self.assertTrue(storedState)
        self.assertIn(f"&state={storedState}", resp.headers["Location"])

    def test_each_authorize_generates_a_fresh_state(self):
        dash, client = self._makeLoggedInClient()
        with patch.object(dash, 'get_user_db') as mock_get_db:
            self._mockDb(mock_get_db)
            first = client.get("/spotify-authorize").headers["Location"]
            second = client.get("/spotify-authorize").headers["Location"]
        self.assertNotEqual(first.split("&state=")[1], second.split("&state=")[1])

    def test_callback_rejects_a_missing_state(self):
        dash, client = self._makeLoggedInClient()
        with patch.object(dash, 'get_user_db') as mock_get_db, \
                patch("requests.post") as mock_post:
            mock_db = self._mockDb(mock_get_db)
            resp = client.get("/spotify-callback?code=attacker-code")

        mock_post.assert_not_called()
        mock_db.updateUserSpotifyCredentials.assert_not_called()
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])

    def test_callback_rejects_a_mismatched_state(self):
        dash, client = self._makeLoggedInClient()
        with client.session_transaction() as sess:
            sess[SPOTIFY_OAUTH_STATE_SESSION_KEY] = "expected-state"
        with patch.object(dash, 'get_user_db') as mock_get_db, \
                patch("requests.post") as mock_post:
            mock_db = self._mockDb(mock_get_db)
            resp = client.get("/spotify-callback?code=attacker-code&state=wrong-state")

        mock_post.assert_not_called()
        mock_db.updateUserSpotifyCredentials.assert_not_called()
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])

    def test_callback_accepts_the_matching_state_only_once(self):
        dash, client = self._makeLoggedInClient()
        with client.session_transaction() as sess:
            sess[SPOTIFY_OAUTH_STATE_SESSION_KEY] = "expected-state"

        tokenResponse = MagicMock(status_code=200)
        tokenResponse.json.return_value = {"refresh_token": "new-refresh-token"}
        with patch.object(dash, 'get_user_db') as mock_get_db, \
                patch("requests.post", return_value=tokenResponse) as mock_post:
            mock_db = self._mockDb(mock_get_db)
            resp = client.get("/spotify-callback?code=good-code&state=expected-state")

            mock_db.updateUserSpotifyCredentials.assert_called_once_with(
                "my_id", "my_secret", "new-refresh-token")
            self.assertIn("success=", resp.headers["Location"])

            # Replay: the stored state was consumed by the first exchange, so
            # the exact same URL must now be rejected without a token exchange.
            replay = client.get("/spotify-callback?code=good-code&state=expected-state")
            self.assertIn("error=", replay.headers["Location"])
            mock_post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
