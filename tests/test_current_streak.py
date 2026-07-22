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


if __name__ == "__main__":
    unittest.main()
