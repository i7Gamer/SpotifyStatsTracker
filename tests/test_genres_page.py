"""The dedicated /genres page: the same coverage unlock gate as Charts, default
genre selection, ?genre= override with fallback, nav-link visibility tied to the
Last.fm kill switch, and the mix-over-time series cap."""
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
    def _makeDb(self, coverage=None, distribution=None):
        db = MagicMock()
        if coverage is not None:
            db.getGenreCoverage.return_value = coverage
        if distribution is not None:
            db.getGenreDistribution.return_value = distribution
        db.getGenreTrends.return_value = {"buckets": ["2026-01"], "series": [{"name": "rock", "data": [1]}]}
        db.getGenreStats.return_value = {"plays": 10, "listenMs": 60000, "firstPlayedTs": None, "sharePercent": 25.0}
        db.getTopArtistsForGenre.return_value = []
        db.getTopTracksForGenre.return_value = []
        return db

    def _get(self, dash, db, query=""):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/genres{query}")

    def test_locked_state_when_coverage_unstubbed(self):
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

    def test_unlocked_default_selection_is_top_genre(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 120, "indie": 80, "jazz": 40})
        resp = self._get(dash, db)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'id="genreDistChart"', resp.data)
        self.assertIn(b'id="genreMixChart"', resp.data)
        # First distribution genre (rock) is the default drill-down selection.
        selectedTrendCall = db.getGenreTrends.call_args_list[-1]
        self.assertEqual(selectedTrendCall.args[0], ["rock"])

    def test_genre_query_override(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 120, "indie": 80, "jazz": 40})
        resp = self._get(dash, db, query="?genre=jazz")
        self.assertEqual(resp.status_code, 200)
        selectedTrendCall = db.getGenreTrends.call_args_list[-1]
        self.assertEqual(selectedTrendCall.args[0], ["jazz"])

    def test_unknown_genre_query_falls_back_to_top(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 120, "indie": 80})
        resp = self._get(dash, db, query="?genre=doesnotexist")
        self.assertEqual(resp.status_code, 200)
        selectedTrendCall = db.getGenreTrends.call_args_list[-1]
        self.assertEqual(selectedTrendCall.args[0], ["rock"])

    def test_mix_trend_series_capped(self):
        dash = self._makeApp()
        manyGenres = {f"g{i}": 100 - i for i in range(GENRE_MIX_TREND_TOP_N + 4)}
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution=manyGenres)
        resp = self._get(dash, db)
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
