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
        db.getExplicitRatio.return_value = {"explicit": 0, "clean": 0}
        db.getReleaseDecadeDistribution.return_value = {}
        db.getCompletionStats.return_value = {"skips": 0, "completes": 0, "partials": 0}
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

    def test_decade_distribution_order_survives_json_serialization(self):
        """getReleaseDecadeDistribution returns decades chronologically
        (Database/database.py's `ORDER BY decade`). Flask's JSON provider
        sorts plain dict keys alphabetically on serialization, which for
        decade labels happens to look identical (chronological order and
        alphabetical order agree for '1990s' < '2000s' < '2010s') - this
        pins the real mechanism (ordered [label, value] pairs, not a dict)
        rather than relying on that coincidence."""
        dash = self._makeApp()
        db = self._makeDb()
        db.getReleaseDecadeDistribution.return_value = {"1990s": 5, "2000s": 40, "2010s": 15}

        resp = self._get(dash, db)

        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        idx1990 = body.index('"1990s"')
        idx2000 = body.index('"2000s"')
        idx2010 = body.index('"2010s"')
        self.assertLess(idx1990, idx2000)
        self.assertLess(idx2000, idx2010)
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

    def test_month_groupby_is_passed_through_and_selected(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._get(dash, db, query="?groupBy=month")

        _, kwargs = db.getListeningTimeSeries.call_args
        self.assertEqual(kwargs["groupBy"], "month")
        _, trendKwargs = db.getArtistTrend.call_args
        self.assertEqual(trendKwargs["groupBy"], "month")
        self.assertIn(b'<option value="month" selected>Month</option>', resp.data)

    def test_time_series_range_is_embedded_for_click_through(self):
        dash = self._makeApp()
        db = self._makeDb()   #< default label "2026-07-01", default groupBy "day"

        resp = self._get(dash, db)

        body = resp.data.decode()
        self.assertIn('"rangeStart": "2026-07-01"', body)
        self.assertIn('"rangeEnd": "2026-07-01"', body)

    def test_single_day_view_hourly_buckets_get_no_range(self):
        """chartsPage() switches to hourly buckets for a single-day interval
        (see timeSeriesGroupBy) - those have no clean calendar-date mapping,
        so they must not carry a (wrong) rangeStart/rangeEnd."""
        dash = self._makeApp()
        db = self._makeDb()
        db.getListeningTimeSeries.return_value = [
            {"label": "2026-07-01 14:00", "totalTimeListened": 1000, "plays": 1},
        ]

        resp = self._get(dash, db, query="?interval=day")

        body = resp.data.decode()
        self.assertNotIn("rangeStart", body)

    def test_artist_trend_series_id_is_embedded_for_click_through(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.getArtistTrend.return_value = {
            "buckets": ["2026-07-01"],
            "series": [{"name": "Artist A", "id": "artist-123", "data": [5]}],
        }

        resp = self._get(dash, db)

        self.assertIn(b'"id": "artist-123"', resp.data)

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
