import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# NOTE: this file deliberately does NOT swap Database modules for MagicMocks in
# sys.modules - see test_artist_image_route.py for why that pattern poisons
# later test files.
from app import SpotifyDashboardApp


class TestChartsRoute(unittest.TestCase):
    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _makeDb(self):
        db = MagicMock()
        db.getListeningTimeSeries.return_value = [
            {"label": "2026-07-01", "totalTimeListened": 1000, "plays": 1},
        ]
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getArtistTrend.return_value = {"buckets": [], "series": []}
        return db

    def _get(self, dash, db, query=""):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/charts{query}")

    def test_redirects_unauthenticated_users_to_login(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=False):
            resp = client.get('/charts')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])

    def test_renders_with_default_month_interval(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._get(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getListeningTimeSeries.assert_called_once()
        _, kwargs = db.getListeningTimeSeries.call_args
        self.assertEqual(kwargs["groupBy"], "day")
        self.assertIsNotNone(kwargs["startDate"])
        self.assertIsNotNone(kwargs["endDate"])
        db.getHourOfDayHeatmap.assert_called_once()
        db.getArtistTrend.assert_called_once()
        self.assertIn(b"Charts", resp.data)
        self.assertIn(b"timeSeriesChart", resp.data)
        self.assertIn(b"heatmapChart", resp.data)
        self.assertIn(b"artistTrendChart", resp.data)

    def test_groupby_param_is_passed_through(self):
        dash = self._makeApp()
        db = self._makeDb()

        self._get(dash, db, query="?groupBy=week")

        _, kwargs = db.getListeningTimeSeries.call_args
        self.assertEqual(kwargs["groupBy"], "week")

    def test_invalid_groupby_falls_back_to_day(self):
        dash = self._makeApp()
        db = self._makeDb()

        self._get(dash, db, query="?groupBy=nonsense")

        _, kwargs = db.getListeningTimeSeries.call_args
        self.assertEqual(kwargs["groupBy"], "day")

    def test_all_time_interval_passes_none_dates(self):
        dash = self._makeApp()
        db = self._makeDb()

        self._get(dash, db, query="?interval=all+time")

        _, kwargs = db.getListeningTimeSeries.call_args
        self.assertIsNone(kwargs["startDate"])
        self.assertIsNone(kwargs["endDate"])

    def test_custom_interval_without_dates_falls_back_to_month(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._get(dash, db, query="?interval=custom")

        self.assertEqual(resp.status_code, 200)
        _, kwargs = db.getListeningTimeSeries.call_args
        self.assertIsNotNone(kwargs["startDate"])

    def test_time_series_data_is_embedded_in_page(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._get(dash, db)

        self.assertIn(b"2026-07-01", resp.data)


if __name__ == "__main__":
    unittest.main()
