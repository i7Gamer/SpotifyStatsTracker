"""GET /health: a cheap, unauthenticated liveness/readiness check for container
orchestration and uptime monitoring - none of the app's other routes serve
this purpose without either requiring auth or doing real work.
"""
import unittest
from unittest.mock import patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class TestHealthRoute(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def test_returns_200_and_ok_status_when_db_is_reachable(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        resp = client.get("/health")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok"})

    def test_does_not_require_authentication(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        # No session cookie set at all.

        resp = client.get("/health")

        self.assertEqual(resp.status_code, 200)

    def test_returns_503_when_the_database_is_unreachable(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        with patch.object(dash.repo, 'connection', side_effect=RuntimeError("database is locked")):
            resp = client.get("/health")

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json().get("status"), "error")


if __name__ == "__main__":
    unittest.main()
