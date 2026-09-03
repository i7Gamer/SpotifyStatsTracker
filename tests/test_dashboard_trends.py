"""Dashboard trend-card subtitle formatters (services/dashboard_trends.py).

Pure string formatting tested with hand-computed oracles - no DB. The DB-side
wiring (Database.getDashboardTrends fetching + hydrating the raw rows and
calling these) is covered by tests/test_trends.py, which is the net for this
extraction (a property test needs an oracle recomputed a different way, not
the formula under test read back at itself)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.dashboard_trends import (
    obsessionSubtitle, rediscoverySubtitle, freshFindSubtitle, forgottenSubtitle,
)

_DAY = 86400
_NOW = 1_800_000_000.0   #< an arbitrary fixed "now" so every test's math is by hand, not wall-clock


class TestObsessionSubtitle(unittest.TestCase):
    def test_singular_play(self):
        self.assertEqual(obsessionSubtitle({"recent_count": 1}), "1 play in the past week")

    def test_plural_plays(self):
        self.assertEqual(obsessionSubtitle({"recent_count": 7}), "7 plays in the past week")

    def test_zero_is_plural(self):
        self.assertEqual(obsessionSubtitle({"recent_count": 0}), "0 plays in the past week")


class TestRediscoverySubtitle(unittest.TestCase):
    def test_days_unplayed_is_floored_at_one(self):
        # max_old_played_at 12 hours before now -> under a day, floored to 1.
        item = {"recent_count": 3, "max_old_played_at": _NOW - (12 * 3600)}
        self.assertEqual(rediscoverySubtitle(item, _NOW),
                         "3 plays this week · unplayed for 1 days")

    def test_exact_day_count(self):
        item = {"recent_count": 2, "max_old_played_at": _NOW - (10 * _DAY)}
        self.assertEqual(rediscoverySubtitle(item, _NOW),
                         "2 plays this week · unplayed for 10 days")

    def test_singular_play_count(self):
        item = {"recent_count": 1, "max_old_played_at": _NOW - (5 * _DAY)}
        self.assertEqual(rediscoverySubtitle(item, _NOW),
                         "1 play this week · unplayed for 5 days")

    def test_missing_max_old_played_at_reads_zero_days(self):
        item = {"recent_count": 4, "max_old_played_at": None}
        self.assertEqual(rediscoverySubtitle(item, _NOW),
                         "4 plays this week · unplayed for 0 days")


class TestFreshFindSubtitle(unittest.TestCase):
    def test_first_heard_today_when_under_a_day_old(self):
        item = {"play_count": 2, "first_played_at": _NOW - (4 * 3600)}   #< 4 hours ago
        self.assertEqual(freshFindSubtitle(item, _NOW), "2 plays · first heard today")

    def test_first_heard_one_day_ago_is_singular(self):
        item = {"play_count": 3, "first_played_at": _NOW - (1 * _DAY)}
        self.assertEqual(freshFindSubtitle(item, _NOW), "3 plays · first heard 1 day ago")

    def test_first_heard_several_days_ago_is_plural(self):
        item = {"play_count": 5, "first_played_at": _NOW - (5 * _DAY)}
        self.assertEqual(freshFindSubtitle(item, _NOW), "5 plays · first heard 5 days ago")

    def test_missing_first_played_at_reads_today(self):
        """Falls to 0 (not the >=1 floor rediscovery/forgotten use) - the only
        one of the three cards floored at 0, since its 14-day window against a
        two-play bar makes "found it this morning" the ordinary case."""
        item = {"play_count": 1, "first_played_at": None}
        self.assertEqual(freshFindSubtitle(item, _NOW), "1 play · first heard today")


class TestForgottenSubtitle(unittest.TestCase):
    def test_months_ago_is_floored_at_one(self):
        # last_played_at 10 days ago -> under GAP_DAYS_PER_MONTH (30), floored to 1.
        item = {"total_plays": 40, "last_played_at": _NOW - (10 * _DAY)}
        self.assertEqual(forgottenSubtitle(item, _NOW),
                         "40 full plays all-time · last played 1 month ago")

    def test_several_months_ago(self):
        item = {"total_plays": 12, "last_played_at": _NOW - (95 * _DAY)}   #< 95 // 30 = 3
        self.assertEqual(forgottenSubtitle(item, _NOW),
                         "12 full plays all-time · last played 3 months ago")

    def test_total_plays_is_never_pluralized_away_from_plays(self):
        """Unlike the other three cards' play counts, total_plays' word is a
        fixed "plays" in the original string - even at 1."""
        item = {"total_plays": 1, "last_played_at": _NOW - (60 * _DAY)}
        self.assertEqual(forgottenSubtitle(item, _NOW),
                         "1 full plays all-time · last played 2 months ago")

    def test_missing_last_played_at_reads_zero_days_one_month(self):
        item = {"total_plays": 8, "last_played_at": None}
        self.assertEqual(forgottenSubtitle(item, _NOW),
                         "8 full plays all-time · last played 1 month ago")
