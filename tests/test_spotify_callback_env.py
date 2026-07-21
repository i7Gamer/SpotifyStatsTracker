"""Tests for the Spotify Developer API integration conditional display and route protection
based on the SPOTIFY_CALLBACK_URL environment variable.
"""
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp, SPOTIFY_OAUTH_STATE_SESSION_KEY
from _app_factory import makeApp


class SpotifyEnvTestCase(unittest.TestCase):
    """Shared app/login scaffolding for the Spotify Developer API route tests."""

    def _makeApp(self):
        app_inst = makeApp()
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
            # A fresh authorization always grants the scope /spotify-authorize
            # requested - any stale "needs reauth" flag must be cleared
            # immediately rather than waiting for the next backfill poll.
            mock_db.setSpotifyNeedsReauth.assert_called_once_with(False)
            self.assertIn("success=", resp.headers["Location"])

            # Replay: the stored state was consumed by the first exchange, so
            # the exact same URL must now be rejected without a token exchange.
            replay = client.get("/spotify-callback?code=good-code&state=expected-state")
            self.assertIn("error=", replay.headers["Location"])
            mock_post.assert_called_once()


@patch.dict(os.environ, {"SPOTIFY_CALLBACK_URL": "http://localhost:5000/spotify-callback"})
class TestSpotifyCallbackErrorDetails(SpotifyEnvTestCase):
    """Token-exchange failures must show the user a generic message - the raw
    Spotify response body / exception text belongs in the server log, not in a
    redirect query param (visible in browser history, access logs, referrers)."""

    def _callbackWithState(self, dash, client):
        with client.session_transaction() as sess:
            sess[SPOTIFY_OAUTH_STATE_SESSION_KEY] = "expected-state"
        return "/spotify-callback?code=good-code&state=expected-state"

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

    def test_non_200_exchange_does_not_leak_response_body(self):
        dash, client = self._makeLoggedInClient()
        url = self._callbackWithState(dash, client)
        errorResponse = MagicMock(status_code=400, text="invalid_client: SECRET-DETAIL-XYZ")
        with patch.object(dash, 'get_user_db') as mock_get_db, \
                patch("requests.post", return_value=errorResponse):
            self._mockDb(mock_get_db)
            resp = client.get(url)

        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])
        self.assertNotIn("SECRET-DETAIL-XYZ", resp.headers["Location"])

    def test_exchange_exception_does_not_leak_exception_text(self):
        dash, client = self._makeLoggedInClient()
        url = self._callbackWithState(dash, client)
        with patch.object(dash, 'get_user_db') as mock_get_db, \
                patch("requests.post", side_effect=Exception("SECRET-EXC-DETAIL-XYZ")):
            self._mockDb(mock_get_db)
            resp = client.get(url)

        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])
        self.assertNotIn("SECRET-EXC-DETAIL-XYZ", resp.headers["Location"])


@patch.dict(os.environ, {"SPOTIFY_CALLBACK_URL": "http://localhost:5000/spotify-callback"})
class TestProfilePageReauthStatus(SpotifyEnvTestCase):
    """Profile's Connection Status card must distinguish "never authorized"
    from "authorized, but the token is missing a required scope" - the
    latter needs a re-authorize prompt too, not just a silently-stuck
    background tracker (see Web API backfill's on_scope_status_change)."""

    def _getProfile(self, credsExtra):
        dash = self._makeApp()
        client = dash.app.test_client()
        self._login(dash, client)
        with patch.object(dash, 'get_user_db') as mock_get_db:
            mock_db = MagicMock()
            mock_db.getUserSpotifyCredentials.return_value = {
                "client_id": "my_id", "client_secret": "my_secret",
                "refresh_token": "rt", **credsExtra,
            }
            mock_get_db.return_value = mock_db
            return client.get("/profile")

    def test_shows_reauth_prompt_when_flagged(self):
        resp = self._getProfile({"needs_reauth": True})
        self.assertIn(b"Authorization Expired - Missing Permission", resp.data)
        self.assertIn(b"Re-authorize with Spotify", resp.data)

    def test_shows_connected_when_not_flagged(self):
        resp = self._getProfile({"needs_reauth": False})
        self.assertIn(b"Connected & Authorized", resp.data)
        self.assertNotIn(b"Authorization Expired", resp.data)

    def test_missing_needs_reauth_key_defaults_to_connected(self):
        """A credentials dict without the key at all (e.g. a mock that
        predates this field) must not be mistaken for "needs reauth"."""
        resp = self._getProfile({})
        self.assertIn(b"Connected & Authorized", resp.data)
        self.assertNotIn(b"Authorization Expired", resp.data)


if __name__ == "__main__":
    unittest.main()
