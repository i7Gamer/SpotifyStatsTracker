"""Query-count regression tests for the hot render paths.

These pin the shape of the SQL a page issues, not its wall time: the failure
mode they exist to catch is a per-item lookup creeping back into a loop that
renders a whole list (the N+1 the batched genre/track lookups replaced). A
plain unit test can't see that - the output is identical either way - so these
count statements against a real connection instead.
"""
import datetime
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from flask import Flask

from conftest import DatabaseTestCase, normalizeTrackForTest
from _app_factory import AppTestCase
import Database.utils as utilsModule
from dashboard.view_models import ViewModelMixin
from services.genre_gate import (
    resolveGenresForTracks, resolveGenresForAlbums, resolveGenresForArtists,
)


def _ts(year, month=6, day=1, hour=12):
    return int(datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc).timestamp())


class _QueryCounter:
    """Counts statements executed on a connection while active."""

    def __init__(self, conn):
        self.conn = conn
        self.statements = []

    def __enter__(self):
        self.conn.set_trace_callback(self.statements.append)
        return self

    def __exit__(self, *exc):
        self.conn.set_trace_callback(None)
        return False

    def matching(self, needle):
        return [s for s in self.statements if needle.lower() in s.lower()]

    def count(self, needle):
        return len(self.matching(needle))


class _Dashboard(ViewModelMixin):
    """The genre-attachment half of SpotifyDashboardApp, without building the
    whole app: _attachGenres only needs the repo and the resolver table."""

    _GENRE_RESOLVERS = {
        "track": resolveGenresForTracks,
        "album": resolveGenresForAlbums,
        "artist": resolveGenresForArtists,
    }

    def __init__(self, repo):
        self.repo = repo


