import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# NOTE: this file deliberately does NOT swap Database modules for MagicMocks in
# sys.modules - see test_artist_image_route.py for why that pattern poisons
# later test files.
from app import SpotifyDashboardApp
from _app_factory import AppTestCase
from _charts_client import HX_HEADERS, chartData


class TestChartsRoute(AppTestCase):
    """The /charts page loads in two phases: the GET returns a lightweight shell
    (the filter card + an empty placeholder) and htmx fetches the chart card
    after first paint. Shell assertions check the filter markup; the canvases
    and every chart dataset live in the fragment - the datasets inside its JSON
    data island, which `chartData` parses the way the page does. The transport
    itself is pinned in tests/test_charts_htmx.py."""

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
        #< the Charts payload carries these too; an unstubbed MagicMock is not
        #  JSON-serializable, so jsonify would 500 the whole route
        db.getMostSkippedSongs.return_value = []
        db.getMostSkippedArtists.return_value = []
        db.repo.getUserSettings.return_value = {"default_dashboard_window": "month", "timezone": None}
        return db

    def _get(self, dash, db, query=""):
        """The page shell (no ajax)."""
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/charts{query}")

    def _getData(self, dash, db, query=""):
        """The chart card, as htmx asks for it."""
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/charts{query}", headers=HX_HEADERS)

    def test_redirects_unauthenticated_users_to_login(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=False):
            resp = client.get('/charts')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.headers['Location'])

    def test_shell_renders_the_filter_card_without_running_data_queries(self):
        """The GET is a shell: it must render the filter controls but defer
        every heavy per-range query - and the canvases they fill - to the
        fragment."""
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._get(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Charts", resp.data)
        self.assertIn(b'id="chartsCard"', resp.data)
        self.assertNotIn(b"timeSeriesChart", resp.data)
        db.getListeningTimeSeries.assert_not_called()
        db.getArtistTrend.assert_not_called()
        db.getHourOfDayHeatmap.assert_not_called()

    def test_the_fragment_runs_the_data_queries_and_carries_every_series(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getData(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/html")
        db.getListeningTimeSeries.assert_called_once()
        db.getHourOfDayHeatmap.assert_called_once()
        db.getArtistTrend.assert_called_once()
        payload = chartData(resp)
        for key in ("timeSeries", "heatmap", "artistTrend", "explicitRatio",
                    "decadeDistribution", "completionStats"):
            self.assertIn(key, payload)
        #< the interval label is markup now, not a payload key for the client
        #  to write into a <span>
        self.assertIn(b"Listening Time", resp.data)

    def test_default_time_window_setting_is_used_when_no_interval_given(self):
        """Charts must honor the user's saved default_dashboard_window
        profile setting (like the dashboard and compare routes already do)
        instead of hardcoding 'month'."""
        dash = self._makeApp()
        db = self._makeDb()
        db.repo.getUserSettings.return_value = {"default_dashboard_window": "week", "timezone": None}

        resp = self._get(dash, db)

        self.assertIn(b'<option value="week" selected>Last Week</option>', resp.data)

    def test_explicit_interval_param_overrides_the_default_time_window_setting(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.repo.getUserSettings.return_value = {"default_dashboard_window": "week", "timezone": None}

        resp = self._get(dash, db, query="?interval=year")

        self.assertIn(b'<option value="year" selected>Last Year</option>', resp.data)

    def test_decade_distribution_order_survives_json_serialization(self):
        """getReleaseDecadeDistribution returns decades chronologically
        (Database/database.py's `ORDER BY decade`). Shipping the payload as an
        ordered [label, value] list (not a dict) keeps that order through
        Flask's key-sorting JSON provider."""
        dash = self._makeApp()
        db = self._makeDb()
        db.getReleaseDecadeDistribution.return_value = {"1990s": 5, "2000s": 40, "2010s": 15}

        resp = self._getData(dash, db)

        self.assertEqual(resp.status_code, 200)
        payload = chartData(resp)
        self.assertEqual([pair[0] for pair in payload["decadeDistribution"]],
                         ["1990s", "2000s", "2010s"])
        _, kwargs = db.getListeningTimeSeries.call_args
        self.assertEqual(kwargs["groupBy"], "day")
        self.assertIsNotNone(kwargs["startDate"])
        self.assertIsNotNone(kwargs["endDate"])

    def test_groupby_param_is_passed_through(self):
        dash = self._makeApp()
        db = self._makeDb()

        self._getData(dash, db, query="?groupBy=week")

        _, kwargs = db.getListeningTimeSeries.call_args
        self.assertEqual(kwargs["groupBy"], "week")

    def test_month_groupby_is_passed_through_and_selected(self):
        dash = self._makeApp()
        db = self._makeDb()

        data = self._getData(dash, db, query="?groupBy=month")
        _, kwargs = db.getListeningTimeSeries.call_args
        self.assertEqual(kwargs["groupBy"], "month")
        _, trendKwargs = db.getArtistTrend.call_args
        self.assertEqual(trendKwargs["groupBy"], "month")
        self.assertEqual(chartData(data)["groupBy"], "month")

        shell = self._get(dash, db, query="?groupBy=month")
        self.assertIn(b'<option value="month" selected>Month</option>', shell.data)

    def test_time_series_range_is_embedded_for_click_through(self):
        dash = self._makeApp()
        db = self._makeDb()   #< default label "2026-07-01", default groupBy "day"

        resp = self._getData(dash, db)

        first = chartData(resp)["timeSeries"][0]
        self.assertEqual(first["rangeStart"], "2026-07-01")
        self.assertEqual(first["rangeEnd"], "2026-07-01")

    def test_single_day_view_hourly_buckets_get_no_range(self):
        """chartsPage() switches to hourly buckets for a single-day interval
        (see timeSeriesGroupBy) - those have no clean calendar-date mapping,
        so they must not carry a (wrong) rangeStart/rangeEnd, and the artist
        trend is dropped entirely (null)."""
        dash = self._makeApp()
        db = self._makeDb()
        db.getListeningTimeSeries.return_value = [
            {"label": "2026-07-01 14:00", "totalTimeListened": 1000, "plays": 1},
        ]

        resp = self._getData(dash, db, query="?interval=day")

        payload = chartData(resp)
        self.assertNotIn("rangeStart", payload["timeSeries"][0])
        self.assertIsNone(payload["artistTrend"])
        #< the date the heading names, rendered rather than shipped as a key
        self.assertIn(b"Listening Time", resp.data)

    def test_artist_trend_series_id_is_embedded_for_click_through(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.getArtistTrend.return_value = {
            "buckets": ["2026-07-01"],
            "series": [{"name": "Artist A", "id": "artist-123", "data": [5]}],
        }

        resp = self._getData(dash, db)

        self.assertEqual(chartData(resp)["artistTrend"]["series"][0]["id"], "artist-123")

    def test_invalid_groupby_falls_back_to_day(self):
        dash = self._makeApp()
        db = self._makeDb()

        self._getData(dash, db, query="?groupBy=nonsense")

        _, kwargs = db.getListeningTimeSeries.call_args
        self.assertEqual(kwargs["groupBy"], "day")

    def test_all_time_interval_passes_none_dates(self):
        dash = self._makeApp()
        db = self._makeDb()

        self._getData(dash, db, query="?interval=all+time")

        _, kwargs = db.getListeningTimeSeries.call_args
        self.assertIsNone(kwargs["startDate"])
        self.assertIsNone(kwargs["endDate"])

    def test_custom_interval_without_dates_falls_back_to_default(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getData(dash, db, query="?interval=custom")

        self.assertEqual(resp.status_code, 200)
        _, kwargs = db.getListeningTimeSeries.call_args
        self.assertIsNotNone(kwargs["startDate"])

    def test_time_series_data_is_embedded_in_the_fragment(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getData(dash, db)

        self.assertIn(b"2026-07-01", resp.data)


if __name__ == "__main__":
    unittest.main()
