"""The Top Genres chart on /charts and the genre unlock gate (play-weighted
coverage: overall mean strictly above 50%, every category at least 30%)."""
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import (
    SpotifyDashboardApp, sanitizeGenreCoverage, genreGatePasses, emptyGenreCoverage,
    GENRE_GATE_OVERALL_MIN_PERCENT, GENRE_GATE_CATEGORY_MIN_PERCENT, CHART_TOP_GENRES_LIMIT,
    resolveGenresForTrack, resolveGenresForAlbum, resolveGenresForArtist,
)
from _app_factory import AppTestCase


def coverageDict(song, album, artist, total=1000):
    """A Database.getGenreCoverage-shaped dict from three percentages,
    overall = mean of the three (matching the real implementation)."""
    def category(percent):
        return {"covered": int(total * percent / 100), "total": total, "percent": percent}
    return {
        "song": category(song),
        "album": category(album),
        "artist": category(artist),
        "overall": {"percent": round((song + album + artist) / 3, 1)},
    }


class GateHelperTestCase(unittest.TestCase):
    def test_passes_only_above_overall_and_at_category_minimums(self):
        self.assertTrue(genreGatePasses(coverageDict(60, 60, 60)))
        self.assertTrue(genreGatePasses(coverageDict(30, 90, 90)))    #< category exactly 30 passes (>=)
        self.assertFalse(genreGatePasses(coverageDict(50, 50, 50)))   #< overall exactly 50 fails (strict >)
        self.assertFalse(genreGatePasses(coverageDict(29, 90, 90)))   #< one category below 30 fails
        self.assertFalse(genreGatePasses(coverageDict(40, 40, 40)))   #< overall too low
        self.assertFalse(genreGatePasses(emptyGenreCoverage()))

    def test_sanitize_passes_well_formed_coverage_through(self):
        coverage = coverageDict(75, 50, 75)
        self.assertEqual(sanitizeGenreCoverage(coverage), coverage)

    def test_sanitize_zeroes_anything_malformed(self):
        empty = emptyGenreCoverage()
        self.assertEqual(sanitizeGenreCoverage(None), empty)
        self.assertEqual(sanitizeGenreCoverage(MagicMock()), empty)
        self.assertEqual(sanitizeGenreCoverage({"song": "nope"}), empty)
        self.assertEqual(sanitizeGenreCoverage({
            "song": {"covered": MagicMock(), "total": 1, "percent": 1.0},
            "album": {"covered": 0, "total": 1, "percent": 0.0},
            "artist": {"covered": 0, "total": 1, "percent": 0.0},
            "overall": {"percent": 0.0},
        }), empty)

    def test_sanitize_passes_own_percent_through_only_when_present(self):
        """ownPercent is optional (older callers/stubs don't produce it) but
        validated like every other field when it is there."""
        coverage = coverageDict(75, 50, 75)
        coverage["song"]["ownPercent"] = 10.0
        self.assertEqual(sanitizeGenreCoverage(coverage), coverage)
        self.assertNotIn("ownPercent", sanitizeGenreCoverage(coverageDict(75, 50, 75))["song"])

        malformed = coverageDict(75, 50, 75)
        malformed["album"]["ownPercent"] = MagicMock()
        self.assertEqual(sanitizeGenreCoverage(malformed), emptyGenreCoverage())

    def test_thresholds_are_the_agreed_values(self):
        self.assertEqual(GENRE_GATE_OVERALL_MIN_PERCENT, 50)
        self.assertEqual(GENRE_GATE_CATEGORY_MIN_PERCENT, 30)


class ResolveGenresForEntityTestCase(unittest.TestCase):
    """resolveGenresFor{Track,Album,Artist} - the same never-let-a-genre-
    lookup-break-a-page degradation contract as resolveGenreCoverage/
    resolveGenreDistribution, at per-item scope: a stubbed test db (or a
    real lookup failure) must degrade to [] rather than raise or leak a
    non-list value into a card's genre badge."""

    def _cases(self):
        return (
            (resolveGenresForTrack, "getGenresForTrack"),
            (resolveGenresForAlbum, "getGenresForAlbum"),
            (resolveGenresForArtist, "getGenresForArtist"),
        )

    def test_well_formed_list_passes_through(self):
        for resolver, dbMethod in self._cases():
            with self.subTest(resolver=resolver.__name__):
                db = MagicMock()
                getattr(db, dbMethod).return_value = ["rock", "dream pop"]
                self.assertEqual(resolver(db, "id1"), ["rock", "dream pop"])

    def test_exception_degrades_to_empty_list(self):
        for resolver, dbMethod in self._cases():
            with self.subTest(resolver=resolver.__name__):
                db = MagicMock()
                getattr(db, dbMethod).side_effect = RuntimeError("boom")
                self.assertEqual(resolver(db, "id1"), [])

    def test_unstubbed_magicmock_return_degrades_to_empty_list(self):
        """An un-configured MagicMock method returns another MagicMock, not
        a list - the exact shape every route test's bare `db = MagicMock()`
        produces when it doesn't set up genre methods."""
        for resolver, _ in self._cases():
            with self.subTest(resolver=resolver.__name__):
                db = MagicMock()
                self.assertEqual(resolver(db, "id1"), [])