class GenreBadgeQueryCountTestCase(DatabaseTestCase):
    """One rendered list = one genre query, however many cards are in it."""

    ITEM_COUNT = 25

    def setUp(self):
        super().setUp()
        patcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed(self):
        tracks, entries = {}, []
        for index in range(self.ITEM_COUNT):
            trackId = f"t{index}"
            tracks[trackId] = normalizeTrackForTest({
                "id": trackId, "name": f"Song {index}",
                "artists": [{"id": f"ar{index}", "name": f"Artist {index}"}],
                "imageId": f"al{index}",   #< doubles as the album id (see normalizeTrackForTest)
            })
            entries.append({"id": trackId, "playedAt": _ts(2026, 6, 1, 12), "timePlayed": 60000})
        db = self._makeDb(tracks, entries)
        for index in range(self.ITEM_COUNT):
            db.repo.replaceTrackGenres(f"t{index}", ["rock", "pop"])
            db.repo.replaceAlbumGenres(f"al{index}", ["rock"])
            db.repo.replaceArtistGenres(f"ar{index}", ["rock"])
        return db

    def _attach(self, db, kind, idPrefix):
        items = [{"id": f"{idPrefix}{index}"} for index in range(self.ITEM_COUNT)]
        dashboard = _Dashboard(db.repo)
        with _QueryCounter(db.repo._conn()) as counter:
            attached = dashboard._attachGenres(db, items, kind)
        return attached, counter

    def test_track_badges_cost_one_genre_query_for_the_whole_list(self):
        db = self._seed()

        attached, counter = self._attach(db, "track", "t")

        self.assertEqual(counter.count("FROM track_genres"), 1)
        self.assertTrue(all(item["genres"] == ["rock", "pop"] for item in attached))

    def test_album_badges_cost_one_genre_query_for_the_whole_list(self):
        db = self._seed()

        attached, counter = self._attach(db, "album", "al")

        self.assertEqual(counter.count("FROM album_genres"), 1)
        self.assertTrue(all(item["genres"] == ["rock"] for item in attached))

    def test_artist_badges_cost_one_genre_query_for_the_whole_list(self):
        db = self._seed()

        attached, counter = self._attach(db, "artist", "ar")

        self.assertEqual(counter.count("FROM artist_genres"), 1)
        self.assertTrue(all(item["genres"] == ["rock"] for item in attached))

    def test_total_query_count_does_not_grow_with_the_number_of_cards(self):
        """The whole point: the per-item path cost 2 queries per card (the
        genre rows plus a fresh read of the inherited-genres setting), so a
        longer list meant proportionally more round trips."""
        db = self._seed()
        dashboard = _Dashboard(db.repo)

        counts = []
        for itemCount in (1, self.ITEM_COUNT):
            items = [{"id": f"t{index}"} for index in range(itemCount)]
            with _QueryCounter(db.repo._conn()) as counter:
                dashboard._attachGenres(db, items, "track")
            counts.append(len(counter.statements))

        self.assertEqual(counts[0], counts[1], f"query count grew with list size: {counts}")

    def test_batched_and_per_item_lookups_agree(self):
        """The batch path must return exactly what the per-item one did,
        including the inherited-genre filtering."""
        db = self._seed()
        db.repo.replaceTrackGenres("t0", ["rock"], inherited=True)

        for includeInherited in (True, False):
            with self.subTest(includeInherited=includeInherited):
                batched = db.getGenresForTracks([f"t{i}" for i in range(self.ITEM_COUNT)],
                                                 includeInherited=includeInherited)
                perItem = {f"t{i}": db.getGenresForTrack(f"t{i}", includeInherited=includeInherited)
                           for i in range(self.ITEM_COUNT)}
                self.assertEqual(batched, perItem)

    def test_ids_with_no_genres_map_to_empty_lists(self):
        db = self._seed()

        result = db.getGenresForTracks(["t0", "does-not-exist"])

        self.assertEqual(result["does-not-exist"], [])
        self.assertEqual(result["t0"], ["rock", "pop"])

    def test_duplicate_ids_are_queried_once_and_still_resolve(self):
        db = self._seed()

        with _QueryCounter(db.repo._conn()) as counter:
            result = db.getGenresForTracks(["t0", "t0", "t1"])

        self.assertEqual(counter.count("FROM track_genres"), 1)
        self.assertEqual(result["t0"], ["rock", "pop"])
        self.assertEqual(result["t1"], ["rock", "pop"])

    def test_empty_id_list_issues_no_query(self):
        db = self._seed()

        with _QueryCounter(db.repo._conn()) as counter:
            result = db.getGenresForTracks([])

        self.assertEqual(result, {})
        self.assertEqual(counter.count("FROM track_genres"), 0)


