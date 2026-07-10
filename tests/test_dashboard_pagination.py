import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# NOTE: unlike some sibling test modules, this file deliberately does NOT swap
# Database modules for MagicMocks in sys.modules. These tests only exercise the
# dashboard route with a per-test mock db (via get_user_db), so module mocks are
# unnecessary - and a module-level mock/restore here would poison the
# patch("Database.database...") targets of test files that run after this one.
import app as appModule
from app import SpotifyDashboardApp


class TestDashboardPagination(unittest.TestCase):
    """Without a search query the dashboard must only materialize the page being
    shown - joining full track metadata onto every entry ever recorded on every
    request gets slow once the history grows large."""

    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _makeDb(self, entryCount):
        db = MagicMock()
        db.getEntriesFromNew.return_value = []
        db.getEntriesCount.return_value = entryCount
        db.getOverallStats.return_value = {
            "currentTopSongs": [],
            "currentTopArtists": [],
            "totalSongsPlayed": 0,
            "totalDurationMs": 0,
            "previousSongsPlayed": 0,
            "previousDurationMs": 0,
        }
        return db

    def _getDashboard(self, dash, db, query=""):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/{query}")

    def test_without_search_fetches_only_one_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp = self._getDashboard(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=0)
        self.assertIn(b"Page 1 of 3", resp.data)

    def test_without_search_requests_correct_offset_for_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp = self._getDashboard(dash, db, query="?page=2")

        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=appModule.PAGE_SIZE)
        self.assertIn(b"Page 2 of 3", resp.data)

    def test_without_search_clamps_page_beyond_range(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp = self._getDashboard(dash, db, query="?page=99")

        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=2 * appModule.PAGE_SIZE)
        self.assertIn(b"Page 3 of 3", resp.data)

    def test_without_search_handles_empty_database(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getDashboard(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=0)
        self.assertIn(b"Page 1 of 1", resp.data)

    def test_with_search_still_scans_full_history(self):
        """Search has to look at everything, so the full-pagination path stays."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp = self._getDashboard(dash, db, query="?q=foo")

        self.assertEqual(resp.status_code, 200)
        db.getEntriesFromNew.assert_called_once_with()
        db.getEntriesCount.assert_not_called()


if __name__ == "__main__":
    unittest.main()
