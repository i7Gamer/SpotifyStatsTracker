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


class TestSkipOnlyDaysAreNotListeningDays(DatabaseTestCase):
    """A day whose only activity was a skip is not a listening day.

    getBucketedPlayTotals stopped filtering is_skip=0 in the WHERE (so a
    skip-only track's detail chart could render), and _getPlayDateSet derived
    its date set from the mere EXISTENCE of a bucket row - so a bucket holding
    nothing but skips started counting as a day of listening. The streak card
    then disagreed with the contribution calendar rendered directly beside it,
    which counts a day only when plays > 0.
    """

    SHORT_MS = 4_000   #< under the default 5s threshold -> classified as a skip

    def _db(self, entries):
        # conftest's helper inserts every play with is_skip=0 (insertPlay never
        # classifies - its callers do), so run the real classifier over the
        # seeded rows rather than setting the column by hand.
        db = self._makeDb({"t1": {"id": "t1", "name": "Song 1", "artists": []}}, entries)
        db.repo.recomputeSkipFlags()
        db.tz = datetime.timezone.utc
        return db

    def _play(self, year, month, day, ms):
        return {"id": "t1", "playedAt": _ts(year, month, day, 9), "timePlayed": ms}

    def test_a_skip_today_does_not_make_today_active(self):
        db = self._db([
            self._play(2026, 1, 8, 60_000),
            self._play(2026, 1, 9, 60_000),
            self._play(2026, 1, 10, self.SHORT_MS),   #< today: skipped, nothing else
        ])

        result = db.getCurrentStreak(now=_now(2026, 1, 10))

        # The run ending yesterday is still alive, but today is not active and
        # must not count as its third day.
        self.assertEqual(result, {"days": 2, "activeToday": False})

    def test_a_skip_only_day_does_not_bridge_a_broken_streak(self):
        db = self._db([
            self._play(2026, 1, 8, 60_000),
            self._play(2026, 1, 9, self.SHORT_MS),   #< skip-only day between two runs
            self._play(2026, 1, 10, 60_000),
        ])

        result = db.getCurrentStreak(now=_now(2026, 1, 10))

        self.assertEqual(result, {"days": 1, "activeToday": True})

    def test_a_history_of_nothing_but_skips_is_no_streak(self):
        db = self._db([
            self._play(2026, 1, 9, self.SHORT_MS),
            self._play(2026, 1, 10, self.SHORT_MS),
        ])

        result = db.getCurrentStreak(now=_now(2026, 1, 10))

        self.assertEqual(result, {"days": 0, "activeToday": False})

    def test_longest_streak_is_not_bridged_by_a_skip_only_day(self):
        db = self._db([
            self._play(2026, 1, 5, 60_000),
            self._play(2026, 1, 6, self.SHORT_MS),   #< skip-only
            self._play(2026, 1, 7, 60_000),
        ])

        streak = db.getLongestStreak(
            startDate=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 2, 1, tzinfo=datetime.timezone.utc),
        )

        self.assertEqual(streak, 1)

    def test_a_day_with_both_a_play_and_a_skip_still_counts(self):
        """The narrowing must not go the other way: a real listen on a day that
        also had a skip is still a listening day."""
        db = self._db([
            {"id": "t1", "playedAt": _ts(2026, 1, 10, 9), "timePlayed": self.SHORT_MS},
            {"id": "t1", "playedAt": _ts(2026, 1, 10, 10), "timePlayed": 60_000},
        ])

        result = db.getCurrentStreak(now=_now(2026, 1, 10))

        self.assertEqual(result, {"days": 1, "activeToday": True})


class TestStreakLongerThanTheLookbackWindow(DatabaseTestCase):
    """The scan was hard-bounded at CURRENT_STREAK_LOOKBACK_DAYS (400), so the
    reported streak capped at ~401 - which made the 1000-day milestone
    (MILESTONE_STREAK_DAY_THRESHOLDS' top entry) unreachable no matter how long
    someone listened, and froze the dashboard's next-milestone bar at ~40%
    forever. The window now widens when the streak reaches its edge."""

    # Plays seed earlier in the day than _now()'s default hour (12): the old
    # window-start (`nowLocal - timedelta(days=lookbackDays)`) matched the
    # play's own clock time when both used the same hour, which pinned
    # nothing - the boundary-day play landed exactly ON the (buggy) window
    # start instead of testing whether an EARLIER-in-the-day play falls
    # outside it. See TestStreakWindowStartIsLocalMidnight below for the
    # regression test that exercises the actual clock-time trap.
    SEED_HOUR = 6

    def _dbWithConsecutiveDays(self, dayCount, lastDay, hour=SEED_HOUR):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        entries = []
        for offset in range(dayCount):
            day = lastDay - datetime.timedelta(days=offset)
            entries.append({
                "id": "t1",
                "playedAt": datetime.datetime(day.year, day.month, day.day, hour,
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


class TestStreakWindowStartIsLocalMidnight(DatabaseTestCase):
    """The lookback window used to start at `now`'s exact clock time N days
    back (`nowLocal - timedelta(days=lookbackDays)`) instead of local
    midnight of the boundary day. A play earlier in that day than `now`'s
    clock time then fell OUTSIDE the fetched window, so _streakFillsWindow
    never saw the run reach the window's edge, the loop never widened, and
    the reported streak silently capped at CURRENT_STREAK_LOOKBACK_DAYS even
    though the real streak went back further."""

    def _dbWithConsecutiveDays(self, dayCount, lastDay, hour):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        entries = []
        for offset in range(dayCount):
            day = lastDay - datetime.timedelta(days=offset)
            entries.append({
                "id": "t1",
                "playedAt": datetime.datetime(day.year, day.month, day.day, hour,
                                               tzinfo=datetime.timezone.utc).timestamp(),
                "timePlayed": 1000,
            })
        db = self._makeDb(tracks, entries)
        db.tz = datetime.timezone.utc
        return db

    def test_plays_earlier_than_nows_clock_time_are_not_capped(self):
        from Database.database import CURRENT_STREAK_LOOKBACK_DAYS

        dayCount = CURRENT_STREAK_LOOKBACK_DAYS + 50   # 450: past the initial window
        lastDay = datetime.date(2026, 1, 10)
        # Every play at 08:00 local; asked at 18:00 local - the boundary
        # day's play is earlier in the day than `now`'s clock time, which is
        # exactly what the old window-start excluded.
        db = self._dbWithConsecutiveDays(dayCount, lastDay, hour=8)

        result = db.getCurrentStreak(now=_now(2026, 1, 10, hour=18))

        self.assertEqual(result["days"], dayCount)

    def test_plays_later_than_nows_clock_time_control(self):
        from Database.database import CURRENT_STREAK_LOOKBACK_DAYS

        dayCount = CURRENT_STREAK_LOOKBACK_DAYS + 50
        lastDay = datetime.date(2026, 1, 10)
        # Control: plays at 20:00, asked at 18:00 - later in the day than
        # `now`'s clock time, so even the old buggy window-start included
        # the boundary day. This must stay 450 both before and after the fix.
        db = self._dbWithConsecutiveDays(dayCount, lastDay, hour=20)

        result = db.getCurrentStreak(now=_now(2026, 1, 10, hour=18))

        self.assertEqual(result["days"], dayCount)


if __name__ == "__main__":
    unittest.main()
