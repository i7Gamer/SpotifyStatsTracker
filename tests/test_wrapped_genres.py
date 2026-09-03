"""The live-computed Top Genres card on /wrapped: gated by year-scoped
coverage, present in the htmx swap fragment, and never read from the
user_wrapped cache (backfill progresses continuously and the admin toggle would
stale it)."""
import datetime
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as appModule
from app import SpotifyDashboardApp, WRAPPED_TOP_GENRES_LIMIT
from _app_factory import AppTestCase
import Database.utils as utilsModule
from test_charts_genres import coverageDict
from conftest import wrappedCachedRow


def _ts(year, month=6, day=1, hour=12):
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc).timestamp()


class WrappedGenresTestBase(AppTestCase):
    def setUp(self):
        tzPatcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        tzPatcher.start()
        self.addCleanup(tzPatcher.stop)

        nowPatcher = patch.object(appModule, "now",
                                  return_value=datetime.datetime(2026, 7, 11, tzinfo=datetime.timezone.utc))
        nowPatcher.start()
        self.addCleanup(nowPatcher.stop)

    def _makeDb(self, earliestPlayedAt=None, coverage=None, distribution=None, workerStatus=None):
        db = MagicMock()
        db.getEntriesFromOld.return_value = (
            [{"id": "x", "playedAt": earliestPlayedAt, "timePlayed": 1}] if earliestPlayedAt is not None else []
        )
        # _buildWrappedContext's only path since R6 (2026-09-02) reads
        # everything from the cache row - db.getTopSongs etc are never
        # called by it anymore.
        db.repo.getCachedWrapped.return_value = wrappedCachedRow()
        if coverage is not None:
            db.getGenreCoverage.return_value = coverage
        if distribution is not None:
            db.getGenreDistribution.return_value = distribution
        if workerStatus is not None:
            db.getLastfmWorkerStatus.return_value = workerStatus
        return db

    def _getWrapped(self, dash, db, query="", headers=None):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/wrapped{query}", headers=headers or {})

    def _getWrappedFragment(self, dash, db, query=""):
        """The htmx swap: an HTML fragment, not the old ?ajax=true JSON."""
        return self._getWrapped(dash, db, query, headers={"HX-Request": "true"})


class TestWrappedGenreCard(WrappedGenresTestBase):
    def test_unstubbed_db_renders_the_locked_card(self):
        dash = self._makeApp()
        db = self._makeDb()   #< genre methods left as bare MagicMocks

        resp = self._getWrapped(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Top Genres of 2026", resp.data)
        self.assertIn(b"Genre insights unlock", resp.data)
        db.getGenreDistribution.assert_not_called()

    def test_locked_card_pitches_a_key_for_a_keyless_user(self):
        """2026-09-02 review, UT-12 follow-up: the locked card used to pass
        the admin's instance-wide lastfmEnabled toggle as _genre_progress's
        lastfmConfigured, so a keyless user (an unstubbed
        getLastfmWorkerStatus, like the regression guard above) with zero
        year coverage saw "No plays in this period yet." instead of the
        "add a key" pitch."""
        dash = self._makeApp()
        db = self._makeDb()   #< genre AND worker-status methods left as bare MagicMocks

        resp = self._getWrapped(dash, db)

        self.assertIn(b"Add a Last.fm API key", resp.data)
        self.assertNotIn(b"No plays in this period yet.", resp.data)

    def test_locked_card_shows_no_plays_for_a_keyed_user(self):
        dash = self._makeApp()
        db = self._makeDb(workerStatus={"configured": True, "running": True})

        resp = self._getWrapped(dash, db)

        self.assertIn(b"No plays in this period yet.", resp.data)
        self.assertNotIn(b"Add a Last.fm API key", resp.data)

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

    def test_the_htmx_fragment_carries_the_genre_card(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"dream pop": 42})

        body = self._getWrappedFragment(dash, db).get_data(as_text=True)

        self.assertIn('id="wrappedGenresCard"', body)
        self.assertIn("dream pop", body)

    def test_every_swap_recomputes_the_genre_card(self):
        """The ?ajax=true layer had three update shapes (all/chart/lists) and
        only the widest one computed genres, so a trend-bucket tweak skipped
        the two year-wide aggregations. htmx swaps ONE fragment - the whole
        recap - so they run on every filter change now. That is a deliberate
        trade: one response shape instead of three, at the cost of a coverage
        and a distribution query per click. If it ever shows up on a large
        library, the lever is caching coverage per (user, year), not
        reintroducing partial responses."""
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1})

        self._getWrappedFragment(dash, db, query="?groupBy=month")

        db.getGenreCoverage.assert_called_once()
        db.getGenreDistribution.assert_called_once()

    def test_cached_wrapped_path_still_computes_genres_live(self):
        """The genre card must never come from the user_wrapped cache - even
        when the rest of the page renders from it. _makeDb's db.repo.
        getCachedWrapped stub already puts every test in this file on the
        cache path (see R6, 2026-09-02) - this test just names that fact."""
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"post rock": 7})

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

    def test_disabled_htmx_fragment_has_no_genre_section(self):
        dash = self._makeApp()
        dash.repo.setLastfmGenreBackfillEnabled(False)
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1})

        body = self._getWrappedFragment(dash, db).get_data(as_text=True)

        #< the card's container still swaps in (it is part of the recap), but
        #  _wrapped_genres.html renders nothing into it
        self.assertIn('id="wrappedGenresCard"', body)
        self.assertNotIn("Top Genres", body)


if __name__ == "__main__":
    unittest.main()
