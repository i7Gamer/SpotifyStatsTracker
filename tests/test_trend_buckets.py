"""The shared "Trend buckets" control: Auto (the empty groupBy value) derives
day/week/month from the range span via _resolveGroupBy - the resolution
Compare's trend always had, now shared by /charts, the song/artist/album
detail pages, and Wrapped. Detail pages also gained an ?ajax=true branch so a
bucket change refetches just the play-history series (static/js/
detail-chart.js) instead of reloading the page.

The template selects submit the RAW param ("" for Auto) and routes resolve it
server-side; open-ended ranges (all time, an item's whole history) derive
their span from getPlayTimeRange, which now takes the same trackId/artistId/
albumId narrowing as the other play-scan queries.
"""
import datetime
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase
from dashboard.date_ranges import DateRangeMixin
from config import COMPARE_TREND_WEEK_SPAN_DAYS, COMPARE_TREND_MONTH_SPAN_DAYS
from Database.repository import Repository
import Database.utils as utilsModule

UTC = datetime.timezone.utc
SECONDS_PER_DAY = 86400
LONG_SPAN_SECONDS = (COMPARE_TREND_MONTH_SPAN_DAYS + 10) * SECONDS_PER_DAY


class _Resolver(DateRangeMixin):
    """Bare mixin host - _resolveGroupBy touches no instance state."""


def _spanDates(days):
    start = datetime.datetime(2024, 1, 1, tzinfo=UTC)
    return start, start + datetime.timedelta(days=days)


class TestResolveGroupBy(unittest.TestCase):
    def setUp(self):
        self.dash = _Resolver()

    def test_explicit_choice_wins_over_any_span(self):
        start, end = _spanDates(COMPARE_TREND_MONTH_SPAN_DAYS * 3)
        for choice in ("day", "week", "month"):
            self.assertEqual(self.dash._resolveGroupBy(choice, start, end), choice)

    def test_auto_short_span_is_day(self):
        self.assertEqual(self.dash._resolveGroupBy("", *_spanDates(COMPARE_TREND_WEEK_SPAN_DAYS)), "day")

    def test_auto_medium_span_is_week(self):
        self.assertEqual(self.dash._resolveGroupBy("", *_spanDates(COMPARE_TREND_WEEK_SPAN_DAYS + 1)), "week")
        self.assertEqual(self.dash._resolveGroupBy("", *_spanDates(COMPARE_TREND_MONTH_SPAN_DAYS)), "week")

    def test_auto_long_span_is_month(self):
        self.assertEqual(self.dash._resolveGroupBy("", *_spanDates(COMPARE_TREND_MONTH_SPAN_DAYS + 1)), "month")

    def test_junk_param_resolves_like_auto(self):
        start, end = _spanDates(COMPARE_TREND_MONTH_SPAN_DAYS + 1)
        self.assertEqual(self.dash._resolveGroupBy("nonsense", start, end), "month")

    def test_missing_dates_fall_back_to_day(self):
        self.assertEqual(self.dash._resolveGroupBy(""), "day")
        self.assertEqual(self.dash._resolveGroupBy("", _spanDates(10)[0], None), "day")


class TestGetDateRangeEndDateDoesNotSpillIntoTomorrow(unittest.TestCase):
    """week/month/year/5years all fall through to _getDateRange's default
    endDate (no interval branch sets one). It must cap at the start of
    tomorrow, local time - not "now plus a day", which stamps today's
    time-of-day onto tomorrow's date and manufactures an empty future bucket
    once getListeningTimeSeries gap-fills up to it."""

    def setUp(self):
        self.dash = _Resolver()
        # startOfDay/convertToDatetime fall back to Database.utils' global tz
        # when _getDateRange isn't given one explicitly (the routes' real
        # calls always pass db.tz - this pins it so the test is deterministic
        # regardless of the machine's TZ env var).
        patcher = patch.object(utilsModule, "tz", UTC)
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch("dashboard.date_ranges.now")
    def test_week_month_year_5years_end_at_start_of_tomorrow(self, mockNow):
        mockNow.return_value = datetime.datetime(2026, 7, 23, 15, 30, tzinfo=UTC)
        expected = datetime.datetime(2026, 7, 24, 0, 0, tzinfo=UTC)
        for interval in ("week", "month", "year", "5years"):
            with self.subTest(interval=interval):
                _, endDate = self.dash._getDateRange(interval)
                self.assertEqual(endDate, expected)

    @patch("dashboard.date_ranges.now")
    def test_end_of_tomorrow_is_stable_even_right_at_midnight(self, mockNow):
        mockNow.return_value = datetime.datetime(2026, 7, 23, 0, 0, tzinfo=UTC)
        _, endDate = self.dash._getDateRange("week")
        self.assertEqual(endDate, datetime.datetime(2026, 7, 24, 0, 0, tzinfo=UTC))


