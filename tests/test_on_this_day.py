import sys
import os
import datetime
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase


def _ts(year, month, day, hour=12, tz=datetime.timezone.utc):
    return datetime.datetime(year, month, day, hour, tzinfo=tz).timestamp()


def _now(year, month, day, hour=12):
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc)


class TestOnThisDay(DatabaseTestCase):
    def _db(self, tracks, entries, tz=datetime.timezone.utc):
        db = self._makeDb(tracks, entries)
        db.tz = tz
        return db

    def test_no_history_returns_empty(self):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        db = self._db(tracks, [{"id": "t1", "playedAt": _ts(2026, 5, 1), "timePlayed": 1000}])
        self.assertEqual(db.getOnThisDay(now=_now(2026, 1, 10)), [])

    def test_single_prior_year_match(self):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": [{"id": "a1", "name": "Artist 1"}]}}
        entries = [{"id": "t1", "playedAt": _ts(2024, 1, 10), "timePlayed": 1000}]
        db = self._db(tracks, entries)
        result = db.getOnThisDay(now=_now(2026, 1, 10))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["year"], 2024)
        self.assertEqual(result[0]["yearsAgo"], 2)
        self.assertEqual(result[0]["trackId"], "t1")
        self.assertEqual(result[0]["trackName"], "Song 1")
        self.assertEqual(result[0]["artistName"], "Artist 1")
        self.assertEqual(result[0]["playCount"], 1)

    def test_current_year_excluded(self):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        entries = [{"id": "t1", "playedAt": _ts(2026, 1, 10), "timePlayed": 1000}]
        db = self._db(tracks, entries)
        self.assertEqual(db.getOnThisDay(now=_now(2026, 1, 10)), [])

    def test_top_track_per_year_by_playcount(self):
        tracks = {
            "t1": {"id": "t1", "name": "Song 1", "artists": []},
            "t2": {"id": "t2", "name": "Song 2", "artists": []},
        }
        entries = [
            {"id": "t1", "playedAt": _ts(2024, 1, 10, 9), "timePlayed": 1000},
            {"id": "t2", "playedAt": _ts(2024, 1, 10, 10), "timePlayed": 1000},
            {"id": "t2", "playedAt": _ts(2024, 1, 10, 11), "timePlayed": 1000},
        ]
        db = self._db(tracks, entries)
        result = db.getOnThisDay(now=_now(2026, 1, 10))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["trackId"], "t2")
        self.assertEqual(result[0]["playCount"], 2)

    def test_years_sorted_descending_and_capped(self):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        entries = [
            {"id": "t1", "playedAt": _ts(2021, 1, 10), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2022, 1, 10), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2023, 1, 10), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2024, 1, 10), "timePlayed": 1000},
        ]
        db = self._db(tracks, entries)
        result = db.getOnThisDay(now=_now(2026, 1, 10), limit=2)
        self.assertEqual([r["year"] for r in result], [2024, 2023])

    def test_only_matching_month_day(self):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        entries = [
            {"id": "t1", "playedAt": _ts(2024, 1, 11), "timePlayed": 1000},  # wrong day
            {"id": "t1", "playedAt": _ts(2024, 2, 10), "timePlayed": 1000},  # wrong month
        ]
        db = self._db(tracks, entries)
        self.assertEqual(db.getOnThisDay(now=_now(2026, 1, 10)), [])

    def test_timezone_boundary(self):
        # Local tz UTC+2. A play at UTC 2024-01-09 23:00 is 2024-01-10 01:00
        # local, so it counts as "on this day" for a local Jan 10 today even
        # though its UTC calendar day is Jan 9.
        tz = datetime.timezone(datetime.timedelta(hours=2))
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        entries = [{"id": "t1", "playedAt": _ts(2024, 1, 9, 23, tz=datetime.timezone.utc), "timePlayed": 1000}]
        db = self._db(tracks, entries, tz=tz)
        result = db.getOnThisDay(now=datetime.datetime(2026, 1, 10, 12, tzinfo=tz))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["year"], 2024)


if __name__ == "__main__":
    unittest.main()
