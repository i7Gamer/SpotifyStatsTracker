"""The live-computed Top Genres card on /wrapped: gated by year-scoped
coverage, present in the ajax payload, and never read from the user_wrapped
cache (backfill progresses continuously and the admin toggle would stale it)."""
import datetime
import json
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as appModule
from app import SpotifyDashboardApp, WRAPPED_TOP_GENRES_LIMIT
import Database.utils as utilsModule
from test_charts_genres import coverageDict


def _ts(year, month=6, day=1, hour=12):
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc).timestamp()


def _cachedWrappedRow():
    """A user_wrapped row as getCachedWrapped returns it (all JSON columns)."""
    return {
        "total_plays": 10, "total_ms": 10000, "longest_streak": 2,
        "peak_day": "2026-01-05", "peak_plays": 5,
        "unique_songs": 3, "unique_artists": 2,
        "discovered_songs": 1, "discovered_artists": 1,
        "time_series_day": "[]", "time_series_week": "[]", "time_series_month": "[]",
        "top_songs": "[]", "top_artists": "[]", "top_albums": "[]",
        "discovered_songs_list": "[]", "discovered_artists_list": "[]",
        "discovered_albums_list": "[]",
    }


class WrappedGenresTestBase(unittest.TestCase):
    def setUp(self):
        tzPatcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        tzPatcher.start()
        self.addCleanup(tzPatcher.stop)

        nowPatcher = patch.object(appModule, "now",
                                  return_value=datetime.datetime(2026, 7, 11, tzinfo=datetime.timezone.utc))
        nowPatcher.start()
        self.addCleanup(nowPatcher.stop)

    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _makeDb(self, earliestPlayedAt=None, coverage=None, distribution=None):
        db = MagicMock()
        db.getEntriesFromOld.return_value = (
            [{"id": "x", "playedAt": earliestPlayedAt, "timePlayed": 1}] if earliestPlayedAt is not None else []
        )
        db.getTopSongs.return_value = []
        db.getTopArtists.return_value = []
        db.getTopAlbums.return_value = []
        db.getPlayTotals.return_value = (0, 0)
        db.getSongsStats.return_value = []
        db.getArtistsStats.return_value = []
        db.getAlbumsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getLongestStreak.return_value = 0
        db.getPeakListeningTime.return_value = None
        db.getSongsCount.return_value = 0
        db.getArtistsCount.return_value = 0
        db.getDiscoveredSongsCount.return_value = 0
        db.getDiscoveredArtistsCount.return_value = 0
        if coverage is not None:
            db.getGenreCoverage.return_value = coverage
        if distribution is not None:
            db.getGenreDistribution.return_value = distribution
        return db

    def _getWrapped(self, dash, db, query=""):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/wrapped{query}")


class TestWrappedGenreCard(WrappedGenresTestBase):
    def test_unstubbed_db_renders_the_locked_card(self):
        dash = self._makeApp()
        db = self._makeDb()   #< genre methods left as bare MagicMocks

        resp = self._getWrapped(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Top Genres of 2026", resp.data)
        self.assertIn(b"Genre insights unlock", resp.data)
        db.getGenreDistribution.assert_not_called()

    def test_unlocked_card_lists_the_genres(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 300, "shoegaze": 120})

        resp = self._getWrapped(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"rock", resp.data)
        self.assertIn(b"shoegaze", resp.data)
        self.assertNotIn(b"Genre insights unlock", resp.data)
        self.assertIn(b"Last.fm", resp.data)

        _, kwargs = db.getGenreDistribution.call_args
        self.assertEqual(kwargs["limit"], WRAPPED_TOP_GENRES_LIMIT)

    def test_genre_range_is_the_selected_year(self):
        dash = self._makeApp()
        db = self._makeDb(earliestPlayedAt=_ts(2024),
                          coverage=coverageDict(80, 60, 90), distribution={"rock": 1})

        self._getWrapped(dash, db, query="?year=2025")

        _, kwargs = db.getGenreCoverage.call_args
        self.assertEqual(kwargs["startDate"].year, 2025)
        self.assertEqual(kwargs["startDate"].month, 1)
        self.assertEqual(kwargs["endDate"].year, 2026)
        _, distKwargs = db.getGenreDistribution.call_args
        self.assertEqual(distKwargs["startDate"].year, 2025)

    def test_ajax_all_payload_carries_the_genre_card_html(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"dream pop": 42})

        resp = self._getWrapped(dash, db, query="?ajax=true&type=all")

        payload = json.loads(resp.data)
        self.assertIn("topGenresHtml", payload)
        self.assertIn("dream pop", payload["topGenresHtml"])

    def test_ajax_lists_payload_skips_the_genre_card(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1})
        resp = self._getWrapped(dash, db, query="?ajax=true&type=lists")
        payload = json.loads(resp.data)
        self.assertNotIn("topGenresHtml", payload)

    def test_ajax_chart_and_lists_requests_never_run_the_genre_queries(self):
        """type=chart/type=lists responses don't include the genre card, so
        the (year-wide) coverage and distribution aggregations must not run
        for them - they'd be computed and discarded on every filter click."""
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1})

        self._getWrapped(dash, db, query="?ajax=true&type=chart")
        self._getWrapped(dash, db, query="?ajax=true&type=lists")

        db.getGenreCoverage.assert_not_called()
        db.getGenreDistribution.assert_not_called()

    def test_cached_wrapped_path_still_computes_genres_live(self):
        """The genre card must never come from the user_wrapped cache - even
        when the rest of the page renders from it."""
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"post rock": 7})
        db.repo.getCachedWrapped.return_value = _cachedWrappedRow()

        resp = self._getWrapped(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"post rock", resp.data)
        db.getGenreDistribution.assert_called_once()

    def test_disabled_hides_the_card_without_querying_coverage(self):
        dash = self._makeApp()
        dash.repo.setLastfmGenreBackfillEnabled(False)
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1})

        resp = self._getWrapped(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"Top Genres of 2026", resp.data)
        self.assertNotIn(b"Genre insights unlock", resp.data)
        db.getGenreCoverage.assert_not_called()
        db.getGenreDistribution.assert_not_called()

    def test_disabled_ajax_all_payload_has_no_genre_section(self):
        dash = self._makeApp()
        dash.repo.setLastfmGenreBackfillEnabled(False)
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1})

        resp = self._getWrapped(dash, db, query="?ajax=true&type=all")

        payload = json.loads(resp.data)
        self.assertNotIn("Top Genres", payload["topGenresHtml"])


if __name__ == "__main__":
    unittest.main()
