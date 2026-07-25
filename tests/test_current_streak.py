import sys
import os
import datetime
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase


def _ts(year, month, day, hour=0):
    """Unix timestamp (seconds) for a UTC datetime."""
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc).timestamp()


def _now(year, month, day, hour=12):
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc)


class TestCurrentStreak(DatabaseTestCase):
    def _db(self, entries):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        db = self._makeDb(tracks, entries)
        # Pin the timezone so the local play-date grouping is deterministic
        # regardless of the host's system timezone.
        db.tz = datetime.timezone.utc
        return db

    def test_no_plays_returns_zero_inactive(self):
        db = self._db([])
        result = db.getCurrentStreak(now=_now(2026, 1, 10))
        self.assertEqual(result, {"days": 0, "activeToday": False})

    def test_single_play_today_is_streak_one_active(self):
        db = self._db([{"id": "t1", "playedAt": _ts(2026, 1, 10, 9), "timePlayed": 1000}])
        result = db.getCurrentStreak(now=_now(2026, 1, 10))
        self.assertEqual(result, {"days": 1, "activeToday": True})

    def test_consecutive_days_including_today(self):
        db = self._db([
            {"id": "t1", "playedAt": _ts(2026, 1, 8, 9), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 9, 9), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 10, 9), "timePlayed": 1000},
        ])
        result = db.getCurrentStreak(now=_now(2026, 1, 10))
        self.assertEqual(result, {"days": 3, "activeToday": True})

    def test_streak_ending_yesterday_is_alive_but_inactive(self):
        # Played the two days ending yesterday, nothing yet today -> the
        # streak is still alive (can be continued) but not active today.
        db = self._db([
            {"id": "t1", "playedAt": _ts(2026, 1, 8, 9), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 9, 9), "timePlayed": 1000},
        ])
        result = db.getCurrentStreak(now=_now(2026, 1, 10))
        self.assertEqual(result, {"days": 2, "activeToday": False})

    def test_gap_of_two_days_breaks_streak(self):
        # Last play was two days ago -> streak is broken.
        db = self._db([
            {"id": "t1", "playedAt": _ts(2026, 1, 7, 9), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 8, 9), "timePlayed": 1000},
        ])
        result = db.getCurrentStreak(now=_now(2026, 1, 10))
        self.assertEqual(result, {"days": 0, "activeToday": False})

    def test_earlier_gap_does_not_extend_current_streak(self):
        # A long-ago run of days must not count; only the run touching
        # today/yesterday does.
        db = self._db([
            {"id": "t1", "playedAt": _ts(2026, 1, 1, 9), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 2, 9), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 3, 9), "timePlayed": 1000},
            # gap
            {"id": "t1", "playedAt": _ts(2026, 1, 9, 9), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 10, 9), "timePlayed": 1000},
        ])
        result = db.getCurrentStreak(now=_now(2026, 1, 10))
        self.assertEqual(result, {"days": 2, "activeToday": True})

    def test_multiple_plays_same_day_count_once(self):
        db = self._db([
            {"id": "t1", "playedAt": _ts(2026, 1, 10, 8), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 10, 20), "timePlayed": 1000},
        ])
        result = db.getCurrentStreak(now=_now(2026, 1, 10))
        self.assertEqual(result, {"days": 1, "activeToday": True})


class TestLongestStreakStillWorks(DatabaseTestCase):
    """The _getPlayDateSet extraction must not change getLongestStreak."""

    def test_longest_streak_unchanged_after_refactor(self):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 1, 5), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 6), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 8), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 9), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 10), "timePlayed": 1000},
        ]
        db = self._makeDb(tracks, entries)
        streak = db.getLongestStreak(
            startDate=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 2, 1, tzinfo=datetime.timezone.utc),
        )
        self.assertEqual(streak, 3)


class TestStreakLongerThanTheLookbackWindow(DatabaseTestCase):
    """The scan was hard-bounded at CURRENT_STREAK_LOOKBACK_DAYS (400), so the
    reported streak capped at ~401 - which made the 1000-day milestone
    (MILESTONE_STREAK_DAY_THRESHOLDS' top entry) unreachable no matter how long
    someone listened, and froze the dashboard's next-milestone bar at ~40%
    forever. The window now widens when the streak reaches its edge."""

    def _dbWithConsecutiveDays(self, dayCount, lastDay):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        entries = []
        for offset in range(dayCount):
            day = lastDay - datetime.timedelta(days=offset)
            entries.append({
                "id": "t1",
                "playedAt": datetime.datetime(day.year, day.month, day.day, 12,
                                               tzinfo=datetime.timezone.utc).timestamp(),
                "timePlayed": 1000,
            })
        db = self._makeDb(tracks, entries)
        db.tz = datetime.timezone.utc
        return db

    def test_a_streak_past_the_initial_window_is_not_capped(self):
        from Database.database import CURRENT_STREAK_LOOKBACK_DAYS

        lastDay = datetime.date(2026, 1, 10)
        dayCount = CURRENT_STREAK_LOOKBACK_DAYS + 50
        db = self._dbWithConsecutiveDays(dayCount, lastDay)

        result = db.getCurrentStreak(now=_now(2026, 1, 10))

        self.assertEqual(result["days"], dayCount)

    def test_a_streak_inside_the_window_still_stops_at_its_real_start(self):
        lastDay = datetime.date(2026, 1, 10)
        db = self._dbWithConsecutiveDays(5, lastDay)

        result = db.getCurrentStreak(now=_now(2026, 1, 10))

        self.assertEqual(result["days"], 5)

    def test_the_top_streak_milestone_is_reachable(self):
        """The threshold that was structurally unreachable."""
        from services.milestones import MILESTONE_STREAK_DAY_THRESHOLDS

        topThreshold = max(MILESTONE_STREAK_DAY_THRESHOLDS)
        lastDay = datetime.date(2026, 1, 10)
        db = self._dbWithConsecutiveDays(topThreshold, lastDay)

        result = db.getCurrentStreak(now=_now(2026, 1, 10))

        self.assertGreaterEqual(result["days"], topThreshold)


if __name__ == "__main__":
    unittest.main()
