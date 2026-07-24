import sys
import os
import datetime
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase


def _ts(year, month, day, hour=0):
    """Unix timestamp (seconds) for a UTC datetime."""
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc).timestamp()


class TestLongestStreak(DatabaseTestCase):
    def test_single_day_has_streak_of_one(self):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        entries = [{"id": "t1", "playedAt": _ts(2026, 1, 5), "timePlayed": 1000}]
        db = self._makeDb(tracks, entries)

        streak = db.getLongestStreak(
            startDate=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 2, 1, tzinfo=datetime.timezone.utc),
        )

        self.assertEqual(streak, 1)

    def test_consecutive_days_count_as_one_streak(self):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 1, 5, 10), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 6, 14), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 7, 8), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 8, 20), "timePlayed": 1000},
        ]
        db = self._makeDb(tracks, entries)

        streak = db.getLongestStreak(
            startDate=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 2, 1, tzinfo=datetime.timezone.utc),
        )

        self.assertEqual(streak, 4)

    def test_gap_in_plays_breaks_streak(self):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 1, 5), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 6), "timePlayed": 1000},
            # Gap on 1/7
            {"id": "t1", "playedAt": _ts(2026, 1, 8), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 9), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 10), "timePlayed": 1000},
        ]
        db = self._makeDb(tracks, entries)

        streak = db.getLongestStreak(
            startDate=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 2, 1, tzinfo=datetime.timezone.utc),
        )

        # Should be the longer streak (3 days) not the first one (2 days)
        self.assertEqual(streak, 3)

    def test_no_plays_returns_zero(self):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        entries = []
        db = self._makeDb(tracks, entries)

        streak = db.getLongestStreak(
            startDate=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 2, 1, tzinfo=datetime.timezone.utc),
        )

        self.assertEqual(streak, 0)


class TestPeakListeningTime(DatabaseTestCase):
    def test_returns_day_with_most_plays(self):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        # Monday: 3 plays
        # Wednesday: 5 plays (peak)
        # Friday: 2 plays
        entries = [
            # Monday 2026-01-05
            {"id": "t1", "playedAt": _ts(2026, 1, 5, 10), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 5, 14), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 5, 18), "timePlayed": 1000},
            # Wednesday 2026-01-07
            {"id": "t1", "playedAt": _ts(2026, 1, 7, 8), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 7, 10), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 7, 12), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 7, 14), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 7, 16), "timePlayed": 1000},
            # Friday 2026-01-09
            {"id": "t1", "playedAt": _ts(2026, 1, 9, 20), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 9, 22), "timePlayed": 1000},
        ]
        db = self._makeDb(tracks, entries)

        day_name, play_count = db.getPeakListeningTime(
            startDate=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 2, 1, tzinfo=datetime.timezone.utc),
        )

        self.assertEqual(day_name, "Wednesday")
        self.assertEqual(play_count, 5)

    def test_no_plays_returns_none(self):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        entries = []
        db = self._makeDb(tracks, entries)

        result = db.getPeakListeningTime(
            startDate=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 2, 1, tzinfo=datetime.timezone.utc),
        )

        self.assertIsNone(result)


class TestDiscoveredCounts(DatabaseTestCase):
    def test_discovered_songs_count_in_year(self):
        tracks = {
            "t1": {"id": "t1", "name": "Old Song", "artists": []},
            "t2": {"id": "t2", "name": "New Song", "artists": []},
            "t3": {"id": "t3", "name": "Another New", "artists": []},
        }
        entries = [
            # t1 first played in 2024
            {"id": "t1", "playedAt": _ts(2024, 6, 15), "timePlayed": 1000},
            # t2 and t3 first played in 2026
            {"id": "t2", "playedAt": _ts(2026, 3, 10), "timePlayed": 1000},
            {"id": "t3", "playedAt": _ts(2026, 5, 20), "timePlayed": 1000},
            # t1 played again in 2026 (but not a discovery)
            {"id": "t1", "playedAt": _ts(2026, 7, 1), "timePlayed": 1000},
        ]
        db = self._makeDb(tracks, entries)

        song_count = db.getDiscoveredSongsCount(
            startDate=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2027, 1, 1, tzinfo=datetime.timezone.utc),
        )

        self.assertEqual(song_count, 2)

    def test_discovered_artists_count_in_year(self):
        tracks = {
            "t1": {"id": "t1", "name": "Song 1", "artists": [{"id": "a1", "name": "Old Artist"}]},
            "t2": {"id": "t2", "name": "Song 2", "artists": [{"id": "a2", "name": "New Artist"}]},
            "t3": {"id": "t3", "name": "Song 3", "artists": [{"id": "a3", "name": "Another New"}]},
        }
        entries = [
            {"id": "t1", "playedAt": _ts(2024, 6, 15), "timePlayed": 1000},
            {"id": "t2", "playedAt": _ts(2026, 3, 10), "timePlayed": 1000},
            {"id": "t3", "playedAt": _ts(2026, 5, 20), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 7, 1), "timePlayed": 1000},
        ]
        db = self._makeDb(tracks, entries)

        artist_count = db.getDiscoveredArtistsCount(
            startDate=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2027, 1, 1, tzinfo=datetime.timezone.utc),
        )

        self.assertEqual(artist_count, 2)

    def test_a_play_exactly_on_the_range_end_is_not_double_counted(self):
        """The count uses a half-open [start, end) - a first play landing exactly
        on the range end (next Jan 1 midnight, the value callers pass) must count
        in the LATER year only, matching the discovered lists' strict `< end`.
        The old closed BETWEEN counted such a play in both years."""
        boundary = datetime.datetime(2027, 1, 1, tzinfo=datetime.timezone.utc)
        tracks = {"t1": {"id": "t1", "name": "Boundary Song", "artists": [{"id": "a1", "name": "Boundary Artist"}]}}
        entries = [{"id": "t1", "playedAt": boundary.timestamp(), "timePlayed": 1000}]
        db = self._makeDb(tracks, entries)

        y2026 = (datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc), boundary)
        y2027 = (boundary, datetime.datetime(2028, 1, 1, tzinfo=datetime.timezone.utc))

        self.assertEqual(db.getDiscoveredSongsCount(startDate=y2026[0], endDate=y2026[1]), 0)
        self.assertEqual(db.getDiscoveredArtistsCount(startDate=y2026[0], endDate=y2026[1]), 0)
        self.assertEqual(db.getDiscoveredSongsCount(startDate=y2027[0], endDate=y2027[1]), 1)
        self.assertEqual(db.getDiscoveredArtistsCount(startDate=y2027[0], endDate=y2027[1]), 1)


if __name__ == "__main__":
    unittest.main()
