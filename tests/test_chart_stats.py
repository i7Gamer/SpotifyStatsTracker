import datetime
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
import Database.utils as utilsModule


def _ts(y, m, d, h=12, mi=0):
    """Unix timestamp (seconds) for a UTC datetime - entries store playedAt this way."""
    return int(datetime.datetime(y, m, d, h, mi, tzinfo=datetime.timezone.utc).timestamp())


class ChartStatsTestCase(DatabaseTestCase):
    """All chart-stats tests fix the app's timezone to UTC so weekday/hour bucketing
    is deterministic regardless of the machine running the suite."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestGetListeningTimeSeries(ChartStatsTestCase):
    def test_daily_grouping_aggregates_same_day_entries(self):
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 7, 1, 9), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 7, 1, 20), "timePlayed": 2000},
            {"id": "t1", "playedAt": _ts(2026, 7, 2, 9), "timePlayed": 1500},
        ]
        db = self._makeDb({}, entries)

        result = db.getListeningTimeSeries(
            startDate=datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 7, 3, tzinfo=datetime.timezone.utc),
            groupBy="day",
        )

        byLabel = {b["label"]: b for b in result}
        self.assertEqual(byLabel["2026-07-01"]["totalTimeListened"], 3000)
        self.assertEqual(byLabel["2026-07-01"]["plays"], 2)
        self.assertEqual(byLabel["2026-07-02"]["totalTimeListened"], 1500)

    def test_daily_grouping_fills_gaps_with_zero(self):
        entries = [{"id": "t1", "playedAt": _ts(2026, 7, 1), "timePlayed": 1000}]
        db = self._makeDb({}, entries)

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
            {"id": "t1", "playedAt": _ts(2026, 7, 13), "timePlayed": 1500},
        ]
        db = self._makeDb({}, entries)

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
        self.assertEqual(result[1]["totalTimeListened"], 1500)

    def test_monthly_grouping_aggregates_entries_in_same_month(self):
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 1, 5), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 20), "timePlayed": 2000},
            {"id": "t1", "playedAt": _ts(2026, 2, 3), "timePlayed": 1500},
        ]
        db = self._makeDb({}, entries)

        result = db.getListeningTimeSeries(
            startDate=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc),
            groupBy="month",
        )

        byLabel = {b["label"]: b for b in result}
        self.assertEqual(byLabel["2026-01"]["totalTimeListened"], 3000)
        self.assertEqual(byLabel["2026-01"]["plays"], 2)
        self.assertEqual(byLabel["2026-02"]["totalTimeListened"], 1500)

    def test_monthly_grouping_fills_gaps_including_short_february(self):
        entries = [{"id": "t1", "playedAt": _ts(2026, 1, 15), "timePlayed": 1000}]
        db = self._makeDb({}, entries)

        result = db.getListeningTimeSeries(
            startDate=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 4, 1, tzinfo=datetime.timezone.utc),
            groupBy="month",
        )

        self.assertEqual([b["label"] for b in result], ["2026-01", "2026-02", "2026-03"])
        self.assertEqual(result[1]["totalTimeListened"], 0)
        self.assertEqual(result[1]["plays"], 0)

    def test_monthly_grouping_handles_year_rollover(self):
        entries = [
            {"id": "t1", "playedAt": _ts(2025, 12, 15), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 5), "timePlayed": 1500},
        ]
        db = self._makeDb({}, entries)

        result = db.getListeningTimeSeries(
            startDate=datetime.datetime(2025, 12, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 2, 1, tzinfo=datetime.timezone.utc),
            groupBy="month",
        )

        self.assertEqual([b["label"] for b in result], ["2025-12", "2026-01"])
        self.assertEqual(result[0]["totalTimeListened"], 1000)
        self.assertEqual(result[1]["totalTimeListened"], 1500)

    def test_empty_entries_with_no_date_range_returns_empty_list(self):
        db = self._makeDb({}, [])
        self.assertEqual(db.getListeningTimeSeries(), [])

    def test_no_date_range_infers_bounds_from_entries(self):
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 7, 1), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 7, 3), "timePlayed": 1500},
        ]
        db = self._makeDb({}, entries)

        result = db.getListeningTimeSeries(groupBy="day")

        self.assertEqual([b["label"] for b in result], ["2026-07-01", "2026-07-02", "2026-07-03"])

    def test_track_id_filter_scopes_to_one_track(self):
        """Detail pages reuse this exact method (and renderTimeSeriesChart on the
        frontend) to show one song/artist/album's own play history."""
        tracks = {
            "t1": {"id": "t1", "name": "Song One", "artists": [{"id": "a1", "name": "Artist A"}]},
            "t2": {"id": "t2", "name": "Song Two", "artists": [{"id": "a1", "name": "Artist A"}]},
        }
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 7, 1), "timePlayed": 1000},
            {"id": "t2", "playedAt": _ts(2026, 7, 1), "timePlayed": 5000},
        ]
        db = self._makeDb(tracks, entries)

        result = db.getListeningTimeSeries(
            startDate=datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 7, 2, tzinfo=datetime.timezone.utc),
            groupBy="day", trackId="t1",
        )

        self.assertEqual(result[0]["totalTimeListened"], 1000)
        self.assertEqual(result[0]["plays"], 1)

    def test_artist_id_filter_scopes_to_one_artist(self):
        tracks = {
            "t1": {"id": "t1", "name": "Song One", "artists": [{"id": "a1", "name": "Artist A"}]},
            "t2": {"id": "t2", "name": "Song Two", "artists": [{"id": "a2", "name": "Artist B"}]},
        }
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 7, 1), "timePlayed": 1000},
            {"id": "t2", "playedAt": _ts(2026, 7, 1), "timePlayed": 5000},
        ]
        db = self._makeDb(tracks, entries)

        result = db.getListeningTimeSeries(
            startDate=datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 7, 2, tzinfo=datetime.timezone.utc),
            groupBy="day", artistId="a1",
        )

        self.assertEqual(result[0]["totalTimeListened"], 1000)

    def test_album_id_filter_scopes_to_one_album(self):
        tracks = {
            "t1": {"id": "t1", "name": "Song One", "artists": [],
                   "imageId": "alb1", "album": {"id": "alb1", "name": "Album One", "url": "u",
                                                 "imageId": "alb1", "imageUrl": "", "totalTracks": 1,
                                                 "releaseDate": 0}},
            "t2": {"id": "t2", "name": "Song Two", "artists": [],
                   "imageId": "alb2", "album": {"id": "alb2", "name": "Album Two", "url": "u",
                                                 "imageId": "alb2", "imageUrl": "", "totalTracks": 1,
                                                 "releaseDate": 0}},
        }
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 7, 1), "timePlayed": 1000},
            {"id": "t2", "playedAt": _ts(2026, 7, 1), "timePlayed": 5000},
        ]
        db = self._makeDb(tracks, entries)

        result = db.getListeningTimeSeries(
            startDate=datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 7, 2, tzinfo=datetime.timezone.utc),
            groupBy="day", albumId="alb1",
        )

        self.assertEqual(result[0]["totalTimeListened"], 1000)


