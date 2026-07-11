import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# NOTE: like test_dashboard_pagination.py, this file deliberately does NOT swap
# Database modules for MagicMocks in sys.modules - it only exercises the route
# with a per-test mock db (via get_user_db).
import app as appModule
from app import SpotifyDashboardApp


class TestTopAlbumsRoute(unittest.TestCase):
    """/top-albums must only ask the DB layer for the current page (SQL-level
    LIMIT/OFFSET) when there's no search query, mirroring /top-songs."""

    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _makeDb(self, albumCount=0):
        db = MagicMock()
        db.getTopAlbums.return_value = []
        db.getAlbumsCount.return_value = albumCount
        db.getPlayTotals.return_value = (0, 0)
        return db

    def _getTopAlbums(self, dash, db, query=""):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/top-albums{query}")

    def test_without_search_fetches_only_one_page(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getTopAlbums(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getAlbumsCount.assert_called_once()
        db.getTopAlbums.assert_called_once()
        kwargs = db.getTopAlbums.call_args.kwargs
        self.assertEqual(kwargs["limit"], appModule.PAGE_SIZE)
        self.assertEqual(kwargs["offset"], 0)
        self.assertEqual(kwargs["by"], "totalTimeListened")   #< topAlbumsPage's default sortBy

    def test_without_search_requests_correct_offset_for_page(self):
        dash = self._makeApp()
        db = self._makeDb(albumCount=120)

        resp = self._getTopAlbums(dash, db, query="?page=2")

        self.assertEqual(resp.status_code, 200)
        kwargs = db.getTopAlbums.call_args.kwargs
        self.assertEqual(kwargs["offset"], appModule.PAGE_SIZE)
        self.assertIn(b"Page 2 of 3", resp.data)

    def test_without_search_passes_requested_sort(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getTopAlbums(dash, db, query="?sortBy=plays")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.getTopAlbums.call_args.kwargs["by"], "plays")

    def test_without_search_handles_empty_database(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getTopAlbums(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Page 1 of 1", resp.data)

    def test_with_search_still_scans_full_history(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getTopAlbums(dash, db, query="?q=foo")

        self.assertEqual(resp.status_code, 200)
        db.getAlbumsCount.assert_not_called()
        kwargs = db.getTopAlbums.call_args.kwargs
        self.assertNotIn("limit", kwargs)
        self.assertNotIn("offset", kwargs)

    def test_totals_come_from_get_play_totals(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.getPlayTotals.return_value = (42, 999000)

        resp = self._getTopAlbums(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getPlayTotals.assert_called_once()
        self.assertIn(b'<p class="summary-value">42</p>', resp.data)

    def test_page_survives_non_numeric_page(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getTopAlbums(dash, db, query="?page=abc")

        self.assertEqual(resp.status_code, 200)

    def test_nav_link_present(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getTopAlbums(dash, db)

        self.assertIn(b'/top-albums', resp.data)


if __name__ == "__main__":
    unittest.main()