def _track(trackId, artistIds, albumId):
    return {
        "id": trackId,
        "name": f"Track {trackId}",
        "url": f"http://example.com/track/{trackId}",
        "artists": [
            {"id": aid, "name": f"Artist {aid}", "url": f"http://example.com/artist/{aid}",
             "imageUrl": "", "imageId": aid}
            for aid in artistIds
        ],
        "album": {
            "id": albumId, "name": f"Album {albumId}", "url": f"http://example.com/album/{albumId}",
            "imageId": albumId, "imageUrl": "", "totalTracks": 10, "releaseDate": 0.0,
        },
        "imageUrl": "", "imageId": albumId, "duration": 200000, "explicit": False,
        "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0.0,
    }


class TestPlayTimeRangeItemScope(unittest.TestCase):
    """getPlayTimeRange's new trackId/artistId/albumId narrowing - the span
    the detail pages' Auto resolution derives from."""

    USER = "alice"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "range.db")
        self.addCleanup(self.repo.connectionManager.close)
        self.repo.upsertUser(self.USER, "alice@example.com")
        self.repo.upsertTrack(_track("t1", ["a1"], "al1"))
        self.repo.upsertTrack(_track("t2", ["a2"], "al2"))
        self.repo.insertPlay(self.USER, "t1", 100.0, 60000)
        self.repo.insertPlay(self.USER, "t1", 300.0, 60000)
        self.repo.insertPlay(self.USER, "t2", 200.0, 60000)

    def test_whole_history_unchanged(self):
        self.assertEqual(self.repo.getPlayTimeRange(self.USER), (100.0, 300.0))

    def test_track_scope(self):
        self.assertEqual(self.repo.getPlayTimeRange(self.USER, trackId="t1"), (100.0, 300.0))
        self.assertEqual(self.repo.getPlayTimeRange(self.USER, trackId="t2"), (200.0, 200.0))

    def test_artist_scope(self):
        self.assertEqual(self.repo.getPlayTimeRange(self.USER, artistId="a2"), (200.0, 200.0))

    def test_album_scope(self):
        self.assertEqual(self.repo.getPlayTimeRange(self.USER, albumId="al1"), (100.0, 300.0))

    def test_no_matching_plays_returns_none(self):
        self.assertIsNone(self.repo.getPlayTimeRange(self.USER, trackId="t999"))

    def test_skips_are_excluded(self):
        self.repo.insertPlay(self.USER, "t2", 50.0, 400, is_skip=1)
        self.assertEqual(self.repo.getPlayTimeRange(self.USER, trackId="t2"), (200.0, 200.0))


class _DetailAjaxTestBase(AppTestCase):
    def _getPath(self, dash, db, path):
        # The detail routes unconditionally fetch a page of play history (see
        # _detailHistoryContext) - default it to "no history", same as
        # test_detail_pages_route.py's _DetailRouteTestBase.
        if not isinstance(db.getEntriesCount.return_value, int):
            db.getEntriesCount.return_value = 0
        if not isinstance(db.getEntriesFromNew.return_value, list):
            db.getEntriesFromNew.return_value = []
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(path)

    def _db(self):
        db = MagicMock()
        db.tz = UTC
        db.getListeningTimeSeries.return_value = [
            {"label": "2024-01-01", "totalTimeListened": 1000, "plays": 1},
        ]
        return db