class BucketedAggregateReuseTestCase(DatabaseTestCase):
    """The timeline and the listening-clock heatmap are two local-time views of
    one pre-aggregated result set. Rendering both from the same range used to
    run that aggregate twice per request, byte for byte identical."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _db(self):
        tracks = {"t1": normalizeTrackForTest({"id": "t1", "name": "Song", "artists": []})}
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 7, 1, 9), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 7, 2, 20), "timePlayed": 2000},
        ]
        return self._makeDb(tracks, entries)

    def test_shared_rows_produce_identical_output_to_fetching_twice(self):
        db = self._db()
        start = datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)
        end = datetime.datetime(2026, 7, 4, tzinfo=datetime.timezone.utc)

        separateSeries = db.getListeningTimeSeries(startDate=start, endDate=end, groupBy="day")
        separateHeatmap = db.getHourOfDayHeatmap(startDate=start, endDate=end)

        rows = db.getPlayBuckets(startDate=start, endDate=end)
        sharedSeries = db.getListeningTimeSeries(startDate=start, endDate=end, groupBy="day", bucketRows=rows)
        sharedHeatmap = db.getHourOfDayHeatmap(startDate=start, endDate=end, bucketRows=rows)

        self.assertEqual(sharedSeries, separateSeries)
        self.assertEqual(sharedHeatmap, separateHeatmap)

    def test_supplied_rows_skip_the_aggregate_entirely(self):
        db = self._db()
        rows = db.getPlayBuckets()

        with _QueryCounter(db.repo._conn()) as counter:
            db.getListeningTimeSeries(groupBy="day", bucketRows=rows)
            db.getHourOfDayHeatmap(bucketRows=rows)

        self.assertEqual(counter.count("FROM plays"), 0)

    def test_empty_supplied_rows_are_honoured_not_refetched(self):
        """[] is a real answer (no plays in range), not 'nothing supplied'."""
        db = self._db()

        with _QueryCounter(db.repo._conn()) as counter:
            series = db.getListeningTimeSeries(groupBy="day", bucketRows=[])

        self.assertEqual(series, [])
        self.assertEqual(counter.count("FROM plays"), 0)

    def test_without_supplied_rows_each_call_still_aggregates(self):
        db = self._db()

        with _QueryCounter(db.repo._conn()) as counter:
            db.getListeningTimeSeries(groupBy="day")

        self.assertEqual(counter.count("FROM plays"), 1)


class AdminOverviewScanCountTestCase(DatabaseTestCase):
    """Two admin-page reads that asked the same question twice (2026-09-03
    review, C-6 and C-10). Both are whole-table scans, and both answers were
    already in hand."""

    def _db(self):
        tracks = {'t1': normalizeTrackForTest({'id': 't1', 'name': 'Song', 'artists': []})}
        entries = [
            {'id': 't1', 'playedAt': _ts(2026, 7, 1, 9), 'timePlayed': 1000},
            {'id': 't1', 'playedAt': _ts(2026, 7, 2, 20), 'timePlayed': 2000},
        ]
        return self._makeDb(tracks, entries)

    def test_the_instance_play_totals_come_from_one_scan(self):
        """COUNT(*) and SUM(time_played) over the same `plays WHERE is_skip = 0`
        - the dominant cost of the page, paid twice."""
        db = self._db()

        with _QueryCounter(db.repo._conn()) as counter:
            stats = db.repo.getGlobalDatabaseStats()

        self.assertEqual(counter.count('FROM plays'), 1)
        self.assertEqual(stats['plays'], 2)
        self.assertEqual(stats['total_time_ms'], 3000)

    def test_an_empty_instance_still_reports_zero_rather_than_none(self):
        """SUM over no rows is NULL; the display figure must stay a number."""
        db = self._makeDb({}, [])

        stats = db.repo.getGlobalDatabaseStats()

        self.assertEqual(stats['plays'], 0)
        self.assertEqual(stats['total_time_ms'], 0)

    def test_catalog_coverage_asks_once_when_inherited_genres_are_off(self):
        """With inherited off, `WHERE (0 OR g.inherited = 0)` IS the own-tags
        query, so the second execute re-ran the first one verbatim."""
        db = self._db()

        with _QueryCounter(db.repo._conn()) as counter:
            db.repo.getCatalogGenreCoverage(includeInherited=False)

        #< two categories carry an inherited flag (tracks, albums); artists
        #  have none and only ever ran one query
        self.assertEqual(counter.count('track_genres'), 1)
        self.assertEqual(counter.count('album_genres'), 1)

    def test_it_still_asks_twice_when_inherited_genres_are_on(self):
        """The negative control: with inherited ON the two questions differ,
        and own_covered genuinely needs its own scan."""
        db = self._db()

        with _QueryCounter(db.repo._conn()) as counter:
            db.repo.getCatalogGenreCoverage(includeInherited=True)

        self.assertEqual(counter.count('track_genres'), 2)

    def test_the_numbers_are_the_same_either_way(self):
        """Reusing the result must not change the answer: with inherited off,
        covered and own_covered are the same count by construction."""
        db = self._db()

        coverage = db.repo.getCatalogGenreCoverage(includeInherited=False)

        for kind in ('song', 'album', 'artist'):
            with self.subTest(kind=kind):
                self.assertEqual(coverage[kind]['covered'], coverage[kind]['own_covered'])
                self.assertEqual(coverage[kind]['percent'], coverage[kind]['own_percent'])

class GenreTrendTransferTestCase(DatabaseTestCase):
    """The genre trend used to transfer one row per (play, genre): a play on a
    track carrying five tags shipped five rows, all bucketed in Python. SQL
    now pre-aggregates into 15-minute buckets like the artist trend does."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _db(self, playCount=20):
        tracks = {"t1": normalizeTrackForTest({"id": "t1", "name": "Song", "artists": []})}
        entries = [{"id": "t1", "playedAt": _ts(2026, 7, 1, 12) + minute * 60, "timePlayed": 60000}
                   for minute in range(playCount)]
        db = self._makeDb(tracks, entries)
        db.repo.replaceTrackGenres("t1", ["rock", "pop", "indie"])
        return db

    def test_rows_transferred_are_bucketed_not_one_per_play(self):
        playCount = 20   #< 20 plays x 3 genres = 60 raw rows, all inside 2 buckets
        db = self._db(playCount=playCount)

        rows = db.repo.getBucketedGenrePlayCounts(db.user, ["rock", "pop", "indie"], 1, None, None)

        self.assertLess(len(rows), playCount)
        self.assertEqual(sum(row["plays"] for row in rows), playCount * 3)

    def test_trend_counts_match_the_plays_that_produced_them(self):
        db = self._db(playCount=20)

        trend = db.getGenreTrends(["rock", "pop"], groupBy="day")

        self.assertEqual(trend["buckets"], ["2026-07-01"])
        for series in trend["series"]:
            self.assertEqual(series["data"], [20])

    def test_inherited_filter_still_applies(self):
        db = self._db(playCount=5)
        db.repo.replaceTrackGenres("t1", ["rock"], inherited=True)

        withInherited = db.getGenreTrends(["rock"], groupBy="day", includeInherited=True)
        withoutInherited = db.getGenreTrends(["rock"], groupBy="day", includeInherited=False)

        self.assertEqual(withInherited["series"][0]["data"], [5])
        self.assertEqual(withoutInherited, {"buckets": [], "series": []})


