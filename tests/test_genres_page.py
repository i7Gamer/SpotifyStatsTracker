"""The dedicated /genres page: a two-phase load (shell GET + ?ajax=true data
payload), a time-period filter defaulting to the profile window, the all-time
unlock gate, default genre selection, ?genre= override with fallback, the
chip-click detail swap (scope=detail), nav-link visibility tied to the Last.fm
kill switch, and the mix-over-time series cap."""
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp, GENRE_MIX_TREND_TOP_N  # noqa: F401
from _app_factory import AppTestCase


def coverageDict(song, album, artist, total=1000):
    def category(percent):
        return {"covered": int(total * percent / 100), "total": total, "percent": percent}
    return {
        "song": category(song),
        "album": category(album),
        "artist": category(artist),
        "overall": {"percent": round((song + album + artist) / 3, 1)},
    }


class GenresPageTestCase(AppTestCase):
    def _makeDb(self, coverage=None, distribution=None, window="all time"):
        db = MagicMock()
        db.repo.getUserSettings.return_value = {"default_dashboard_window": window, "timezone": None}
        if coverage is not None:
            db.getGenreCoverage.return_value = coverage
        if distribution is not None:
            db.getGenreDistribution.return_value = distribution
        db.getGenreTrends.return_value = {"buckets": ["2026-01"], "series": [{"name": "rock", "data": [1]}]}
        db.getGenreStats.return_value = {"plays": 10, "listenMs": 60000, "firstPlayedTs": None, "sharePercent": 25.0}
        db.getTopArtistsForGenre.return_value = []
        db.getTopTracksForGenre.return_value = []
        db.getGenreHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenreArtistCounts.return_value = {"rock": 12, "jazz": 4}
        return db

    def _get(self, dash, db, query=""):
        """The page shell (no ajax)."""
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/genres{query}")

    def _getData(self, dash, db, query=""):
        """The ajax JSON payload (full, or scope=detail when the query sets it)."""
        client = dash.app.test_client()
        sep = "&" if query else "?"
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/genres{query}{sep}ajax=true")

    def test_locked_shell_shows_progress_and_defers_data(self):
        dash = self._makeApp()
        db = self._makeDb()   #< getGenreCoverage is a bare MagicMock -> sanitizes to zeros
        resp = self._get(dash, db)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Genre insights unlock", resp.data)
        db.getGenreDistribution.assert_not_called()
        db.getGenreTrends.assert_not_called()

    def test_locked_at_exact_threshold(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(50, 50, 50))
        resp = self._get(dash, db)
        self.assertIn(b"Genre insights unlock", resp.data)
        db.getGenreDistribution.assert_not_called()

    def test_unlock_gate_uses_all_time_coverage_not_the_selected_window(self):
        """A narrow window must not hide the page: the gate is evaluated
        all-time (startDate/endDate both None), only the displayed data below
        is scoped to the window."""
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1}, window="day")
        self._get(dash, db)
        _, coverageKwargs = db.getGenreCoverage.call_args
        self.assertIsNone(coverageKwargs["startDate"])
        self.assertIsNone(coverageKwargs["endDate"])

    def test_default_time_window_setting_selects_the_filter_option(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1}, window="week")
        resp = self._get(dash, db)
        self.assertIn(b'<option value="week" selected>Last Week</option>', resp.data)

    def test_shell_renders_overview_canvases_and_defers_data(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 120})
        resp = self._get(dash, db)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'id="genreDistChart"', resp.data)
        self.assertIn(b'id="genreMixChart"', resp.data)
        self.assertIn(b'id="genreChipRow"', resp.data)
        db.getGenreDistribution.assert_not_called()

    def test_shell_has_auto_trend_buckets_control(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1})
        resp = self._get(dash, db)
        self.assertIn(b"Trend buckets:", resp.data)
        self.assertIn(b'<option value="" selected>Auto</option>', resp.data)

    def test_trend_buckets_control_hidden_on_single_day_windows(self):
        # Mirrors charts.html: single-day views bucket by hour, so the control
        # would be a no-op - it's hidden, not just ignored.
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1}, window="day")
        resp = self._get(dash, db)
        self.assertIn(b'id="groupByContainer" style="display: none;"', resp.data)

    def test_explicit_groupby_scopes_every_trend_query(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 120, "indie": 80}, window="month")
        self._getData(dash, db, query="?groupBy=week")
        for call in db.getGenreTrends.call_args_list:   #< the mix trend AND the drill-down trend
            self.assertEqual(call.kwargs.get("groupBy"), "week")

    def test_auto_groupby_resolves_from_the_play_range_on_all_time(self):
        import datetime
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1}, window="all time")
        db.tz = datetime.timezone.utc
        longSpan = (3 * 365) * 86400.0
        with patch.object(dash.repo, "getPlayTimeRange", return_value=(0.0, longSpan)):
            self._getData(dash, db)
        self.assertEqual(db.getGenreTrends.call_args.kwargs.get("groupBy"), "month")

    def test_auto_groupby_short_window_is_day(self):
        # The reported bug: a sub-month window month-bucketed into <=2 points.
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1}, window="week")
        self._getData(dash, db)
        self.assertEqual(db.getGenreTrends.call_args.kwargs.get("groupBy"), "day")

    def test_single_day_view_uses_hour_buckets(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1})
        self._getData(dash, db, query="?interval=day")
        self.assertEqual(db.getGenreTrends.call_args.kwargs.get("groupBy"), "hour")

    def test_ajax_full_payload_selects_top_genre_and_scopes_data(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 120, "indie": 80, "jazz": 40}, window="month")
        resp = self._getData(dash, db)
        payload = resp.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["genre"], "rock")
        self.assertIn("chipsHtml", payload)
        self.assertIn("distributionPairs", payload)
        # First distribution genre (rock) is the default drill-down selection.
        selectedTrendCall = db.getGenreTrends.call_args_list[-1]
        self.assertEqual(selectedTrendCall.args[0], ["rock"])
        # A non-all-time window scopes the distribution query.
        _, distKwargs = db.getGenreDistribution.call_args
        self.assertIsNotNone(distKwargs["startDate"])

    def test_ajax_detail_scoped_heatmap_and_partial(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 120})
        resp = self._getData(dash, db)
        payload = resp.get_json()
        self.assertIn("genreClockChart", payload["detailHtml"])
        self.assertIn("Listening Clock", payload["detailHtml"])
        # The per-genre heatmap is fetched for the selected genre.
        self.assertEqual(db.getGenreHourOfDayHeatmap.call_args.args[0], "rock")

    def test_ajax_full_payload_ships_breadth_pairs(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 120, "jazz": 40})
        shell = self._get(dash, db)
        # Genre Share legend + companion breadth chart live in the shell.
        self.assertIn(b'id="genreShareLegend"', shell.data)
        self.assertIn(b'id="genreBreadthChart"', shell.data)
        self.assertIn(b'Artists per Genre', shell.data)

        payload = self._getData(dash, db).get_json()
        db.getGenreArtistCounts.assert_called_with(["rock", "jazz"])
        # Breadth ships as [label, value] pairs, ranked most-artists-first.
        self.assertIn(["rock", 12], payload["breadthPairs"])

    def test_ajax_detail_scope_returns_only_the_detail(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 120, "jazz": 40})
        resp = self._getData(dash, db, query="?genre=jazz&scope=detail")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        payload = resp.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["genre"], "jazz")
        self.assertIn("genreClockChart", payload["detailHtml"])
        self.assertIn("selectedTrend", payload)
        self.assertIn("clock", payload)
        # scope=detail is just the partial, not the whole payload.
        self.assertNotIn("distributionPairs", payload)
        self.assertNotIn("genreDistChart", payload["detailHtml"])

    def test_ajax_when_locked_returns_not_ok(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(10, 10, 10))
        resp = self._getData(dash, db, query="?genre=rock")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": False})

    def test_genre_query_override(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 120, "indie": 80, "jazz": 40})
        resp = self._getData(dash, db, query="?genre=jazz")
        self.assertEqual(resp.status_code, 200)
        selectedTrendCall = db.getGenreTrends.call_args_list[-1]
        self.assertEqual(selectedTrendCall.args[0], ["jazz"])

    def test_unknown_genre_query_falls_back_to_top(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 120, "indie": 80})
        resp = self._getData(dash, db, query="?genre=doesnotexist")
        self.assertEqual(resp.status_code, 200)
        selectedTrendCall = db.getGenreTrends.call_args_list[-1]
        self.assertEqual(selectedTrendCall.args[0], ["rock"])

    def test_mix_trend_series_capped(self):
        dash = self._makeApp()
        manyGenres = {f"g{i}": 100 - i for i in range(GENRE_MIX_TREND_TOP_N + 4)}
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution=manyGenres)
        resp = self._getData(dash, db)
        self.assertEqual(resp.status_code, 200)
        # First getGenreTrends call is the mix-over-time overview chart.
        mixCall = db.getGenreTrends.call_args_list[0]
        self.assertLessEqual(len(mixCall.args[0]), GENRE_MIX_TREND_TOP_N)

    def test_nav_link_present_when_enabled(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1})
        resp = self._get(dash, db)
        self.assertIn(b'>Genres</a>', resp.data)

    def test_disabled_hides_nav_link_and_content(self):
        dash = self._makeApp()
        dash.repo.setLastfmGenreBackfillEnabled(False)
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1})
        resp = self._get(dash, db)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'>Genres</a>', resp.data)
        self.assertNotIn(b'id="genreDistChart"', resp.data)
        db.getGenreCoverage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
