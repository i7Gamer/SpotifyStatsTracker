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
    resolveGenresForTracks, resolveGenresForAlbums, resolveGenresForArtists,
)
from _app_factory import AppTestCase
from _charts_client import HX_HEADERS, chartData


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


class ResolveGenresForManyTestCase(unittest.TestCase):
    """resolveGenresFor{Tracks,Albums,Artists} - the batched lookups the card
    lists actually use. Same never-let-a-genre-lookup-break-a-page contract,
    one level up: degrade to {} (callers read a missing id as "no genres")."""

    def _cases(self):
        return (
            (resolveGenresForTracks, "getGenresForTracks"),
            (resolveGenresForAlbums, "getGenresForAlbums"),
            (resolveGenresForArtists, "getGenresForArtists"),
        )

    def test_well_formed_mapping_passes_through(self):
        for resolver, dbMethod in self._cases():
            with self.subTest(resolver=resolver.__name__):
                db = MagicMock()
                getattr(db, dbMethod).return_value = {"id1": ["rock"], "id2": []}
                self.assertEqual(resolver(db, ["id1", "id2"]), {"id1": ["rock"], "id2": []})

    def test_exception_degrades_to_empty_mapping(self):
        for resolver, dbMethod in self._cases():
            with self.subTest(resolver=resolver.__name__):
                db = MagicMock()
                getattr(db, dbMethod).side_effect = RuntimeError("boom")
                self.assertEqual(resolver(db, ["id1"]), {})

    def test_unstubbed_magicmock_return_degrades_to_empty_mapping(self):
        for resolver, _ in self._cases():
            with self.subTest(resolver=resolver.__name__):
                db = MagicMock()
                self.assertEqual(resolver(db, ["id1"]), {})

    def test_non_list_values_are_dropped_not_rendered(self):
        """A malformed value must never reach a card's genre badge, even if
        the mapping itself is well formed."""
        for resolver, dbMethod in self._cases():
            with self.subTest(resolver=resolver.__name__):
                db = MagicMock()
                getattr(db, dbMethod).return_value = {"id1": ["rock"], "id2": "pop"}
                self.assertEqual(resolver(db, ["id1", "id2"]), {"id1": ["rock"]})

    def test_empty_id_list_never_touches_the_db(self):
        for resolver, dbMethod in self._cases():
            with self.subTest(resolver=resolver.__name__):
                db = MagicMock()
                self.assertEqual(resolver(db, []), {})
                getattr(db, dbMethod).assert_not_called()


