import unittest
from unittest.mock import patch, MagicMock
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# NOTE: this file deliberately does NOT swap Database modules for MagicMocks in
# sys.modules. These tests never construct a real Database (user_databases is
# populated with per-test mocks), and a module-level mock/restore here would
# poison the patch("Database.database...") targets of test files that run after
# this one - which used to silently send tests to the real network.
from app import SpotifyDashboardApp


class TestServeArtistImageRoute(unittest.TestCase):
    def setUp(self):
        # Keep tests from regenerating the real secrets/flask_secret_key.txt
        # (the mocked Path.exists in _makeApp would otherwise force a rewrite).
        patcher = patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    @patch('app.send_from_directory')
    @patch('app.os.path.exists')
    def test_delegates_lazy_fetch_to_the_cached_user_database(self, mock_path_exists, mock_send):
        """The route must not reimplement the scrape itself; it should delegate to the
        already-instantiated Database for that user so the fetch is deduplicated
        instead of duplicating the download logic inline."""
        mock_path_exists.return_value = False
        mock_send.return_value = "OK"

        dash = self._makeApp()
        fakeDb = MagicMock()
        dash.user_databases["alice"] = fakeDb

        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get('/img/alice/artists/artist123.jpeg')

        self.assertEqual(resp.status_code, 200)
        fakeDb.lazyFetchArtistImage.assert_called_once()
        calledArtistId, calledPath = fakeDb.lazyFetchArtistImage.call_args[0]
        self.assertEqual(calledArtistId, "artist123")
        # Images are shared across every user now, not stored per user - the path
        # is under the shared Media/artists dir, with no username segment.
        self.assertTrue(str(calledPath).endswith(os.path.join("Media", "artists", "artist123.jpeg")))

    @patch('app.send_from_directory')
    @patch('app.os.path.exists')
    def test_skips_lazy_fetch_when_file_already_present(self, mock_path_exists, mock_send):
        mock_path_exists.return_value = True
        mock_send.return_value = "OK"

        dash = self._makeApp()
        fakeDb = MagicMock()
        dash.user_databases["alice"] = fakeDb

        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get('/img/alice/artists/artist123.jpeg')

        self.assertEqual(resp.status_code, 200)
        fakeDb.lazyFetchArtistImage.assert_not_called()

    @patch('app.send_from_directory')
    @patch('app.os.path.exists')
    def test_serves_existing_file_without_error_when_no_database_cached_yet(self, mock_path_exists, mock_send):
        """If no Database has been instantiated for this username (e.g. server just
        restarted), the route must not crash - it just skips the lazy fetch."""
        mock_path_exists.return_value = False
        mock_send.return_value = "OK"

        dash = self._makeApp()

        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='unknown_user'):
            with client.session_transaction() as sess:
                sess['email'] = 'unknown_user@example.com'
            resp = client.get('/img/unknown_user/artists/artist123.jpeg')

        self.assertEqual(resp.status_code, 200)

    @patch('app.send_from_directory')
    @patch('app.os.path.exists')
    def test_denies_lazy_fetch_for_mismatched_session_user(self, mock_path_exists, mock_send):
        """Authorization must be checked before any lazy-fetch delegation happens."""
        mock_path_exists.return_value = False
        mock_send.return_value = "OK"

        dash = self._makeApp()
        fakeDb = MagicMock()
        dash.user_databases["bob"] = fakeDb

        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get('/img/bob/artists/artist123.jpeg')

        self.assertEqual(resp.status_code, 404)
        fakeDb.lazyFetchArtistImage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
