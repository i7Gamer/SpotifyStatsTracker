"""Local-timezone chart bucketing (services/time_buckets.py).

Pure layout logic tested against plain lists of pre-aggregated bucket rows -
no DB - mirroring test_listening_calendar.py. Database.database's
getGenreTrends/getArtistTrend/getHourOfDayHeatmap/getGenreHourOfDayHeatmap/
getListeningTimeSeries wiring into these is covered by the existing
chart/heatmap/trend route and Database-level tests (test_chart_stats.py,
test_trends.py, test_trend_buckets.py, test_repository_genre_trend.py, ...) -
this file is the pure-function net underneath them, with hand-computed
oracles (a property test needs an oracle recomputed a different way, not the
formula under test read back at itself)."""
import os
import sys
import datetime
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import services.time_buckets as timeBucketsModule
from services.time_buckets import (
    bucketKey, bucketRowsByKey, fillHeatmapGrid, buildTimeSeries,
    MAX_TIME_SERIES_BUCKETS, TIME_SERIES_MIN_BUCKET_DAYS,
)

_UTC = datetime.timezone.utc
_NY = ZoneInfo("America/New_York")


def _row(ts, totalTimeListened=1000, plays=1, skips=0, **extra):
    row = {"bucketStartTs": ts, "totalTimeListened": totalTimeListened, "plays": plays}
    if skips is not None:
        row["skips"] = skips
    row.update(extra)
    return row


class TestBucketKey(unittest.TestCase):
    """2026-07-22 is a Wednesday; the Monday of its week is 2026-07-20 (hand
    verified against a calendar, independent of the code under test)."""

    _WEDNESDAY = datetime.datetime(2026, 7, 22, 14, 35, 9, tzinfo=_UTC)

    def test_day_groupby_is_the_local_calendar_date(self):
        self.assertEqual(bucketKey(self._WEDNESDAY, "day", _UTC), "2026-07-22")

    def test_unknown_groupby_falls_back_to_day(self):
        self.assertEqual(bucketKey(self._WEDNESDAY, "not-a-real-groupby", _UTC), "2026-07-22")

    def test_week_groupby_is_the_monday_of_that_week(self):
        self.assertEqual(bucketKey(self._WEDNESDAY, "week", _UTC), "2026-07-20")

    def test_hour_groupby_truncates_to_the_hour(self):
        self.assertEqual(bucketKey(self._WEDNESDAY, "hour", _UTC), "2026-07-22 14:00")

    def test_month_groupby_is_year_and_month(self):
        self.assertEqual(bucketKey(self._WEDNESDAY, "month", _UTC), "2026-07")

    def test_keys_are_lexically_sortable_across_the_month_seam(self):
        """getGenreTrends/getArtistTrend sort the bucket-key union directly -
        no date parsing - so "2026-08" must sort after "2026-07-31"."""
        july31 = datetime.datetime(2026, 7, 31, tzinfo=_UTC)
        august1 = datetime.datetime(2026, 8, 1, tzinfo=_UTC)
        self.assertLess(bucketKey(july31, "day", _UTC), bucketKey(august1, "day", _UTC))

    def test_a_non_utc_timezone_shifts_the_day_boundary(self):
        """23:30 UTC on the 22nd is 19:30 the same day in New York (UTC-4 in
        July, DST) - not yet past midnight there, unlike a naive UTC read."""
        lateUtc = datetime.datetime(2026, 7, 22, 23, 30, tzinfo=_UTC)
        self.assertEqual(bucketKey(lateUtc, "day", _NY), "2026-07-22")
        stillLater = datetime.datetime(2026, 7, 23, 3, 30, tzinfo=_UTC)   #< 23:30 EDT on the 22nd
        self.assertEqual(bucketKey(stillLater, "day", _NY), "2026-07-22")


class TestBucketRowsByKey(unittest.TestCase):
    def test_pairs_each_row_with_its_bucket_key_in_order(self):
        ts1 = datetime.datetime(2026, 7, 20, 10, 0, tzinfo=_UTC).timestamp()
        ts2 = datetime.datetime(2026, 7, 21, 10, 0, tzinfo=_UTC).timestamp()
        rows = [_row(ts1, plays=3), _row(ts2, plays=5)]

        result = bucketRowsByKey(rows, _UTC, "day")

        self.assertEqual([key for key, _ in result], ["2026-07-20", "2026-07-21"])
        self.assertEqual([row["plays"] for _, row in result], [3, 5])

    def test_rows_sharing_a_bucketStartTs_get_the_same_key(self):
        ts = datetime.datetime(2026, 7, 20, 10, 0, tzinfo=_UTC).timestamp()
        rows = [_row(ts, plays=1), _row(ts, plays=2)]

        result = bucketRowsByKey(rows, _UTC, "week")

        self.assertEqual([key for key, _ in result], ["2026-07-20", "2026-07-20"])

    def test_the_timezone_conversion_is_memoized_per_distinct_timestamp(self):
        """The whole point of the cache: 5 rows sharing one bucketStartTs must
        convert it to a local datetime once, not 5 times (bucketKeyCache's
        original justification - ~77k rows collapsing to ~21k conversions on
        a large library)."""
        ts = datetime.datetime(2026, 7, 20, 10, 0, tzinfo=_UTC).timestamp()
        rows = [_row(ts) for _ in range(5)]

        with patch.object(timeBucketsModule, "convertToDatetime",
                          wraps=timeBucketsModule.convertToDatetime) as spy:
            bucketRowsByKey(rows, _UTC, "day")

        self.assertEqual(spy.call_count, 1)

    def test_empty_rows_returns_empty(self):
        self.assertEqual(bucketRowsByKey([], _UTC, "day"), [])