class TestGetHourOfDayHeatmap(ChartStatsTestCase):
    def test_buckets_by_weekday_and_hour(self):
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 7, 6, 9), "timePlayed": 1000},   # Monday 09:00
            {"id": "t1", "playedAt": _ts(2026, 7, 6, 9, 30), "timePlayed": 1500},  # Monday 09:xx, same bucket
            {"id": "t1", "playedAt": _ts(2026, 7, 12, 23), "timePlayed": 2000},  # Sunday 23:00
        ]
        db = self._makeDb({}, entries)

        grid = db.getHourOfDayHeatmap()

        self.assertEqual(len(grid), 7)
        self.assertEqual(len(grid[0]), 24)
        self.assertEqual(grid[0][9]["totalTimeListened"], 2500)  # Monday=0, hour 9
        self.assertEqual(grid[0][9]["plays"], 2)
        self.assertEqual(grid[6][23]["totalTimeListened"], 2000)  # Sunday=6, hour 23
        self.assertEqual(grid[0][0]["totalTimeListened"], 0)
        self.assertEqual(grid[0][0]["plays"], 0)

    def test_respects_date_range_filter(self):
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 7, 1, 9), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 8, 1, 9), "timePlayed": 5000},
        ]
        db = self._makeDb({}, entries)

        grid = db.getHourOfDayHeatmap(
            startDate=datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 7, 2, tzinfo=datetime.timezone.utc),
        )

        totalAcrossGrid = sum(cell["totalTimeListened"] for row in grid for cell in row)
        self.assertEqual(totalAcrossGrid, 1000)

    def test_empty_database_returns_zeroed_grid(self):
        db = self._makeDb({}, [])
        grid = db.getHourOfDayHeatmap()
        self.assertEqual(len(grid), 7)
        self.assertTrue(all(cell["plays"] == 0 for row in grid for cell in row))

    def test_track_id_filter_scopes_to_one_track(self):
        """The song detail subpage reuses this to show a 'when you listen to
        this song' heatmap scoped to just its own plays."""
        tracks = {
            "t1": {"id": "t1", "name": "Song One", "artists": [{"id": "a1", "name": "Artist A"}]},
            "t2": {"id": "t2", "name": "Song Two", "artists": [{"id": "a1", "name": "Artist A"}]},
        }
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 7, 6, 9), "timePlayed": 1000},   # Monday 09:00
            {"id": "t2", "playedAt": _ts(2026, 7, 6, 9), "timePlayed": 5000},   # Monday 09:00, different track
        ]
        db = self._makeDb(tracks, entries)

        grid = db.getHourOfDayHeatmap(trackId="t1")

        self.assertEqual(grid[0][9]["totalTimeListened"], 1000)
        self.assertEqual(grid[0][9]["plays"], 1)


