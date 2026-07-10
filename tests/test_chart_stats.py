import datetime
import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# See tests/test_database_images.py for why this guard is needed: other test
# modules replace Database.database with a MagicMock at import time, and unittest
# discover imports every test file before running any of them.
if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.database import Database
import Database.utils as utilsModule


def _bareDatabaseWithData(tracks, entries):
    """A Database instance with just enough state for the chart-stats methods,
    skipping the heavy __init__ (autoimporter/listener setup) and file I/O by
    pre-seeding the in-memory caches directly."""
    db = Database.__new__(Database)
    db.fileLock = threading.RLock()
    db.tracksCache = tracks
    db.entriesCache = entries
    db.playlistsCache = None
    return db


def _ts(y, m, d, h=12, mi=0):
    """Unix timestamp (seconds) for a UTC datetime - entries store playedAt this way."""
    return int(datetime.datetime(y, m, d, h, mi, tzinfo=datetime.timezone.utc).timestamp())


class ChartStatsTestCase(unittest.TestCase):
    """All chart-stats tests fix the app's timezone to UTC so weekday/hour bucketing
    is deterministic regardless of the machine running the suite."""

    def setUp(self):
        patcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestGetListeningTimeSeries(ChartStatsTestCase):
    def test_daily_grouping_aggregates_same_day_entries(self):
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 7, 1, 9), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 7, 1, 20), "timePlayed": 2000},
            {"id": "t1", "playedAt": _ts(2026, 7, 2, 9), "timePlayed": 500},
        ]
        db = _bareDatabaseWithData({}, entries)

        result = db.getListeningTimeSeries(
            startDate=datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 7, 3, tzinfo=datetime.timezone.utc),
            groupBy="day",
        )

        byLabel = {b["label"]: b for b in result}
        self.assertEqual(byLabel["2026-07-01"]["totalTimeListened"], 3000)
        self.assertEqual(byLabel["2026-07-01"]["plays"], 2)
        self.assertEqual(byLabel["2026-07-02"]["totalTimeListened"], 500)

    def test_daily_grouping_fills_gaps_with_zero(self):
        entries = [{"id": "t1", "playedAt": _ts(2026, 7, 1), "timePlayed": 1000}]
        db = _bareDatabaseWithData({}, entries)

        result = db.getListeningTimeSeries(
            startDate=datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 7, 4, tzinfo=datetime.timezone.utc),
            groupBy="day",
        )

        self.assertEqual([b["label"] for b in result], ["2026-07-01", "2026-07-02", "2026-07-03"])
        self.assertEqual(result[1]["totalTimeListened"], 0)
        self.assertEqual(result[1]["plays"], 0)

    def test_weekly_grouping_aggregates_entries_in_same_week(self):
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 7, 6), "timePlayed": 1000},   # Monday
            {"id": "t1", "playedAt": _ts(2026, 7, 9), "timePlayed": 2000},   # Thursday, same week
            {"id": "t1", "playedAt": _ts(2026, 7, 13), "timePlayed": 500},   # next Monday
        ]
        db = _bareDatabaseWithData({}, entries)

        result = db.getListeningTimeSeries(
            startDate=datetime.datetime(2026, 7, 6, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 7, 14, tzinfo=datetime.timezone.utc),
            groupBy="week",
        )

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["label"], "2026-07-06")
        self.assertEqual(result[0]["totalTimeListened"], 3000)
        self.assertEqual(result[0]["plays"], 2)
        self.assertEqual(result[1]["label"], "2026-07-13")
        self.assertEqual(result[1]["totalTimeListened"], 500)

    def test_empty_entries_with_no_date_range_returns_empty_list(self):
        db = _bareDatabaseWithData({}, [])
        self.assertEqual(db.getListeningTimeSeries(), [])

    def test_no_date_range_infers_bounds_from_entries(self):
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 7, 1), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 7, 3), "timePlayed": 500},
        ]
        db = _bareDatabaseWithData({}, entries)

        result = db.getListeningTimeSeries(groupBy="day")

        self.assertEqual([b["label"] for b in result], ["2026-07-01", "2026-07-02", "2026-07-03"])