class ChartsGenresTestCase(AppTestCase):
    def _makeDb(self, coverage=None, distribution=None, workerStatus=None):
        db = MagicMock()
        db.getListeningTimeSeries.return_value = []
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
        if coverage is not None:
            db.getGenreCoverage.return_value = coverage
        if distribution is not None:
            db.getGenreDistribution.return_value = distribution
        if workerStatus is not None:
            db.getLastfmWorkerStatus.return_value = workerStatus
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
        """The chart card, as htmx asks for it. The Top Genres section is
        range-scoped, so it is rendered into this fragment (and the distribution
        it draws rides in the fragment's JSON data island)."""
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/charts{query}", headers=HX_HEADERS)

    def test_unstubbed_magicmock_db_still_renders_the_locked_state(self):
        """Regression guard for every pre-genre charts test: a db whose genre
        methods return MagicMocks must sanitize to zeros, not crash."""
        dash = self._makeApp()
        db = self._makeDb()   #< getGenreCoverage left as a bare MagicMock

        resp = self._getData(dash, db)

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Genre insights unlock", body)
        self.assertNotIn('id="genreChart"', body)
        db.getGenreDistribution.assert_not_called()

    def test_locked_when_overall_is_exactly_at_the_threshold(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(50, 50, 50))
        resp = self._getData(dash, db)
        self.assertIn("Genre insights unlock", resp.get_data(as_text=True))
        db.getGenreDistribution.assert_not_called()

    def test_locked_when_one_category_is_below_its_minimum(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(29, 90, 90))   #< overall ~69.7 passes, songs don't
        resp = self._getData(dash, db)
        self.assertIn("Genre insights unlock", resp.get_data(as_text=True))
        db.getGenreDistribution.assert_not_called()

    def test_locked_state_shows_the_per_category_progress(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(29, 90, 45))
        html = self._getData(dash, db).get_data(as_text=True)
        self.assertIn("29", html)
        self.assertIn("90", html)
        self.assertIn("45", html)

    def test_zero_song_plays_in_range_says_no_plays_not_add_a_key(self):
        """2026-09-02 review, UT-12 (+follow-up): a user who already has a
        working Last.fm key and just picked an empty week must see "No
        plays", not the API-key pitch. lastfmEnabled being true (the
        section is hidden entirely otherwise - see the disabled test below)
        is the admin's INSTANCE-WIDE toggle, not proof this particular user
        has a key - that's userHasLastfmKey/genre_worker.configured, stubbed
        here via getLastfmWorkerStatus.

        coverage.song.total=0 means literally zero song plays in the
        selected range (the coverage denominator) - not zero coverage
        percent, which coverageDict's default total=1000 keeps nonzero."""
        dash = self._makeApp()
        coverage = coverageDict(0, 90, 45)
        coverage["song"]["total"] = 0
        coverage["song"]["covered"] = 0
        db = self._makeDb(coverage=coverage, workerStatus={"configured": True, "running": True})

        html = self._getData(dash, db).get_data(as_text=True)

        self.assertIn("No plays in this period yet.", html)
        self.assertNotIn("Add a Last.fm API key", html)

    def test_zero_song_plays_in_range_pitches_a_key_for_a_keyless_user(self):
        """The other half of UT-12's follow-up: a user who has never
        configured a Last.fm key at all (getLastfmWorkerStatus unstubbed,
        like every other test in this class before this fix) must still see
        the "add a key" pitch on a zero-play range, not "No plays in this
        period yet." - that message would be actively wrong for them."""
        dash = self._makeApp()
        coverage = coverageDict(0, 90, 45)
        coverage["song"]["total"] = 0
        coverage["song"]["covered"] = 0
        db = self._makeDb(coverage=coverage)   #< getLastfmWorkerStatus left as a bare MagicMock

        html = self._getData(dash, db).get_data(as_text=True)

        self.assertIn("Add a Last.fm API key", html)
        self.assertNotIn("No plays in this period yet.", html)

    def test_unlocked_renders_the_genre_chart_with_the_distribution(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 120, "indie rock": 80})

        resp = self._getData(dash, db)

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('id="genreChart"', body)
        self.assertNotIn("Genre insights unlock", body)
        self.assertIn("indie rock", [pair[0] for pair in chartData(resp)["genreDistribution"]])

        _, coverageKwargs = db.getGenreCoverage.call_args
        self.assertIn("startDate", coverageKwargs)
        self.assertIn("endDate", coverageKwargs)
        _, distributionKwargs = db.getGenreDistribution.call_args
        self.assertEqual(distributionKwargs["limit"], CHART_TOP_GENRES_LIMIT)
        self.assertEqual(distributionKwargs["startDate"], coverageKwargs["startDate"])
        self.assertEqual(distributionKwargs["endDate"], coverageKwargs["endDate"])

    def test_shell_defers_all_genre_queries(self):
        """The shell must not run any genre query - coverage/distribution are
        resolved only in the fragment, which is also where the whole section
        now lives (it is range-scoped, so it changes with every filter)."""
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1})

        resp = self._get(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"Top Genres", resp.data)
        db.getGenreCoverage.assert_not_called()
        db.getGenreDistribution.assert_not_called()

    def test_unlocked_chart_carries_the_lastfm_attribution(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1})
        resp = self._getData(dash, db)
        self.assertIn("Last.fm", resp.get_data(as_text=True))

    def test_unlocked_chart_leads_with_the_top_genre(self):
        """The chart is called Top Genres, so the top one is the first thing
        read. It used to be reversed to climb toward the biggest bar, which put
        the answer at the bottom; the payload now keeps getGenreDistribution's
        own most-played-first order, the same one Wrapped and Compare render."""
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          distribution={"rock": 120, "indie rock": 80, "jazz": 40})

        resp = self._getData(dash, db)

        self.assertEqual(resp.status_code, 200)
        labels = [pair[0] for pair in chartData(resp)["genreDistribution"]]
        self.assertEqual(labels, ["rock", "indie rock", "jazz"])

    #< the markup of one progress bar's track; counting it counts the bars
    _PROGRESS_BAR_TRACK = "background: rgba(255, 255, 255, 0.08); border-radius: 4px; height: 10px"

    def test_overall_is_text_on_the_own_tags_line_not_a_fourth_bar(self):
        """Overall coverage rides at the end of the own-tags sentence; only the
        three categories get a bar of their own."""
        dash = self._makeApp()
        coverage = coverageDict(29, 90, 45)
        coverage["song"]["ownPercent"] = 12.0
        coverage["album"]["ownPercent"] = 34.0
        html = self._getData(dash, self._makeDb(coverage=coverage)).get_data(as_text=True)

        self.assertEqual(html.count(self._PROGRESS_BAR_TRACK), 3)
        self.assertIn("Overall: <strong>{}%</strong>".format(coverage["overall"]["percent"]), html)
        self.assertLess(html.index("Counting only own"), html.index("Overall:"))
        #< same paragraph: no tag closes between the sentence and the appended text
        self.assertNotIn("</p>", html[html.index("Counting only own"):html.index("Overall:")])

    def test_overall_still_shows_when_the_own_tags_line_is_hidden(self):
        """Without ownPercent there is no own-tags sentence to append to, but
        the overall percentage must not vanish with it."""
        dash = self._makeApp()
        coverage = coverageDict(29, 90, 45)   #< no ownPercent anywhere
        html = self._getData(dash, self._makeDb(coverage=coverage)).get_data(as_text=True)

        self.assertNotIn("Counting only own", html)
        self.assertIn("Overall: <strong>{}%</strong>".format(coverage["overall"]["percent"]), html)

    def test_coverage_errors_degrade_to_the_locked_state(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.getGenreCoverage.side_effect = RuntimeError("db exploded")
        resp = self._getData(dash, db)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Genre insights unlock", resp.get_data(as_text=True))

    def test_disabled_hides_the_whole_section_without_querying_coverage(self):
        """The admin's instance-wide kill switch hides the Top Genres section
        entirely - neither the chart nor the locked-progress fallback, which
        would otherwise misleadingly invite adding a Last.fm key for a
        feature the admin turned off. Checked on both phases, since the section
        could reappear from either: the shell has never carried it, and the
        fragment omits it without touching any genre query."""
        dash = self._makeApp()
        dash.repo.setLastfmGenreBackfillEnabled(False)
        db = self._makeDb(coverage=coverageDict(80, 60, 90), distribution={"rock": 1})

        shell = self._get(dash, db)
        self.assertEqual(shell.status_code, 200)
        self.assertNotIn(b"Top Genres", shell.data)
        self.assertNotIn(b'id="chartsGenreSection"', shell.data)

        data = self._getData(dash, db)
        self.assertNotIn(b"Top Genres", data.data)
        self.assertNotIn(b'id="chartsGenreSection"', data.data)
        self.assertIsNone(chartData(data)["genreDistribution"])
        db.getGenreCoverage.assert_not_called()
        db.getGenreDistribution.assert_not_called()


if __name__ == "__main__":
    unittest.main()