class OnThisDayQueryPlanTestCase(DatabaseTestCase):
    """getOnThisDay runs on every dashboard render. Matching on
    strftime('%m-%d', played_at, 'unixepoch') meant re-deriving a calendar date
    for every play the user had ever made - no index can serve that, so the
    cost grew with total history. It now asks for one indexed range per prior
    year."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _db(self, years=(2023, 2024, 2025)):
        tracks = {"t1": normalizeTrackForTest({"id": "t1", "name": "Song", "artists": []})}
        entries = [{"id": "t1", "playedAt": _ts(year, 7, 1, 12), "timePlayed": 60000} for year in years]
        # Unrelated history the old full-scan form still had to walk.
        entries += [{"id": "t1", "playedAt": _ts(year, 3, 15, 12), "timePlayed": 60000} for year in years]
        return self._makeDb(tracks, entries)

    def _plan(self, db, sql, params):
        return " ".join(row["detail"] for row in
                        db.repo._conn().execute("EXPLAIN QUERY PLAN " + sql, params).fetchall())

    def test_lookup_uses_the_play_time_index_instead_of_scanning(self):
        db = self._db()
        windows = db._onThisDayWindows(datetime.date(2026, 7, 1))
        self.assertTrue(windows)
        params = [db.user]
        for startTs, endTs in windows:
            params.extend((startTs, endTs))
        rangeClauses = " OR ".join("(p.played_at >= ? AND p.played_at < ?)" for _ in windows)

        plan = self._plan(db, f"SELECT p.played_at FROM plays p WHERE p.username = ? "
                              f"AND p.is_skip=0 AND ({rangeClauses})", params)

        self.assertIn("idx_plays_user_time", plan)
        self.assertNotIn("SCAN p", plan)

    def test_one_window_per_prior_year_with_data(self):
        db = self._db(years=(2023, 2024, 2025))

        windows = db._onThisDayWindows(datetime.date(2026, 7, 1))

        self.assertEqual(len(windows), 3)   #< 2023, 2024, 2025 - not the current year

    def test_no_history_asks_for_no_windows(self):
        db = self._makeDb({}, [])

        self.assertEqual(db._onThisDayWindows(datetime.date(2026, 7, 1)), [])

    def test_leap_day_falls_back_to_feb_28_in_non_leap_years(self):
        """datetime.date(2023, 2, 29) doesn't exist - building the window must
        not raise on Feb 29."""
        db = self._db(years=(2023,))

        windows = db._onThisDayWindows(datetime.date(2024, 2, 29))

        self.assertEqual(len(windows), 1)

    def test_leap_day_reports_no_matches_for_non_leap_years(self):
        """The exact local month/day match still rejects Feb 28 rows when the
        user is looking at Feb 29."""
        tracks = {"t1": normalizeTrackForTest({"id": "t1", "name": "Song", "artists": []})}
        entries = [{"id": "t1", "playedAt": _ts(2023, 2, 28, 12), "timePlayed": 60000}]
        db = self._makeDb(tracks, entries)

        result = db.getOnThisDay(now=datetime.datetime(2024, 2, 29, 12, tzinfo=datetime.timezone.utc))

        self.assertEqual(result, [])


class HistoryFullPlaysQueryPlanTestCase(DatabaseTestCase):
    """/history's "Full plays only" filter (on by DEFAULT) adds a tracks join to
    a count that was a pure index scan, and that count is the one O(N) query the
    page makes - the list itself is bounded by OFFSET+50.

    The join is accepted (the completion predicate needs duration_ms per play
    row, so every alternative form pays the same per-row seek, and an is_skip
    index was measured and rejected twice - see Database/db.py). What is NOT
    acceptable is the planner making `tracks` the OUTER loop: that drops
    idx_plays_user_time, and with it the ability to use played_at as an index
    bound, so a "Last Week" count starts walking the whole catalog. It flips
    that way on a small library once ANALYZE has run - this app never runs it,
    but Database/db.py already documents that per-user data distribution changes
    which index SQLite picks, so the shape is pinned rather than assumed."""

    def _db(self):
        tracks = {"t1": normalizeTrackForTest({"id": "t1", "name": "Song", "artists": []})}
        entries = [{"id": "t1", "playedAt": _ts(2025, month, 1), "timePlayed": 60000}
                   for month in range(1, 13)]
        return self._makeDb(tracks, entries)

    def _countPlan(self, db, **kwargs):
        """The plan for the statement getPlaysCount ACTUALLY issues, rather than
        a copy of it here that would drift the first time the query changes.
        The trace callback hands back the statement with its parameters already
        expanded, so it re-explains without them."""
        conn = db.repo._conn()
        with _QueryCounter(conn) as counter:
            db.repo.getPlaysCount(db.user, **kwargs)
        countSql = [sql for sql in counter.statements if "COUNT(*)" in sql]
        self.assertEqual(len(countSql), 1, counter.statements)
        return " ".join(row["detail"] for row in
                        conn.execute("EXPLAIN QUERY PLAN " + countSql[0]).fetchall())

    def test_the_ranged_count_still_rides_the_play_time_index(self):
        plan = self._countPlan(self._db(), startTs=_ts(2025, 3, 1), endTs=_ts(2025, 6, 1),
                               fullPlaysOnly=True)

        self.assertIn("idx_plays_user_time", plan)

    def test_the_catalog_is_never_the_outer_loop(self):
        """A SCAN of ft means every track in the library is walked and the
        played_at range stops being a seek - the regression this class exists
        for."""
        plan = self._countPlan(self._db(), startTs=_ts(2025, 3, 1), endTs=_ts(2025, 6, 1),
                               fullPlaysOnly=True)

        self.assertNotIn("SCAN ft", plan)

    def test_the_all_time_count_still_rides_the_play_time_index(self):
        """The default /history view is unscoped, so this is the plan most page
        loads actually get."""
        plan = self._countPlan(self._db(), fullPlaysOnly=True)

        self.assertIn("idx_plays_user_time", plan)
        self.assertNotIn("SCAN ft", plan)

    def test_the_filter_off_leaves_the_plays_scan_alone(self):
        """_tracksJoin is emitted only when a duration filter is active, so the
        queries that never needed the catalog keep not touching it."""
        plan = self._countPlan(self._db(), fullPlaysOnly=False)

        self.assertNotIn("tracks", plan)


class PerRequestMemoizationTestCase(AppTestCase):
    """Context processors re-run on every render, and one request can render a
    dozen templates - an un-memoized "cheap settings read" is really one
    app_settings SELECT per setting per partial."""

    def test_instance_wide_settings_are_read_once_per_request(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        settingReads = {
            "tags_enabled": patch.object(dash.repo, "isTagsEnabled", wraps=dash.repo.isTagsEnabled),
            "registration_enabled": patch.object(dash.repo, "isRegistrationEnabled",
                                                  wraps=dash.repo.isRegistrationEnabled),
        }
        mocks = {name: patcher.start() for name, patcher in settingReads.items()}
        for patcher in settingReads.values():
            self.addCleanup(patcher.stop)

        resp = client.get("/login")

        self.assertEqual(resp.status_code, 200)
        for name, mock in mocks.items():
            with self.subTest(setting=name):
                self.assertLessEqual(mock.call_count, 1)


class PlaylistNameLookupTestCase(DatabaseTestCase):
    """The history and detail play lists resolve a playlist name per row, and a
    page of plays from one listening session mostly shares a few contexts."""

    def test_repeated_lookups_within_one_request_hit_the_db_once(self):
        db = self._makeDb({}, [])
        db.repo.upsertPlaylistName("pl1", "playlist", "My Playlist")
        app = Flask(__name__)

        with patch.object(db.repo, "getPlaylistName", wraps=db.repo.getPlaylistName) as spy:
            with app.test_request_context("/"):
                names = [db.playlistName("playlist:pl1") for _ in range(10)]

        self.assertEqual(names, ["My Playlist"] * 10)
        self.assertEqual(spy.call_count, 1)

    def test_lookups_outside_a_request_are_not_cached(self):
        """Background workers have no app context - they must read through
        rather than share a stale name across an entire process lifetime."""
        db = self._makeDb({}, [])
        db.repo.upsertPlaylistName("pl1", "playlist", "My Playlist")

        with patch.object(db.repo, "getPlaylistName", wraps=db.repo.getPlaylistName) as spy:
            db.playlistName("playlist:pl1")
            db.playlistName("playlist:pl1")

        self.assertEqual(spy.call_count, 2)


class DashboardTrendsHydrationTestCase(DatabaseTestCase):
    """The three trend cards were hydrated one getTrack at a time - 3 queries
    each, 9 for an endpoint returning at most 3 tracks."""

    def test_trend_tracks_are_hydrated_in_one_batch(self):
        tracks = {"t1": normalizeTrackForTest({"id": "t1", "name": "Song", "artists": []})}
        entries = [{"id": "t1", "playedAt": _ts(2026, 7, 1, 12) + n * 3600, "timePlayed": 60000}
                   for n in range(5)]
        db = self._makeDb(tracks, entries)

        with patch.object(db.repo, "getTrack", wraps=db.repo.getTrack) as perItem:
            db.getDashboardTrends(now_ts=_ts(2026, 7, 2, 12))

        perItem.assert_not_called()


if __name__ == "__main__":
    import unittest
    unittest.main()