class TestGetBucketedPlayTotals(ChartStatsTestCase):
    """The SQL half of the date-bucketed charts: plays pre-aggregated into
    fixed 15-minute UTC buckets, so Python only maps bucket starts to the
    user's IANA timezone instead of iterating every play row."""

    def test_aggregates_plays_within_the_same_bucket(self):
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 7, 1, 12, 1), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 7, 1, 12, 14), "timePlayed": 2000},   #< same 15-min bucket
            {"id": "t1", "playedAt": _ts(2026, 7, 1, 12, 16), "timePlayed": 4000},   #< next bucket
        ]
        db = self._makeDb({}, entries)

        rows = db.repo.getBucketedPlayTotals("testuser")

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["bucketStartTs"], _ts(2026, 7, 1, 12, 0))
        self.assertEqual(rows[0]["plays"], 2)
        self.assertEqual(rows[0]["totalTimeListened"], 3000)
        self.assertEqual(rows[1]["bucketStartTs"], _ts(2026, 7, 1, 12, 15))
        self.assertEqual(rows[1]["plays"], 1)


class TestBucketedTimezoneCorrectness(ChartStatsTestCase):
    """Buckets are aggregated in UTC by SQL, but chart labels must follow the
    app's configurable timezone - these pin the boundary cases that would
    break if the SQL buckets ever got coarser than the smallest real-world
    UTC offset granularity (15 minutes)."""

    def _patchTz(self, offsetHours, offsetMinutes=0):
        fixedTz = datetime.timezone(datetime.timedelta(hours=offsetHours, minutes=offsetMinutes))
        patcher = patch.object(utilsModule, "tz", fixedTz)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_time_series_buckets_by_local_day_across_utc_midnight(self):
        self._patchTz(2)
        #< 22:30 UTC July 1 = 00:30 local July 2
        entries = [{"id": "t1", "playedAt": _ts(2026, 7, 1, 22, 30), "timePlayed": 1000}]
        db = self._makeDb({}, entries)

        result = db.getListeningTimeSeries(groupBy="day")

        self.assertEqual([b["label"] for b in result], ["2026-07-02"])

    def test_time_series_respects_half_hour_offset_timezones(self):
        self._patchTz(5, 30)   #< e.g. Asia/Kolkata
        #< 18:45 UTC July 1 = 00:15 local July 2
        entries = [{"id": "t1", "playedAt": _ts(2026, 7, 1, 18, 45), "timePlayed": 1000}]
        db = self._makeDb({}, entries)

        result = db.getListeningTimeSeries(groupBy="day")

        self.assertEqual([b["label"] for b in result], ["2026-07-02"])

    def test_heatmap_maps_weekday_and_hour_in_local_time(self):
        self._patchTz(2)
        #< Monday 23:30 UTC = Tuesday 01:30 local
        entries = [{"id": "t1", "playedAt": _ts(2026, 7, 6, 23, 30), "timePlayed": 1000}]
        db = self._makeDb({}, entries)

        grid = db.getHourOfDayHeatmap()

        self.assertEqual(grid[1][1]["plays"], 1)
        self.assertEqual(grid[0][23]["plays"], 0)   #< NOT the UTC cell


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
        db = self._makeDb(tracks, entries)

        result = db.getArtistTrend(topN=2, groupBy="week")

        names = {series["name"] for series in result["series"]}
        self.assertEqual(names, {"Artist A", "Artist B"})  # A=3 plays, B=1, C=1 -> A and B win the tie-break by order

    def test_series_values_align_with_buckets(self):
        tracks, entries = self._sampleData()
        db = self._makeDb(tracks, entries)

        result = db.getArtistTrend(topN=1, groupBy="week")

        self.assertEqual(result["buckets"], ["2026-07-06", "2026-07-13"])
        artistASeries = next(s for s in result["series"] if s["name"] == "Artist A")
        self.assertEqual(artistASeries["data"], [2, 1])

    def test_empty_entries_returns_empty_structure(self):
        db = self._makeDb({}, [])
        result = db.getArtistTrend()
        self.assertEqual(result, {"buckets": [], "series": []})

    def test_skips_plays_whose_track_has_no_resolvable_artists(self):
        """A track with zero artists (track_artists has no rows for it) can't
        contribute to any artist's trend line - the inner JOIN naturally excludes
        it, the SQL-backed equivalent of the old 'missing metadata -> skip'."""
        tracks = {"ghost": {"id": "ghost", "name": "No Artist Song", "artists": []}}
        entries = [{"id": "ghost", "playedAt": _ts(2026, 7, 6), "timePlayed": 1000}]
        db = self._makeDb(tracks, entries)

        result = db.getArtistTrend()

        self.assertEqual(result, {"buckets": [], "series": []})

    def test_series_carries_the_artists_id_for_click_through(self):
        tracks, entries = self._sampleData()
        db = self._makeDb(tracks, entries)

        result = db.getArtistTrend(topN=2, groupBy="week")

        idsByName = {series["name"]: series["id"] for series in result["series"]}
        self.assertEqual(idsByName, {"Artist A": "a1", "Artist B": "a2"})

    def _twoIdsSharingAName(self, plays1, plays2):
        """Two distinct artist ids both named 'Shared Name', with plays1/plays2
        total plays respectively - same-named-artist-merge test fixture."""
        artist1 = {"name": "Shared Name", "id": "id1"}
        artist2 = {"name": "Shared Name", "id": "id2"}
        tracks = {
            "song1": {"id": "song1", "name": "Song 1", "artists": [artist1]},
            "song2": {"id": "song2", "name": "Song 2", "artists": [artist2]},
        }
        entries = (
            [{"id": "song1", "playedAt": _ts(2026, 7, 6, h=i), "timePlayed": 1000} for i in range(plays1)]
            + [{"id": "song2", "playedAt": _ts(2026, 7, 6, h=12 + i), "timePlayed": 1000} for i in range(plays2)]
        )
        return tracks, entries

    def test_same_name_different_ids_merge_into_one_line_with_the_plurality_id(self):
        """Two different artist ids sharing a display name still merge into
        one series (by design, see getBucketedArtistPlayCounts's docstring) -
        the id representing that line for click-through must be whichever id
        contributed more plays under that name."""
        tracks, entries = self._twoIdsSharingAName(plays1=1, plays2=3)
        db = self._makeDb(tracks, entries)

        result = db.getArtistTrend(topN=1, groupBy="week")

        self.assertEqual(len(result["series"]), 1)
        self.assertEqual(result["series"][0]["id"], "id2")
        self.assertEqual(result["series"][0]["data"], [4])   #< the merged line's totals are unaffected by id choice

    def test_same_name_different_ids_reversed_play_counts_still_pick_the_plurality_id(self):
        """Same as above with the play counts swapped, to pin that the winner
        is determined by play count and not by which id happened to be
        inserted/queried first."""
        tracks, entries = self._twoIdsSharingAName(plays1=3, plays2=1)
        db = self._makeDb(tracks, entries)

        result = db.getArtistTrend(topN=1, groupBy="week")

        self.assertEqual(result["series"][0]["id"], "id1")

    def test_same_name_different_ids_tie_breaks_deterministically_by_id(self):
        tracks, entries = self._twoIdsSharingAName(plays1=2, plays2=2)
        db = self._makeDb(tracks, entries)

        result = db.getArtistTrend(topN=1, groupBy="week")

        self.assertEqual(result["series"][0]["id"], "id1")   #< lexicographically smaller id wins ties


