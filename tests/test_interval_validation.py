"""An unrecognized ?interval= (e.g. a hand-edited URL) must fall back to the
route's default window, not silently resolve to ALL-TIME data under a mislabeled
heading. _getDateRange and _getIntervalLabel now both coerce junk to `default`
so the data and the label can't disagree (2026-07-24 review, item 1).
"""
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import makeApp
import dashboard.date_ranges as dateRangesModule

# Frozen "now" so two _getDateRange calls in one test can be compared exactly -
# the relative windows ("week" = now - 7 days) otherwise differ by microseconds.
_FROZEN_NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def _frozenNow():
    return patch.object(dateRangesModule, "now", return_value=_FROZEN_NOW)


class TestIntervalValidation(unittest.TestCase):
    def test_junk_interval_falls_back_to_default_not_all_time(self):
        dash = makeApp()
        start, end = dash._getDateRange("week2", default="day", tz=timezone.utc)
        # All-time is (None, None); the default "day" window is a real range.
        self.assertIsNotNone(start)
        self.assertIsNotNone(end)

    def test_all_time_still_returns_an_open_range(self):
        dash = makeApp()
        start, end = dash._getDateRange("all time", default="day", tz=timezone.utc)
        self.assertIsNone(start)
        self.assertIsNone(end)

    def test_known_intervals_are_unchanged(self):
        dash = makeApp()
        start, end = dash._getDateRange("week", default="day", tz=timezone.utc)
        self.assertIsNotNone(start)
        self.assertIsNotNone(end)

    def test_label_of_junk_interval_matches_the_default(self):
        dash = makeApp()
        self.assertEqual(dash._getIntervalLabel("week2", default="day"), "Yesterday")
        self.assertEqual(dash._getIntervalLabel("week2", default="week"), "Last Week")

    def test_label_of_known_intervals_unchanged(self):
        dash = makeApp()
        self.assertEqual(dash._getIntervalLabel("all time"), "All Time")
        self.assertEqual(dash._getIntervalLabel("month"), "Last Month")


class TestCustomDatesOnlyApplyToTheCustomInterval(unittest.TestCase):
    """Custom dates used to win over any named interval, and routes only guard
    the inverse (custom with no dates). A stale or shared URL carrying both -
    ?interval=week&startDate=2020-01-01&endDate=2020-01-02 - therefore rendered
    2020's data under a "Last Week" heading."""

    def test_named_interval_ignores_stray_custom_dates(self):
        dash = makeApp()

        with _frozenNow():
            withCustom = dash._getDateRange("week", customStart="2020-01-01", customEnd="2020-01-02",
                                             default="day", tz=timezone.utc)
            withoutCustom = dash._getDateRange("week", default="day", tz=timezone.utc)

        self.assertEqual(withCustom, withoutCustom)

    def test_custom_interval_still_honours_its_dates(self):
        dash = makeApp()

        start, end = dash._getDateRange("custom", customStart="2020-01-01", customEnd="2020-01-02",
                                         default="day", tz=timezone.utc)

        self.assertEqual(start.date().isoformat(), "2020-01-01")
        self.assertEqual(end.date().isoformat(), "2020-01-03")   #< half-open: end date + 1 day

    def test_label_ignores_stray_custom_dates_on_a_named_interval(self):
        dash = makeApp()

        label = dash._getIntervalLabel("week", customStart="2020-01-01", customEnd="2020-01-02")

        self.assertEqual(label, "Last Week")


class TestUnparseableCustomDates(unittest.TestCase):
    """An unparseable custom range used to fall through to the all-time branch,
    showing a user's ENTIRE history under a "Custom range: x to y" label."""

    def test_falls_back_to_the_default_window_not_all_time(self):
        dash = makeApp()

        with _frozenNow():
            start, end = dash._getDateRange("custom", customStart="not-a-date", customEnd="also-bad",
                                             default="week", tz=timezone.utc)
            expected = dash._getDateRange("week", default="week", tz=timezone.utc)

        self.assertIsNotNone(start)   #< all-time would be (None, None)
        self.assertIsNotNone(end)
        self.assertEqual((start, end), expected)

    def test_label_does_not_promise_a_range_the_data_lacks(self):
        dash = makeApp()

        label = dash._getIntervalLabel("custom", customStart="not-a-date", customEnd="also-bad",
                                        default="week")

        self.assertEqual(label, "Last Week")

    def test_an_empty_interval_labels_as_the_default_not_yesterday(self):
        """_getDateRange maps "" to `default`; the label mapped it to "day" and
        rendered "Yesterday". "" is deliberately a valid interval, so a
        hand-edited or bookmarked ?interval= reached both - all-time data under
        a "Yesterday" heading."""
        dash = makeApp()

        self.assertEqual(dash._getIntervalLabel("", default="all time"), "All Time")
        self.assertEqual(dash._getIntervalLabel("", default="month"), "Last Month")

    def test_an_absent_interval_still_labels_as_the_default(self):
        dash = makeApp()

        self.assertEqual(dash._getIntervalLabel(None, default="week"), "Last Week")

    def test_valid_custom_dates_still_label_as_a_custom_range(self):
        dash = makeApp()

        label = dash._getIntervalLabel("custom", customStart="2020-01-01", customEnd="2020-01-02")

        self.assertEqual(label, "Custom range: 2020-01-01 to 2020-01-02")


if __name__ == "__main__":
    unittest.main()
