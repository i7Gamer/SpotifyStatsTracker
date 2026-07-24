"""An unrecognized ?interval= (e.g. a hand-edited URL) must fall back to the
route's default window, not silently resolve to ALL-TIME data under a mislabeled
heading. _getDateRange and _getIntervalLabel now both coerce junk to `default`
so the data and the label can't disagree (2026-07-24 review, item 1).
"""
import os
import sys
import unittest
from datetime import timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import makeApp


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


if __name__ == "__main__":
    unittest.main()
