"""The Last.fm API key section on /profile: save (with live key validation),
remove, rate limiting and encrypted storage."""
import unittest
from unittest.mock import patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp, RATE_LIMIT_MAX_ATTEMPTS

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


def _lastfmResponse(statusCode=200, payload=None):
    response = MagicMock()
    response.status_code = statusCode
    response.json.return_value = payload if payload is not None else {
        "toptags": {"tag": [{"name": "pop", "count": 100}]}}
    return response


class ProfileLastfmTestCase(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def setUp(self):
        self.dash = self._makeApp()

    def _makeDb(self, username):
        """MagicMock db whose Last.fm key accessors delegate to the real repo,
        so the route's storage effects are observable."""
        db = MagicMock()
        db.repo = self.dash.repo
        db.getUserSpotifyCredentials.return_value = {}
        db.getUserLastfmApiKey.side_effect = lambda: self.dash.repo.getUserLastfmApiKey(username)
        db.updateUserLastfmApiKey.side_effect = lambda key: self.dash.repo.updateUserLastfmApiKey(username, key)
        return db

    def _loginAs(self, username, email):
        self.dash.repo.upsertUser(username, email)
        self.db = self._makeDb(username)
        for patcher in (
            patch.object(self.dash, 'is_user_logged_in', return_value=True),
            patch.object(self.dash, 'get_username_for_email', return_value=username),
            patch.object(self.dash, 'get_user_db', return_value=self.db),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        client = self.dash.app.test_client()
        with client.session_transaction() as sess:
            sess['email'] = email
            sess['username'] = username
        return client


class TestLastfmSectionRendering(ProfileLastfmTestCase):
    def test_section_renders_without_the_spotify_callback_env(self):
        self.assertNotIn("SPOTIFY_CALLBACK_URL", os.environ)
        client = self._loginAs("alice", "alice@example.com")
        resp = client.get("/profile")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Last.fm API Settings", resp.data)
        self.assertIn(b'name="lastfm_api_key"', resp.data)

    def test_status_reflects_a_stored_key(self):
        client = self._loginAs("alice", "alice@example.com")
        self.assertIn(b"Not Configured", client.get("/profile").data)

        self.dash.repo.updateUserLastfmApiKey("alice", "key123")
        resp = client.get("/profile")
        self.assertIn(b"remove_lastfm", resp.data)   #< remove button only with a stored key
        self.assertNotIn(b"key123", resp.data)       #< the key itself is never echoed back

    def test_section_hides_when_the_admin_disables_lastfm_backfill(self):
        self.dash.repo.setLastfmGenreBackfillEnabled(False)
        client = self._loginAs("alice", "alice@example.com")
        resp = client.get("/profile")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"Last.fm API Settings", resp.data)
        self.assertNotIn(b'name="lastfm_api_key"', resp.data)


class TestSaveLastfmKey(ProfileLastfmTestCase):
    @patch("Database.lastfm.requests.get")
    def test_valid_key_is_stored_encrypted_and_starts_the_worker(self, mockGet):
        mockGet.return_value = _lastfmResponse()
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile", data={"action": "save_lastfm", "lastfm_api_key": "goodkey123"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Last.fm API key saved", resp.data)
        self.assertEqual(self.dash.repo.getUserLastfmApiKey("alice"), "goodkey123")
        raw = self.dash.repo._conn().execute(
            "SELECT lastfm_api_key FROM users WHERE username='alice'").fetchone()[0]
        self.assertTrue(raw.startswith("enc:v1:"))
        self.db.startLastfmGenreBackfiller.assert_called_once()
        self.db.startLastfmBiographyBackfiller.assert_called_once()
        self.db.startLastfmAlbumBiographyBackfiller.assert_called_once()

    @patch("Database.lastfm.requests.get")
    def test_rejected_key_is_not_stored(self, mockGet):
        mockGet.return_value = _lastfmResponse(statusCode=403, payload={"error": 10})
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile", data={"action": "save_lastfm", "lastfm_api_key": "badkey"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"rejected", resp.data)
        self.assertIsNone(self.dash.repo.getUserLastfmApiKey("alice"))
        self.db.startLastfmGenreBackfiller.assert_not_called()
        self.db.startLastfmBiographyBackfiller.assert_not_called()
        self.db.startLastfmAlbumBiographyBackfiller.assert_not_called()

    @patch("Database.lastfm.requests.get")
    def test_unreachable_lastfm_is_not_stored(self, mockGet):
        import requests as requestsModule
        mockGet.side_effect = requestsModule.exceptions.ConnectionError("down")
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile", data={"action": "save_lastfm", "lastfm_api_key": "somekey"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Could not reach Last.fm", resp.data)
        self.assertIsNone(self.dash.repo.getUserLastfmApiKey("alice"))

    def test_blank_key_is_rejected_without_a_request(self):
        client = self._loginAs("alice", "alice@example.com")
        with patch("Database.lastfm.requests.get") as mockGet:
            resp = client.post("/profile", data={"action": "save_lastfm", "lastfm_api_key": "   "})
            mockGet.assert_not_called()
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"required", resp.data)

    def test_busy_rate_limit_budget_reports_without_storing(self):
        with patch("routes.auth.LastfmClient") as mockClientClass:
            mockClientClass.return_value.validateApiKey.return_value = {"ok": False, "error": "busy"}
            client = self._loginAs("alice", "alice@example.com")

            resp = client.post("/profile", data={"action": "save_lastfm", "lastfm_api_key": "somekey"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"busy right now", resp.data)
        self.assertIsNone(self.dash.repo.getUserLastfmApiKey("alice"))

    @patch("Database.lastfm.requests.get")
    def test_storage_failure_after_validation_reports_an_error(self, mockGet):
        mockGet.return_value = _lastfmResponse()
        client = self._loginAs("alice", "alice@example.com")
        self.db.updateUserLastfmApiKey.side_effect = RuntimeError("disk full")

        resp = client.post("/profile", data={"action": "save_lastfm", "lastfm_api_key": "goodkey123"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Failed to save the Last.fm API key", resp.data)
        self.assertNotIn(b"Last.fm API key saved", resp.data)

    @patch("Database.lastfm.requests.get")
    def test_save_is_rate_limited(self, mockGet):
        """Every save fires a live validation request against Last.fm - the
        action shares the per-IP limiter with login/register/request_share."""
        mockGet.return_value = _lastfmResponse()
        client = self._loginAs("alice", "alice@example.com")

        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            resp = client.post("/profile", data={"action": "save_lastfm", "lastfm_api_key": "goodkey123"})
            self.assertEqual(resp.status_code, 200)

        resp = client.post("/profile", data={"action": "save_lastfm", "lastfm_api_key": "goodkey123"})
        self.assertEqual(resp.status_code, 429)
        self.assertIn(b"Too many attempts", resp.data)

    @patch("Database.lastfm.requests.get")
    def test_disabled_refuses_new_keys(self, mockGet):
        self.dash.repo.setLastfmGenreBackfillEnabled(False)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile", data={"action": "save_lastfm", "lastfm_api_key": "goodkey123"})

        self.assertEqual(resp.status_code, 404)
        self.assertIsNone(self.dash.repo.getUserLastfmApiKey("alice"))
        mockGet.assert_not_called()


class TestRemoveLastfmKey(ProfileLastfmTestCase):
    def test_remove_clears_the_key_and_stops_the_worker(self):
        client = self._loginAs("alice", "alice@example.com")
        self.dash.repo.updateUserLastfmApiKey("alice", "key123")

        resp = client.post("/profile", data={"action": "remove_lastfm"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Last.fm API key removed", resp.data)
        self.assertIsNone(self.dash.repo.getUserLastfmApiKey("alice"))
        self.db.stopLastfmGenreBackfiller.assert_called_once()
        self.db.stopLastfmBiographyBackfiller.assert_called_once()
        self.db.stopLastfmAlbumBiographyBackfiller.assert_called_once()

    def test_remove_failure_reports_an_error(self):
        client = self._loginAs("alice", "alice@example.com")
        self.dash.repo.updateUserLastfmApiKey("alice", "key123")
        self.db.stopLastfmGenreBackfiller.side_effect = RuntimeError("thread wedged")

        resp = client.post("/profile", data={"action": "remove_lastfm"})

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Failed to remove the Last.fm API key", resp.data)
        self.assertNotIn(b"Last.fm API key removed", resp.data)


if __name__ == "__main__":
    unittest.main()