class ChartsGenresTestCase(AppTestCase):
    def _makeDb(self, coverage=None, distribution=None):
        db = MagicMock()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getArtistTrend.return_value = {"buckets": [], "series": []}
        db.getExplicitRatio.return_value = {"explicit": 0, "clean": 0}
        db.getReleaseDecadeDistribution.return_value = {}
        db.getCompletionStats.return_value = {"skips": 0, "completes": 0, "partials": 0}
        if coverage is not None:
            db.getGenreCoverage.return_value = coverage
        if distribution is not None:
            db.getGenreDistribution.return_value = distribution
        return db

    def _get(self, dash, db, query=""):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/charts{query}")

    def test_unstubbed_magicmock_db_still_renders_the_locked_state(self):
        """Regression guard for every pre-genre charts test: a db whose genre
        methods return MagicMocks must sanitize to zeros, not crash."""
        dash = self._makeApp()
        db = self._makeDb()   #< getGenreCoverage left as a bare MagicMock

        resp = self._get(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Genre insights unlock", resp.data)
        self.assertNotIn(b'id="genreChart"', resp.data)
        db.getGenreDistribution.assert_not_called()

    def test_locked_when_overall_is_exactly_at_the_threshold(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(50, 50, 50))
        resp = self._get(dash, db)
        self.assertIn(b"Genre insights unlock", resp.data)
        db.getGenreDistribution.assert_not_called()

    def test_locked_when_one_category_is_below_its_minimum(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(29, 90, 90))   #< overall ~69.7 passes, songs don't
        resp = self._get(dash, db)
        self.assertIn(b"Genre insights unlock", resp.data)
        db.getGenreDistribution.assert_not_called()

    def test_locked_state_shows_the_per_category_progress(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(29, 90, 45))
        resp = self._get(dash, db)
        self.assertIn(b"29", resp.data)
        self.assertIn(b"90", resp.data)
        self.assertIn(b"45", resp.data)

    def test_unlocked_renders_the_genre_chart_with_the_distribution(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 120, "indie rock": 80})

        resp = self._get(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'id="genreChart"', resp.data)
        self.assertIn(b"indie rock", resp.data)
        self.assertNotIn(b"Genre insights unlock", resp.data)

        _, coverageKwargs = db.getGenreCoverage.call_args
        self.assertIn("startDate", coverageKwargs)
        self.assertIn("endDate", coverageKwargs)
        _, distributionKwargs = db.getGenreDistribution.call_args
        self.assertEqual(distributionKwargs["limit"], CHART_TOP_GENRES_LIMIT)
        self.assertEqual(distributionKwargs["startDate"], coverageKwargs["startDate"])
        self.assertEqual(distributionKwargs["endDate"], coverageKwargs["endDate"])

    def test_unlocked_chart_carries_the_lastfm_attribution(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1})
        resp = self._get(dash, db)
        self.assertIn(b"Last.fm", resp.data)

    def test_unlocked_chart_displays_genres_in_ascending_play_count_order(self):
        """getGenreDistribution still returns most-played-first (unchanged
        query/selection - Wrapped and Compare both rely on that order) but
        chartsPage() reverses its own copy so the bar chart reads smallest
        first."""
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 120, "indie rock": 80, "jazz": 40})

        resp = self._get(dash, db)

        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        jazzPos = body.index('"jazz"')
        indieRockPos = body.index('"indie rock"')
        rockPos = body.index('"rock"')
        self.assertLess(jazzPos, indieRockPos)
        self.assertLess(indieRockPos, rockPos)

    def test_coverage_errors_degrade_to_the_locked_state(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.getGenreCoverage.side_effect = RuntimeError("db exploded")
        resp = self._get(dash, db)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Genre insights unlock", resp.data)

    def test_disabled_hides_the_whole_section_without_querying_coverage(self):
        """The admin's instance-wide kill switch hides the Top Genres section
        entirely - neither the chart nor the locked-progress fallback, which
        would otherwise misleadingly invite adding a Last.fm key for a
        feature the admin turned off."""
        dash = self._makeApp()
        dash.repo.setLastfmGenreBackfillEnabled(False)
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1})

        resp = self._get(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"Top Genres", resp.data)
        self.assertNotIn(b"Genre insights unlock", resp.data)
        self.assertNotIn(b'id="genreChart"', resp.data)
        db.getGenreCoverage.assert_not_called()
        db.getGenreDistribution.assert_not_called()


if __name__ == "__main__":
    unittest.main()