class TestSongDetailAjax(_DetailAjaxTestBase):
    def _song(self):
        return {"id": "t1", "name": "Song One", "plays": 5}

    def test_ajax_returns_series_and_resolved_bucket_only(self):
        dash = self._makeApp()
        db = self._db()
        db.getSong.return_value = self._song()
        with patch.object(dash.repo, "getPlayTimeRange", return_value=(0.0, LONG_SPAN_SECONDS)) as mockRange:
            resp = self._getPath(dash, db, "/song/t1?ajax=true")

        self.assertEqual(resp.mimetype, "application/json")
        payload = resp.get_json()
        self.assertEqual(payload["groupBy"], "month")   #< auto from the multi-year item span
        self.assertEqual(len(payload["timeSeries"]), 1)
        self.assertEqual(mockRange.call_args.kwargs.get("trackId"), "t1")
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "month")
        db.getHourOfDayHeatmap.assert_not_called()   #< bucket-independent work is skipped

    def test_explicit_bucket_wins_on_ajax(self):
        dash = self._makeApp()
        db = self._db()
        db.getSong.return_value = self._song()
        with patch.object(dash.repo, "getPlayTimeRange", return_value=(0.0, LONG_SPAN_SECONDS)):
            resp = self._getPath(dash, db, "/song/t1?ajax=true&groupBy=day")

        self.assertEqual(resp.get_json()["groupBy"], "day")

    def test_full_page_defaults_to_auto_selected(self):
        dash = self._makeApp()
        db = self._db()
        db.getSong.return_value = {
            "id": "t1", "name": "Song One", "url": "http://example.com/t1",
            "imageId": "alb1", "duration": 200000, "explicit": False, "isrc": "",
            "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album One", "url": "u", "imageId": "alb1",
                      "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
            "artists": [{"id": "a1", "name": "Artist A", "url": "u", "imageUrl": "", "imageId": "a1"}],
            "plays": 5, "totalTimeListened": 50000, "firstListenedAt": 100,
        }
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b'<option value="" selected>Auto</option>', resp.data)
        self.assertIn(b"detail-chart.js", resp.data)


class TestArtistDetailAjax(_DetailAjaxTestBase):
    def test_ajax_returns_series_and_skips_the_song_list(self):
        dash = self._makeApp()
        db = self._db()
        db.getArtist.return_value = {"id": "a1", "name": "Artist A"}
        with patch.object(dash.repo, "getPlayTimeRange", return_value=(0.0, LONG_SPAN_SECONDS)) as mockRange:
            resp = self._getPath(dash, db, "/artist/a1?ajax=true")

        payload = resp.get_json()
        self.assertEqual(payload["groupBy"], "month")
        self.assertEqual(mockRange.call_args.kwargs.get("artistId"), "a1")
        db.getSongsStats.assert_not_called()
        db.lazyFetchArtistBio.assert_not_called()


class TestAlbumDetailAjax(_DetailAjaxTestBase):
    def test_ajax_returns_series_and_skips_the_song_list(self):
        dash = self._makeApp()
        db = self._db()
        db.getAlbum.return_value = {"id": "alb1", "name": "Album One"}
        with patch.object(dash.repo, "getPlayTimeRange", return_value=(0.0, LONG_SPAN_SECONDS)) as mockRange:
            resp = self._getPath(dash, db, "/album/alb1?ajax=true")

        payload = resp.get_json()
        self.assertEqual(payload["groupBy"], "month")
        self.assertEqual(mockRange.call_args.kwargs.get("albumId"), "alb1")
        db.getSongsStats.assert_not_called()
        db.lazyFetchAlbumBio.assert_not_called()


class TestChartsAutoBuckets(AppTestCase):
    def _makeDb(self):
        db = MagicMock()
        db.tz = UTC
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getArtistTrend.return_value = {"buckets": [], "series": []}
        db.getExplicitRatio.return_value = {"explicit": 0, "clean": 0}
        db.getReleaseDecadeDistribution.return_value = {}
        db.getCompletionStats.return_value = {"skips": 0, "completes": 0, "partials": 0}
        db.repo.getUserSettings.return_value = {"default_dashboard_window": "month", "timezone": None}
        return db

    def _get(self, dash, db, query=""):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/charts{query}")

    def test_all_time_auto_buckets_from_the_play_range(self):
        dash = self._makeApp()
        db = self._makeDb()
        with patch.object(dash.repo, "getPlayTimeRange", return_value=(0.0, LONG_SPAN_SECONDS)):
            resp = self._get(dash, db, "?interval=all+time&ajax=true")

        self.assertEqual(resp.get_json()["groupBy"], "month")
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "month")

    def test_all_time_auto_without_plays_falls_back_to_day(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._get(dash, db, "?interval=all+time&ajax=true")   #< real (empty) repo: no play range

        self.assertEqual(resp.get_json()["groupBy"], "day")

    def test_shell_defaults_to_auto_selected(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._get(dash, db)

        self.assertIn(b'<option value="" selected>Auto</option>', resp.data)
        self.assertIn(b"Trend buckets:", resp.data)

    def test_explicit_bucket_stays_selected_in_the_shell(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._get(dash, db, "?groupBy=week")

        self.assertIn(b'<option value="week" selected>Week</option>', resp.data)


if __name__ == "__main__":
    unittest.main()