class TestFillHeatmapGrid(unittest.TestCase):
    """2026-07-20 00:00 UTC is a Monday (weekday()==0) - hand-verified via
    datetime.timestamp()/fromtimestamp(), independent of the bucketing code."""

    def test_returns_a_7_by_24_grid_of_zeroed_cells_for_no_rows(self):
        grid = fillHeatmapGrid([], _UTC)

        self.assertEqual(len(grid), 7)
        self.assertTrue(all(len(row) == 24 for row in grid))
        self.assertEqual(grid[0][0], {"totalTimeListened": 0, "plays": 0})

    def test_a_row_lands_in_its_local_weekday_and_hour_cell(self):
        monday3am = datetime.datetime(2026, 7, 20, 3, 0, tzinfo=_UTC)
        self.assertEqual(monday3am.weekday(), 0)   #< Monday, hand check
        ts = monday3am.timestamp()

        grid = fillHeatmapGrid([_row(ts, totalTimeListened=5000, plays=2)], _UTC)

        self.assertEqual(grid[0][3], {"totalTimeListened": 5000, "plays": 2})
        # Every other cell stays at zero.
        self.assertEqual(grid[0][4], {"totalTimeListened": 0, "plays": 0})
        self.assertEqual(grid[1][3], {"totalTimeListened": 0, "plays": 0})

    def test_two_rows_in_the_same_cell_accumulate(self):
        monday3am = datetime.datetime(2026, 7, 20, 3, 0, tzinfo=_UTC).timestamp()
        monday3_15am = datetime.datetime(2026, 7, 20, 3, 15, tzinfo=_UTC).timestamp()

        grid = fillHeatmapGrid(
            [_row(monday3am, totalTimeListened=1000, plays=1),
             _row(monday3_15am, totalTimeListened=2000, plays=3)],
            _UTC)

        self.assertEqual(grid[0][3], {"totalTimeListened": 3000, "plays": 4})

    def test_a_non_utc_timezone_shifts_the_weekday_and_hour(self):
        """23:30 UTC on Sunday the 19th is 19:30 EDT the same Sunday in New
        York (UTC-4 in July) - both land on Sunday (weekday 6), but at
        different hours."""
        sundayLateUtc = datetime.datetime(2026, 7, 19, 23, 30, tzinfo=_UTC)
        self.assertEqual(sundayLateUtc.weekday(), 6)   #< Sunday, hand check
        ts = sundayLateUtc.timestamp()

        gridUtc = fillHeatmapGrid([_row(ts)], _UTC)
        gridNy = fillHeatmapGrid([_row(ts)], _NY)

        self.assertEqual(gridUtc[6][23]["plays"], 1)   #< Sunday 23:30 UTC
        self.assertEqual(gridNy[6][19]["plays"], 1)    #< Sunday 19:30 in New York


