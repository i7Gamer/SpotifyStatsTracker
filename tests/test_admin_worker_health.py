"""Tests for expanded Admin Worker Health insights, including Database worker status
accessors and admin route worker state aggregation."""
import contextlib
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.database import Database
from app import SpotifyDashboardApp


class TestDatabaseWorkerStatusAccessors(unittest.TestCase):
    def setUp(self):
        self.repo_patcher = patch('Database.database.Repository')
        self.mock_repo_cls = self.repo_patcher.start()
        self.mock_repo = MagicMock()
        self.mock_repo_cls.return_value = self.mock_repo

        self.db = Database.__new__(Database)
        self.db.repo = self.mock_repo
        self.db.user = "alice"
        self.db.autoImporter = MagicMock()


    def tearDown(self):
        self.repo_patcher.stop()

    def test_get_spotify_api_worker_status_unconfigured(self):
        self.mock_repo.getUserSpotifyCredentials.return_value = None
        self.db.backfiller_thread = None

        status = self.db.getSpotifyApiWorkerStatus()
        self.assertFalse(status["configured"])
        self.assertFalse(status["running"])

    def test_get_spotify_api_worker_status_running(self):
        self.mock_repo.getUserSpotifyCredentials.return_value = {
            "client_id": "id", "client_secret": "sec", "refresh_token": "token"
        }
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        self.db.backfiller_thread = mock_thread

        status = self.db.getSpotifyApiWorkerStatus()
        self.assertTrue(status["configured"])
        self.assertTrue(status["running"])

    def test_get_auto_importer_worker_status(self):
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        mock_wd = MagicMock()
        mock_wd.thread = mock_thread
        mock_wd.run = True

        self.db.autoImporter = MagicMock()
        self.db.autoImporter.wd = mock_wd

        status = self.db.getAutoImporterWorkerStatus()
        self.assertTrue(status["configured"])
        self.assertTrue(status["running"])

    def test_get_wrapped_worker_status(self):
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        self.db.wrapped_thread = mock_thread

        status = self.db.getWrappedWorkerStatus()
        self.assertTrue(status["configured"])
        self.assertTrue(status["running"])


