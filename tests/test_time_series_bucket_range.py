"""_timeSeriesBucketRange()/_embedTimeSeriesTextElements()'s rangeStart/
rangeEnd stamping - the prerequisite Charts' chart click-to-navigate reads
to build a `?interval=custom&startDate=...&endDate=...` link. Values are
plain "YYYY-MM-DD" strings chosen to round-trip straight into
_getDateRange's custom-range parsing, which treats its own endDate as
inclusive (see _getDateRange's "+ timedelta(days=1)").
"""
import unittest
from unittest.mock import patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp
from _app_factory import makeApp as _makeApp

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'



class TestTimeSeriesBucketRange(unittest.TestCase):
    def test_day_bucket_is_a_single_inclusive_day(self):
        dash = _makeApp()
        self.assertEqual(dash._timeSeriesBucketRange("2026-07-15", "day"), ("2026-07-15", "2026-07-15"))

    def test_week_bucket_spans_monday_to_sunday(self):
        dash = _makeApp()
        self.assertEqual(dash._timeSeriesBucketRange("2026-07-06", "week"), ("2026-07-06", "2026-07-12"))

    def test_month_bucket_spans_the_whole_calendar_month(self):
        dash = _makeApp()
        self.assertEqual(dash._timeSeriesBucketRange("2026-07", "month"), ("2026-07-01", "2026-07-31"))

    def test_month_bucket_handles_a_28_day_february(self):
        dash = _makeApp()
        self.assertEqual(dash._timeSeriesBucketRange("2026-02", "month"), ("2026-02-01", "2026-02-28"))

    def test_month_bucket_handles_the_december_to_january_year_rollover(self):
        """December's "next month" is January of the FOLLOWING year - a fixed
        month+1 without a year bump would silently wrap to month 13."""
        dash = _makeApp()
        self.assertEqual(dash._timeSeriesBucketRange("2026-12", "month"), ("2026-12-01", "2026-12-31"))

    def test_unsupported_groupby_returns_none(self):
        """The Charts single-day view buckets by hour (see chartsPage's
        timeSeriesGroupBy) - there's no clean calendar-date mapping for that,
        so those buckets are left un-clickable rather than linking somewhere
        wrong."""
        dash = _makeApp()
        self.assertIsNone(dash._timeSeriesBucketRange("2026-07-15 14:00", "hour"))

    def test_none_groupby_returns_none(self):
        """The default for callers that don't opt into click-navigation
        (Wrapped's own chart, detail pages) - see _embedTimeSeriesTextElements's
        docstring."""
        dash = _makeApp()
        self.assertIsNone(dash._timeSeriesBucketRange("2026-07-15", None))

    def test_malformed_label_returns_none_instead_of_raising(self):
        dash = _makeApp()
        self.assertIsNone(dash._timeSeriesBucketRange("not-a-date", "day"))


class TestEmbedTimeSeriesTextElements(unittest.TestCase):
    def test_stamps_range_fields_when_groupby_given(self):
        dash = _makeApp()
        timeSeries = [{"label": "2026-07-06", "totalTimeListened": 60000, "plays": 3}]

        result = dash._embedTimeSeriesTextElements(timeSeries, groupBy="week")

        self.assertEqual(result[0]["rangeStart"], "2026-07-06")
        self.assertEqual(result[0]["rangeEnd"], "2026-07-12")
        self.assertEqual(result[0]["totalTimeListenedText"], "1m 0s")

    def test_omits_range_fields_when_groupby_not_given(self):
        """Wrapped's own call site (_buildWrappedContext) doesn't pass
        groupBy - its chart isn't click-navigable, so no rangeStart/rangeEnd
        should end up in the payload sent to the client."""
        dash = _makeApp()
        timeSeries = [{"label": "2026-07-06", "totalTimeListened": 60000, "plays": 3}]

        result = dash._embedTimeSeriesTextElements(timeSeries)

        self.assertNotIn("rangeStart", result[0])
        self.assertNotIn("rangeEnd", result[0])

    def test_mutates_and_returns_the_same_list(self):
        """Matches the pre-existing totalTimeListenedText stamping - callers
        rely on in-place mutation, not a new list."""
        dash = _makeApp()
        timeSeries = [{"label": "2026-07-06", "totalTimeListened": 0, "plays": 0}]

        result = dash._embedTimeSeriesTextElements(timeSeries, groupBy="day")

        self.assertIs(result, timeSeries)


if __name__ == "__main__":
    unittest.main()