class TestBuildTimeSeries(unittest.TestCase):
    def test_empty_rows_with_no_explicit_range_is_empty(self):
        self.assertEqual(buildTimeSeries([], _UTC, "day"), [])

    def test_a_single_row_produces_one_bucket_when_the_range_is_implicit(self):
        ts = datetime.datetime(2026, 7, 20, 10, 0, tzinfo=_UTC).timestamp()

        result = buildTimeSeries([_row(ts, totalTimeListened=4000, plays=2)], _UTC, "day")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], {"label": "2026-07-20", "totalTimeListened": 4000,
                                     "plays": 2, "skips": 0})

    def test_gaps_between_rows_are_zero_filled(self):
        """A play on the 20th and one on the 23rd must gap-fill the 21st and
        22nd as zero buckets, not skip straight from one to the other."""
        ts1 = datetime.datetime(2026, 7, 20, 10, 0, tzinfo=_UTC).timestamp()
        ts2 = datetime.datetime(2026, 7, 23, 10, 0, tzinfo=_UTC).timestamp()

        result = buildTimeSeries([_row(ts1, plays=1), _row(ts2, plays=1)], _UTC, "day")

        self.assertEqual([b["label"] for b in result],
                         ["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23"])
        self.assertEqual([b["plays"] for b in result], [1, 0, 0, 1])

    def test_skips_default_to_zero_when_the_row_omits_them(self):
        ts = datetime.datetime(2026, 7, 20, 10, 0, tzinfo=_UTC).timestamp()
        row = _row(ts, skips=None)   #< _row omits the "skips" key entirely when skips=None

        result = buildTimeSeries([row], _UTC, "day")

        self.assertEqual(result[0]["skips"], 0)

    def test_explicit_start_and_end_bound_the_gap_fill_beyond_the_rows(self):
        ts = datetime.datetime(2026, 7, 21, 10, 0, tzinfo=_UTC).timestamp()
        start = datetime.datetime(2026, 7, 20, tzinfo=_UTC)
        end = datetime.datetime(2026, 7, 24, tzinfo=_UTC)   #< half-open, so up to the 23rd

        result = buildTimeSeries([_row(ts, plays=9)], _UTC, "day", startDate=start, endDate=end)

        self.assertEqual([b["label"] for b in result],
                         ["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23"])
        self.assertEqual([b["plays"] for b in result], [0, 9, 0, 0])

    def test_week_groupby_buckets_by_monday(self):
        ts1 = datetime.datetime(2026, 7, 22, tzinfo=_UTC).timestamp()   #< Wed, week of 7/20
        ts2 = datetime.datetime(2026, 7, 29, tzinfo=_UTC).timestamp()   #< Wed, week of 7/27

        result = buildTimeSeries([_row(ts1, plays=1), _row(ts2, plays=1)], _UTC, "week")

        self.assertEqual([b["label"] for b in result], ["2026-07-20", "2026-07-27"])

    def test_month_groupby_advances_a_calendar_month_not_a_fixed_span(self):
        ts1 = datetime.datetime(2026, 1, 31, tzinfo=_UTC).timestamp()
        ts2 = datetime.datetime(2026, 3, 1, tzinfo=_UTC).timestamp()

        result = buildTimeSeries([_row(ts1, plays=1), _row(ts2, plays=1)], _UTC, "month")

        #< Jan (31 days) then Feb (28) must each count as ONE bucket, not
        #  however many 31-day steps it'd take a fixed timedelta to cross them
        self.assertEqual([b["label"] for b in result], ["2026-01", "2026-02", "2026-03"])

    def test_hour_groupby_advances_by_one_hour(self):
        ts1 = datetime.datetime(2026, 7, 20, 10, 0, tzinfo=_UTC).timestamp()
        ts2 = datetime.datetime(2026, 7, 20, 12, 0, tzinfo=_UTC).timestamp()

        result = buildTimeSeries([_row(ts1, plays=1), _row(ts2, plays=1)], _UTC, "hour")

        self.assertEqual([b["label"] for b in result],
                         ["2026-07-20 10:00", "2026-07-20 11:00", "2026-07-20 12:00"])
        self.assertEqual([b["plays"] for b in result], [1, 0, 1])

    def test_a_range_implying_more_than_the_cap_is_clamped_to_the_newest_end(self):
        """MAX_TIME_SERIES_BUCKETS is the gap-fill's hard ceiling (an
        unvalidated centuries-long range must not emit one bucket per day
        across it) - the START is clamped up so the newest data survives."""
        ts = datetime.datetime(2023, 11, 14, tzinfo=_UTC).timestamp()
        start = datetime.datetime(1200, 1, 1, tzinfo=_UTC)
        end = datetime.datetime(2024, 1, 1, tzinfo=_UTC)

        result = buildTimeSeries([_row(ts, plays=1)], _UTC, "day", startDate=start, endDate=end)

        #< +1: re-aligning the clamped start onto the bucket grid can add one
        self.assertLessEqual(len(result), MAX_TIME_SERIES_BUCKETS + 1)
        self.assertEqual(sum(b["plays"] for b in result), 1)
        self.assertEqual(result[-1]["label"], "2023-12-31")

    def test_a_normal_range_is_not_clamped(self):
        ts = datetime.datetime(2026, 7, 20, tzinfo=_UTC).timestamp()
        start = datetime.datetime(2026, 7, 1, tzinfo=_UTC)
        end = datetime.datetime(2026, 7, 31, tzinfo=_UTC)

        result = buildTimeSeries([_row(ts, plays=1)], _UTC, "day", startDate=start, endDate=end)

        self.assertEqual(len(result), 30)

    def test_a_non_utc_timezone_buckets_by_the_local_day(self):
        """23:30 UTC on the 22nd is still the 22nd in New York (EDT,
        UTC-4) - a UTC-only bucketer would put it on the 23rd instead."""
        ts = datetime.datetime(2026, 7, 22, 23, 30, tzinfo=_UTC).timestamp()

        resultUtc = buildTimeSeries([_row(ts, plays=1)], _UTC, "day")
        resultNy = buildTimeSeries([_row(ts, plays=1)], _NY, "day")

        self.assertEqual(resultUtc[0]["label"], "2026-07-22")
        self.assertEqual(resultNy[0]["label"], "2026-07-22")


class TestTimeSeriesMinBucketDaysTable(unittest.TestCase):
    """The clamp's sizing table - every groupBy buildTimeSeries branches on
    must have an entry, or the clamp computation KeyErrors instead of
    degrading gracefully."""

    def test_every_groupby_branch_has_an_entry(self):
        for groupBy in ("hour", "day", "week", "month"):
            self.assertIn(groupBy, TIME_SERIES_MIN_BUCKET_DAYS)
