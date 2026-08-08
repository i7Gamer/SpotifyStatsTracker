"""The dedicated /genres page: a two-phase load (shell GET + an htmx fragment
request), a time-period filter defaulting to the profile window, the all-time
unlock gate, default genre selection, ?genre= override with fallback, the
chip-click detail swap, nav-link visibility tied to the Last.fm kill switch, and
the mix-over-time series cap.

This is the page's CONTENT: which queries run, scoped how, and what ends up
rendered. The transport underneath it - the HX-Request marker, the HX-Target
fragment split, the JSON data islands, the shell's htmx wiring - is pinned
separately by tests/test_genres_htmx.py."""
import json
import re
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp, GENRE_MIX_TREND_TOP_N  # noqa: F401
from _app_factory import AppTestCase
from routes.genres import GENRE_EXPLORE_ID

#< what htmx puts on every request it makes; the fragment branch keys on it
HX_HEADERS = {"HX-Request": "true"}
#< ...plus the region being replaced, which is what picks the drill-down-only
#  fragment over the full one
HX_EXPLORE_HEADERS = {"HX-Request": "true", "HX-Target": GENRE_EXPLORE_ID}


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

    def _get(self, dash, db, query="", headers=None):
        """The page shell (a plain browser GET)."""
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/genres{query}", headers=headers or {})

    def _getData(self, dash, db, query="", headers=None):
        """The htmx fragment - the full data zone, or the drill-down alone when
        the caller passes HX_EXPLORE_HEADERS."""
        return self._get(dash, db, query, headers=headers or HX_HEADERS)

    def _island(self, resp, elementId):
        """The chart datasets the fragment carries for genres.js. These are what
        the old JSON payload's dataset keys became."""
        body = resp.get_data(as_text=True)
        match = re.search(r'<script type="application/json" id="%s">(.*?)</script>' % elementId,
                          body, re.S)
        self.assertIsNotNone(match, f"no {elementId} data island in the fragment")
        return json.loads(match.group(1))

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

    def test_an_empty_interval_resolves_to_the_saved_window(self):
        """`?interval=` is PRESENT and empty, so the .get() default never fires
        and "" reaches _getValidInterval, which accepts it. _getDateRange
        coerces "" to the default for the DATA, but the All Time option above
        only matches the "all time" spelling - so "" selected nothing and the
        browser displayed the first option, Today, over week-scoped numbers.
        See the same test on /charts, and the `or` the dashboard route carries."""
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1}, window="week")
        resp = self._get(dash, db, query="?interval=")
        self.assertIn(b'<option value="week" selected>Last Week</option>', resp.data)

    def test_shell_renders_an_empty_swap_target_and_defers_data(self):
        # The canvases used to live in the shell and be filled by JS. They now
        # arrive with the fragment, so the shell is the filter form plus the
        # placeholder that asks for it - and still runs no per-range query.
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 120})
        resp = self._get(dash, db)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'id="genresResults"', resp.data)
        self.assertNotIn(b'id="genreDistChart"', resp.data)
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

    def test_full_fragment_selects_top_genre_and_scopes_data(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 120, "indie": 80, "jazz": 40}, window="month")
        resp = self._getData(dash, db)
        self.assertEqual(resp.status_code, 200)
        self.assertRegex(resp.get_data(as_text=True),
                         r'class="genre-chip selected"[^>]*data-genre="rock"')
        self.assertIn("distributionPairs", self._island(resp, "genres-overview-data"))
        # First distribution genre (rock) is the default drill-down selection.
        selectedTrendCall = db.getGenreTrends.call_args_list[-1]
        self.assertEqual(selectedTrendCall.args[0], ["rock"])
        # A non-all-time window scopes the distribution query.
        _, distKwargs = db.getGenreDistribution.call_args
        self.assertIsNotNone(distKwargs["startDate"])

    def test_fragment_carries_the_scoped_heatmap_and_the_detail_partial(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 120})
        resp = self._getData(dash, db)
        body = resp.get_data(as_text=True)
        self.assertIn("genreClockChart", body)
        self.assertIn("Listening Clock", body)
        # The per-genre heatmap is fetched for the selected genre.
        self.assertEqual(db.getGenreHourOfDayHeatmap.call_args.args[0], "rock")

    def test_full_fragment_ships_the_overview_charts_and_their_breadth_pairs(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 120, "jazz": 40})
        resp = self._getData(dash, db)
        # Genre Share legend + companion breadth chart ride with the fragment.
        body = resp.get_data(as_text=True)
        self.assertIn('id="genreShareLegend"', body)
        self.assertIn('id="genreBreadthChart"', body)
        self.assertIn("Artists per Genre", body)

        db.getGenreArtistCounts.assert_called_with(["rock", "jazz"])
        # Breadth ships as [label, value] pairs, ranked most-artists-first.
        self.assertIn(["rock", 12], self._island(resp, "genres-overview-data")["breadthPairs"])

    def test_a_chip_swap_returns_only_the_drill_down(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 120, "jazz": 40})
        resp = self._getData(dash, db, query="?genre=jazz", headers=HX_EXPLORE_HEADERS)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertRegex(body, r'class="genre-chip selected"[^>]*data-genre="jazz"')
        self.assertIn("genreClockChart", body)
        self.assertEqual(self._island(resp, "genres-detail-data")["genre"], "jazz")
        # The drill-down alone: no overview markup, and none of its queries.
        self.assertNotIn("genreDistChart", body)
        self.assertNotIn('id="genres-overview-data"', body)
        db.getGenreArtistCounts.assert_not_called()

    def test_a_fragment_request_when_locked_swaps_nothing(self):
        # 204 is htmx's "no swap": the gate is all-time and stable, so this only
        # happens if it flipped under a live page, and leaving the placeholders
        # up beats replacing them with something that could ask again.
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(10, 10, 10))
        resp = self._getData(dash, db, query="?genre=rock")
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp.get_data(as_text=True), "")

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

    def test_detail_listen_time_drops_seconds_above_the_threshold(self):
        """The stat strip's Listen time follows the dashboard's Total listen
        time: past LISTEN_TIME_HIDE_SECONDS_ABOVE_HOURS the seconds are noise."""
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 120})
        longMs = (12 * 3600 + 3 * 60 + 41) * 1000
        db.getGenreStats.return_value = {"plays": 10, "listenMs": longMs,
                                         "firstPlayedTs": None, "sharePercent": 25.0}
        body = self._getData(dash, db).get_data(as_text=True)
        self.assertIn("12h 3m", body)
        self.assertNotIn("12h 3m 41s", body)

    def test_detail_listen_time_keeps_seconds_below_the_threshold(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 120})
        shortMs = (9 * 3600 + 59 * 60 + 59) * 1000
        db.getGenreStats.return_value = {"plays": 10, "listenMs": shortMs,
                                         "firstPlayedTs": None, "sharePercent": 25.0}
        self.assertIn("9h 59m 59s", self._getData(dash, db).get_data(as_text=True))

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
        #< no swap target and no htmx wiring, so nothing ever asks for the data
        self.assertNotIn(b'id="genresResults"', resp.data)
        db.getGenreCoverage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