class TestGetListeningCalendar(ChartStatsTestCase):
    """DB->grid wiring for the dashboard streak calendar - the grid layout
    itself is covered in test_listening_calendar.py. `now` is injected for a
    deterministic 'today', mirroring getCurrentStreak."""

    _NOW = datetime.datetime(2026, 7, 23, 12, tzinfo=datetime.timezone.utc)   # Thursday

    def test_maps_plays_to_local_days_with_summed_counts(self):
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 7, 20, 9), "timePlayed": 1000},    # Monday
            {"id": "t1", "playedAt": _ts(2026, 7, 20, 20), "timePlayed": 1000},   # Monday again -> 2
            {"id": "t1", "playedAt": _ts(2026, 7, 22, 9), "timePlayed": 1000},    # Wednesday
        ]
        db = self._makeDb({}, entries)

        cal = db.getListeningCalendar(now=self._NOW, weeks=1)

        col = cal["weeks"][-1]
        self.assertEqual(col[0]["count"], 2)   # Monday
        self.assertEqual(col[2]["count"], 1)   # Wednesday
        self.assertEqual(col[1]["count"], 0)   # Tuesday
        self.assertEqual(cal["activeDays"], 2)
        self.assertEqual(cal["totalPlays"], 3)
        self.assertEqual(cal["maxCount"], 2)

    def test_empty_database_is_a_full_grid_of_zero_cells(self):
        from services.listening_calendar import CALENDAR_WEEKS
        db = self._makeDb({}, [])

        cal = db.getListeningCalendar(now=self._NOW)

        self.assertEqual(len(cal["weeks"]), CALENDAR_WEEKS)
        self.assertEqual(cal["activeDays"], 0)
        self.assertEqual(cal["totalPlays"], 0)

    def test_plays_before_the_window_are_excluded(self):
        entries = [{"id": "t1", "playedAt": _ts(2025, 1, 1, 9), "timePlayed": 1000}]
        db = self._makeDb({}, entries)

        cal = db.getListeningCalendar(now=self._NOW, weeks=4)

        self.assertEqual(cal["activeDays"], 0)
        self.assertEqual(cal["totalPlays"], 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
