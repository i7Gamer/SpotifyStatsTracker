"""Listening streak calendar grid builder (services/listening_calendar.py).

Pure layout logic tested against a plain {date: count} map - no DB - mirroring
how services/milestones.py is unit-tested. Database.getListeningCalendar gathers
the per-day counts and calls into this; that DB->grid wiring is covered in
test_chart_stats.py."""
import os
import sys
import datetime
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.listening_calendar import (
    buildListeningCalendar, _intensityLevel,
    CALENDAR_WEEKS, CALENDAR_INTENSITY_LEVELS, _DAYS_PER_WEEK,
)

# 2026-07-23 is a Thursday (weekday 3); the Monday of its week is 2026-07-20 and
# the Sunday is 2026-07-26.
_TODAY = datetime.date(2026, 7, 23)
_MONTH_ABBRS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class TestIntensityLevel(unittest.TestCase):
    def test_zero_count_is_level_zero(self):
        self.assertEqual(_intensityLevel(0, 100), 0)

    def test_zero_max_is_level_zero(self):
        self.assertEqual(_intensityLevel(5, 0), 0)

    def test_max_count_is_top_level(self):
        self.assertEqual(_intensityLevel(100, 100), CALENDAR_INTENSITY_LEVELS)

    def test_any_positive_count_is_at_least_level_one(self):
        self.assertEqual(_intensityLevel(1, 100000), 1)

    def test_buckets_scale_relative_to_max(self):
        self.assertEqual(_intensityLevel(25, 100), 1)
        self.assertEqual(_intensityLevel(50, 100), 2)
        self.assertEqual(_intensityLevel(75, 100), 3)
        self.assertEqual(_intensityLevel(100, 100), 4)


class TestBuildListeningCalendar(unittest.TestCase):
    def test_grid_dimensions(self):
        cal = buildListeningCalendar({}, _TODAY, weeks=53)
        self.assertEqual(len(cal["weeks"]), 53)
        for col in cal["weeks"]:
            self.assertEqual(len(col), _DAYS_PER_WEEK)

    def test_default_weeks_constant(self):
        cal = buildListeningCalendar({}, _TODAY)
        self.assertEqual(len(cal["weeks"]), CALENDAR_WEEKS)

    def test_rows_run_monday_to_sunday(self):
        cal = buildListeningCalendar({}, _TODAY, weeks=1)
        lastCol = cal["weeks"][-1]
        self.assertEqual(lastCol[0]["date"], "2026-07-20")   # Monday row
        self.assertEqual(lastCol[6]["date"], "2026-07-26")   # Sunday row

    def test_last_column_is_the_current_week(self):
        cal = buildListeningCalendar({}, _TODAY, weeks=4)
        self.assertEqual(cal["weeks"][-1][3]["date"], "2026-07-23")   # Thu = today
        # First column is 3 weeks earlier.
        self.assertEqual(cal["weeks"][0][0]["date"], "2026-06-29")

    def test_today_flag_and_future_cells(self):
        cal = buildListeningCalendar({}, _TODAY, weeks=1)
        lastCol = cal["weeks"][-1]
        self.assertTrue(lastCol[3]["today"])          # Thursday is today
        self.assertFalse(lastCol[3]["future"])
        self.assertFalse(lastCol[0]["future"])        # Mon..Thu already happened
        self.assertTrue(lastCol[4]["future"])         # Fri/Sat/Sun are ahead
        self.assertTrue(lastCol[6]["future"])

    def test_counts_and_levels_land_on_the_right_day(self):
        cal = buildListeningCalendar({"2026-07-20": 10, "2026-07-22": 5}, _TODAY, weeks=1)
        lastCol = cal["weeks"][-1]
        self.assertEqual((lastCol[0]["count"], lastCol[0]["level"]), (10, CALENDAR_INTENSITY_LEVELS))  # Mon, busiest
        self.assertEqual((lastCol[2]["count"], lastCol[2]["level"]), (5, 2))   # Wed, half of max
        self.assertEqual((lastCol[1]["count"], lastCol[1]["level"]), (0, 0))   # Tue, none

    def test_future_days_never_carry_counts(self):
        cal = buildListeningCalendar({"2026-07-26": 99}, _TODAY, weeks=1)   # Sunday is future
        sunday = cal["weeks"][-1][6]
        self.assertTrue(sunday["future"])
        self.assertEqual(sunday["count"], 0)
        self.assertEqual(sunday["level"], 0)

    def test_summary_totals(self):
        cal = buildListeningCalendar({"2026-07-20": 10, "2026-07-22": 5}, _TODAY, weeks=1)
        self.assertEqual(cal["activeDays"], 2)
        self.assertEqual(cal["totalPlays"], 15)
        self.assertEqual(cal["maxCount"], 10)

    def test_counts_outside_the_window_are_ignored(self):
        cal = buildListeningCalendar({"2020-01-01": 100}, _TODAY, weeks=1)
        self.assertEqual(cal["activeDays"], 0)
        self.assertEqual(cal["totalPlays"], 0)
        self.assertEqual(cal["maxCount"], 0)

    def test_month_labels_are_ordered_month_starts(self):
        cal = buildListeningCalendar({}, _TODAY, weeks=53)
        labels = cal["monthLabels"]
        self.assertTrue(labels)
        weekIdxs = [lbl["weekIndex"] for lbl in labels]
        self.assertEqual(weekIdxs, sorted(weekIdxs))            # left-to-right
        self.assertEqual(len(weekIdxs), len(set(weekIdxs)))     # one per column at most
        for lbl in labels:
            self.assertIn(lbl["label"], _MONTH_ABBRS)
            self.assertTrue(0 <= lbl["weekIndex"] < 53)


if __name__ == "__main__":
    unittest.main()