class TestGetHourOfDayHeatmap(ChartStatsTestCase):
    def test_buckets_by_weekday_and_hour(self):
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 7, 6, 9), "timePlayed": 1000},   # Monday 09:00
            {"id": "t1", "playedAt": _ts(2026, 7, 6, 9, 30), "timePlayed": 500},  # Monday 09:xx, same bucket
            {"id": "t1", "playedAt": _ts(2026, 7, 12, 23), "timePlayed": 2000},  # Sunday 23:00
        ]
        db = _bareDatabaseWithData({}, entries)

        grid = db.getHourOfDayHeatmap()

        self.assertEqual(len(grid), 7)
        self.assertEqual(len(grid[0]), 24)
        self.assertEqual(grid[0][9]["totalTimeListened"], 1500)  # Monday=0, hour 9
        self.assertEqual(grid[0][9]["plays"], 2)
        self.assertEqual(grid[6][23]["totalTimeListened"], 2000)  # Sunday=6, hour 23
        self.assertEqual(grid[0][0]["totalTimeListened"], 0)
        self.assertEqual(grid[0][0]["plays"], 0)

    def test_respects_date_range_filter(self):
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 7, 1, 9), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 8, 1, 9), "timePlayed": 5000},
        ]
        db = _bareDatabaseWithData({}, entries)

        grid = db.getHourOfDayHeatmap(
            startDate=datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 7, 2, tzinfo=datetime.timezone.utc),
        )

        totalAcrossGrid = sum(cell["totalTimeListened"] for row in grid for cell in row)
        self.assertEqual(totalAcrossGrid, 1000)

    def test_empty_database_returns_zeroed_grid(self):
        db = _bareDatabaseWithData({}, [])
        grid = db.getHourOfDayHeatmap()
        self.assertEqual(len(grid), 7)
        self.assertTrue(all(cell["plays"] == 0 for row in grid for cell in row))


class TestGetArtistTrend(ChartStatsTestCase):
    def _sampleData(self):
        artistA = {"name": "Artist A", "id": "a1"}
        artistB = {"name": "Artist B", "id": "a2"}
        artistC = {"name": "Artist C", "id": "a3"}
        tracks = {
            "song1": {"id": "song1", "name": "Song 1", "artists": [artistA]},
            "song2": {"id": "song2", "name": "Song 2", "artists": [artistB]},
            "song3": {"id": "song3", "name": "Song 3", "artists": [artistC]},
        }
        entries = [
            {"id": "song1", "playedAt": _ts(2026, 7, 6), "timePlayed": 1000},   # week 1, Artist A
            {"id": "song1", "playedAt": _ts(2026, 7, 7), "timePlayed": 1000},   # week 1, Artist A
            {"id": "song2", "playedAt": _ts(2026, 7, 6), "timePlayed": 1000},   # week 1, Artist B
            {"id": "song1", "playedAt": _ts(2026, 7, 13), "timePlayed": 1000},  # week 2, Artist A
            {"id": "song3", "playedAt": _ts(2026, 7, 13), "timePlayed": 1000},  # week 2, Artist C
        ]
        return tracks, entries

    def test_selects_top_n_artists_by_total_plays(self):
        tracks, entries = self._sampleData()
        db = _bareDatabaseWithData(tracks, entries)

        result = db.getArtistTrend(topN=2, groupBy="week")

        names = {series["name"] for series in result["series"]}
        self.assertEqual(names, {"Artist A", "Artist B"})  # A=3 plays, B=1, C=1 -> A and B win the tie-break by order

    def test_series_values_align_with_buckets(self):
        tracks, entries = self._sampleData()
        db = _bareDatabaseWithData(tracks, entries)

        result = db.getArtistTrend(topN=1, groupBy="week")

        self.assertEqual(result["buckets"], ["2026-07-06", "2026-07-13"])
        artistASeries = next(s for s in result["series"] if s["name"] == "Artist A")
        self.assertEqual(artistASeries["data"], [2, 1])

    def test_empty_entries_returns_empty_structure(self):
        db = _bareDatabaseWithData({}, [])
        result = db.getArtistTrend()
        self.assertEqual(result, {"buckets": [], "series": []})

    def test_skips_entries_with_missing_track_metadata(self):
        entries = [{"id": "ghost", "playedAt": _ts(2026, 7, 6), "timePlayed": 1000}]
        db = _bareDatabaseWithData({}, entries)
        db._saveNewTrackFromId = MagicMock(side_effect=Exception("no listener"))

        result = db.getArtistTrend()

        self.assertEqual(result, {"buckets": [], "series": []})


if __name__ == "__main__":
    unittest.main()