class TestAdminWorkerHealthRoute(unittest.TestCase):
    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def test_admin_route_renders_expanded_worker_health(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        dash = SpotifyDashboardApp()

        users = [
            {
                "username": "alice", "email": "alice@example.com",
                "cookies_json": '{"sp_dc": "123"}',
                "spotify_client_id": "client_id", "spotify_refresh_token": "refresh_token",
                "lastfm_api_key": "enc:v1:something",
                "created_at": 1718000000.0, "is_admin": True,
            }
        ]

        mock_db = MagicMock()
        mock_db.getListenerHealth.return_value = {"status": "HEALTHY"}
        mock_db.getLastfmWorkerStatus.return_value = {"configured": True, "running": True}
        mock_db.getSpotifyApiWorkerStatus.return_value = {"configured": True, "running": True}
        mock_db.getLastfmAlbumBiographyWorkerStatus.return_value = {"configured": True, "running": False}
        mock_db.getLastfmBiographyWorkerStatus.return_value = {"configured": True, "running": False}
        mock_db.getAutoImporterWorkerStatus.return_value = {"configured": True, "running": True}
        mock_db.getWrappedWorkerStatus.return_value = {"configured": True, "running": False}

        mock_backup = MagicMock()
        mock_backup.is_alive.return_value = True
        dash.backupWorker = mock_backup

        # adminPage()'s per-user row reads dashboard.user_databases (an
        # already-active session), not get_user_db() - populate it so
        # mock_db's worker statuses actually get exercised by the row.
        dash.user_databases = {"alice": mock_db}

        insights = {
            "getCatalogGenreCoverage": {
                "song": {"covered": 0, "total": 0, "percent": 0.0},
                "album": {"covered": 0, "total": 0, "percent": 0.0},
                "artist": {"covered": 0, "total": 0, "percent": 0.0},
                "overall": {"percent": 0.0},
            },
            "getCatalogBiographyCoverage": {
                "artist": {"covered": 0, "total": 0}, "album": {"covered": 0, "total": 0},
            },
            "getRecentRegistrationCounts": {"last_7_days": 0, "last_30_days": 0},
            "getInstanceShareCounts": {"pending": 0, "accepted": 0},
            "getActiveShareLinksCount": 0,
        }

        patches = [
            patch.object(dash.repo, 'getGlobalDatabaseStats', return_value={}),
            patch.object(dash.repo, 'getAllUsersDetails', return_value=users),
            patch.object(dash.repo, 'isAdmin', return_value=True),
            patch.object(dash.repo, 'getPlaysCount', return_value=10),
            patch.object(dash.repo, 'getSkipCount', return_value=2),
            patch.object(dash.repo, 'getAdminUsernames', return_value=['alice']),
            patch.object(dash, 'is_user_logged_in', return_value=True),
            patch.object(dash, 'get_username_for_email', return_value='alice'),
            patch.object(dash, 'get_user_db', return_value=mock_db),
        ]
        for name, value in insights.items():
            patches.append(patch.object(dash.repo, name, return_value=value))

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get("/admin")
            body = resp.data.decode()

            self.assertEqual(resp.status_code, 200)
            self.assertIn("Spotify API Backfill Workers", body)
            self.assertIn("Last.fm Album Bio Workers", body)
            self.assertIn("Last.fm Artist Bio Workers", body)
            self.assertIn("Auto-Importer Watchdogs", body)
            self.assertIn("Wrapped Calculation Workers", body)
            self.assertIn("Database Backup Service", body)

    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def test_catalog_backfill_coverage_layout_details_below_bar(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        dash = SpotifyDashboardApp()

        users = [
            {
                "username": "alice", "email": "alice@example.com",
                "cookies_json": None, "spotify_client_id": None, "spotify_refresh_token": None,
                "lastfm_api_key": None, "created_at": 1718000000.0, "is_admin": True,
            }
        ]

        insights = {
            "getCatalogGenreCoverage": {
                "song": {"covered": 10, "own_covered": 5, "total": 20, "percent": 50.0, "own_percent": 25.0, "ownPercent": 25.0},
                "album": {"covered": 15, "own_covered": 9, "total": 30, "percent": 50.0, "own_percent": 30.0, "ownPercent": 30.0},
                "artist": {"covered": 20, "own_covered": 20, "total": 40, "percent": 50.0, "own_percent": 50.0, "ownPercent": 50.0},
                "overall": {"percent": 50.0},
            },
            "getCatalogBiographyCoverage": {
                "artist": {"covered": 5, "total": 10}, "album": {"covered": 8, "total": 16},
            },
            "getRecentRegistrationCounts": {"last_7_days": 0, "last_30_days": 0},
            "getInstanceShareCounts": {"pending": 0, "accepted": 0},
            "getActiveShareLinksCount": 0,
        }

        patches = [
            patch.object(dash.repo, 'getGlobalDatabaseStats', return_value={}),
            patch.object(dash.repo, 'getAllUsersDetails', return_value=users),
            patch.object(dash.repo, 'isAdmin', return_value=True),
            patch.object(dash.repo, 'getPlaysCount', return_value=10),
            patch.object(dash.repo, 'getSkipCount', return_value=2),
            patch.object(dash.repo, 'getAdminUsernames', return_value=['alice']),
            patch.object(dash, 'is_user_logged_in', return_value=True),
            patch.object(dash, 'get_username_for_email', return_value='alice'),
            patch.object(dash, 'get_user_db', return_value=None),
        ]
        for name, value in insights.items():
            patches.append(patch.object(dash.repo, name, return_value=value))

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get("/admin")
            body = resp.data.decode()

            self.assertEqual(resp.status_code, 200)
            self.assertIn("Covered: 10 / 20", body)
            self.assertIn("Covered: 15 / 30", body)
            self.assertIn("Covered: 20 / 40", body)
            self.assertIn("Covered: 5 / 10", body)
            self.assertIn("Covered: 8 / 16", body)
            self.assertIn("Un-inherited: 25.0%", body)
            self.assertIn("Un-inherited: 30.0%", body)
            self.assertNotIn("Un-inherited: 50.0%", body)


if __name__ == "__main__":
    unittest.main()
