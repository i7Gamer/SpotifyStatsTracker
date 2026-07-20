"""The biography-backfill progress card on /overview - regression coverage
for the `rows` variable actually reaching templates/_biography_progress.html
(bug: overview.html used a bare {% include %} while the partial expects
`rows`, so biography_rows never crossed the include boundary and the card
silently rendered empty)."""
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp


class OverviewBiographyTestBase(unittest.TestCase):
    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    _MOCK_STATS = {"tracks": 10, "artists": 5, "albums": 3, "plays": 100,
                   "total_time_ms": 36000000, "db_size_bytes": 1048576}

    def _makeDb(self, biographyCoverage=None, artistWorkerStatus=None, albumWorkerStatus=None):
        db = MagicMock()
        db.getListenerHealth.return_value = {"status": "HEALTHY", "error_count": 0,
                                             "last_error": None, "seconds_since_last_poll": 5}
        db.repo = MagicMock()
        if biographyCoverage is not None:
            db.repo.getBiographyCoverage.return_value = biographyCoverage
        if artistWorkerStatus is not None:
            db.getLastfmBiographyWorkerStatus.return_value = artistWorkerStatus
        if albumWorkerStatus is not None:
            db.getLastfmAlbumBiographyWorkerStatus.return_value = albumWorkerStatus
        return db

    def _getOverview(self, dash, db, isAdmin=False):
        with patch.object(dash.repo, 'getGlobalDatabaseStats', return_value=self._MOCK_STATS), \
             patch.object(dash.repo, 'getAllUsersDetails', return_value=[]), \
             patch.object(dash.repo, 'isAdmin', return_value=isAdmin), \
             patch.object(dash.repo, 'getPlaysCount', return_value=123), \
             patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_user_db', return_value=db):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get("/overview")


class TestOverviewBiographyCard(OverviewBiographyTestBase):
    def test_logged_in_user_sees_progress_bars_and_worker_badges(self):
        dash = self._makeApp()
        db = self._makeDb(
            biographyCoverage={"artist": {"covered": 29, "total": 90},
                               "album": {"covered": 12, "total": 45}},
            artistWorkerStatus={"configured": True, "running": True},
            albumWorkerStatus={"configured": True, "running": False},
        )

        resp = self._getOverview(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Biography Backfill Progress", resp.data)
        # These come from _biography_progress.html's `rows` loop - if the
        # include doesn't pass biography_rows through as `rows`, the loop
        # iterates zero times and none of this shows up.
        self.assertIn(b"29", resp.data)
        self.assertIn(b"90", resp.data)
        self.assertIn(b"12", resp.data)
        self.assertIn(b"45", resp.data)
        self.assertIn(b"WORKER RUNNING", resp.data)
        self.assertIn(b"WORKER IDLE", resp.data)

    def test_unconfigured_worker_shows_no_api_key_badge(self):
        dash = self._makeApp()
        db = self._makeDb(
            biographyCoverage={"artist": {"covered": 0, "total": 10},
                               "album": {"covered": 0, "total": 10}},
        )

        resp = self._getOverview(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"NO API KEY", resp.data)


if __name__ == "__main__":
    unittest.main()
