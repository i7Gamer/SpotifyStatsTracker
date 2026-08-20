import json
import sys
import os
import datetime
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase


def _ts(year, month, day, hour=0):
    """Unix timestamp (seconds) for a UTC datetime."""
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc).timestamp()


def _utc(year, month=1, day=1):
    return datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc)


#< long enough to clear any configured skip threshold - a skipped play is
#  invisible to the is_skip=0 aggregates the discovery lists are built from
NON_SKIP_MS = 240000


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

    def test_a_range_of_nothing_but_skips_returns_none(self):
        """getBucketedPlayTotals stopped filtering is_skip=0 in the WHERE, so
        `if not rows` is no longer the same test as "no plays in range" - a
        skip-only range came back with an arbitrary weekday and 0 plays."""
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        entries = [
            #< 4s: under the default 5s threshold -> classified as a skip
            {"id": "t1", "playedAt": _ts(2026, 1, 7, 10), "timePlayed": 4_000},
            {"id": "t1", "playedAt": _ts(2026, 1, 8, 10), "timePlayed": 4_000},
        ]
        db = self._makeDb(tracks, entries)
        db.repo.recomputeSkipFlags()

        result = db.getPeakListeningTime(
            startDate=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 2, 1, tzinfo=datetime.timezone.utc),
        )

        self.assertIsNone(result)

    def test_a_skip_only_day_does_not_win_the_peak(self):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": []}}
        entries = [
            # Wednesday 2026-01-07: three skips and nothing else.
            {"id": "t1", "playedAt": _ts(2026, 1, 7, 10), "timePlayed": 4_000},
            {"id": "t1", "playedAt": _ts(2026, 1, 7, 11), "timePlayed": 4_000},
            {"id": "t1", "playedAt": _ts(2026, 1, 7, 12), "timePlayed": 4_000},
            # Thursday 2026-01-08: one real listen.
            {"id": "t1", "playedAt": _ts(2026, 1, 8, 10), "timePlayed": 60_000},
        ]
        db = self._makeDb(tracks, entries)
        db.repo.recomputeSkipFlags()

        day_name, play_count = db.getPeakListeningTime(
            startDate=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2026, 2, 1, tzinfo=datetime.timezone.utc),
        )

        self.assertEqual(day_name, "Thursday")
        self.assertEqual(play_count, 1)


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


class TestDiscoveredLists(DatabaseTestCase):
    """The Wrapped discovery lists' semantics, pinned INDEPENDENTLY of how they
    are computed: an entity is discovered in the year of its first-ever
    listen, appears in that year's list only, and both its RANK and its
    displayed play count are LIFETIME numbers (the stats behind the list have
    never been date-bounded - plays from later years count). These pins are
    what the SQL pushdown must preserve."""

    _DISCOVERY_TRACKS = {
        "t1": {"id": "t1", "name": "Song 1", "artists": [{"id": "a1", "name": "Artist 1"}]},
        "t2": {"id": "t2", "name": "Song 2", "artists": [{"id": "a2", "name": "Artist 2"}]},
    }

    def _discoveryEntries(self):
        return (
            #< t1: discovered Dec 2025, most of its plays land in 2026
            [{"id": "t1", "playedAt": _ts(2025, 12, 20, h), "timePlayed": NON_SKIP_MS} for h in (1, 2)]
            + [{"id": "t1", "playedAt": _ts(2026, 1, 10, h), "timePlayed": NON_SKIP_MS} for h in (1, 2, 3)]
            #< t2: discovered 2026
            + [{"id": "t2", "playedAt": _ts(2026, 2, 1, h), "timePlayed": NON_SKIP_MS} for h in (1, 2, 3, 4)]
        )

    def _calculateYear(self, db, year):
        db._calculateAndSaveWrapped(year, _utc(year), _utc(year + 1),
                                    max_played_at=_ts(year, 12, 31))
        return db.repo.getCachedWrapped("testuser", year)

    def test_a_discovery_is_credited_to_its_first_year_with_lifetime_plays(self):
        db = self._makeDb(self._DISCOVERY_TRACKS, self._discoveryEntries())

        wrapped2025 = self._calculateYear(db, 2025)
        wrapped2026 = self._calculateYear(db, 2026)

        songs2025 = json.loads(wrapped2025["discovered_songs_list"])
        self.assertEqual([s["id"] for s in songs2025], ["t1"])
        self.assertEqual(songs2025[0]["plays"], 5,
                         "the displayed count is LIFETIME plays, not the discovery year's")

        songs2026 = json.loads(wrapped2026["discovered_songs_list"])
        self.assertEqual([s["id"] for s in songs2026], ["t2"],
                         "a song discovered in 2025 must not reappear in 2026's list")

        artists2025 = json.loads(wrapped2025["discovered_artists_list"])
        artists2026 = json.loads(wrapped2026["discovered_artists_list"])
        self.assertEqual([a["id"] for a in artists2025], ["a1"])
        self.assertEqual([a["id"] for a in artists2026], ["a2"])

    def test_discovered_lists_rank_by_lifetime_plays_and_cap_at_the_limit(self):
        tracks = {
            tid: {"id": tid, "name": f"Song {tid}", "artists": []}
            for tid in ("tTop", "tMid", "tLow")
        }
        entries = (
            #< tTop: ONE in-year play, two more the year after - lifetime 3
            [{"id": "tTop", "playedAt": _ts(2026, 3, 1), "timePlayed": NON_SKIP_MS}]
            + [{"id": "tTop", "playedAt": _ts(2027, 1, 5, h), "timePlayed": NON_SKIP_MS} for h in (1, 2)]
            + [{"id": "tMid", "playedAt": _ts(2026, 4, 1, h), "timePlayed": NON_SKIP_MS} for h in (1, 2)]
            + [{"id": "tLow", "playedAt": _ts(2026, 5, 1), "timePlayed": NON_SKIP_MS}]
        )
        db = self._makeDb(tracks, entries)

        with patch("Database.workers.wrapped_worker.WRAPPED_LIST_LIMIT", 2):
            wrapped = self._calculateYear(db, 2026)

        songs = json.loads(wrapped["discovered_songs_list"])
        self.assertEqual([s["id"] for s in songs], ["tTop", "tMid"],
                         "ranked by LIFETIME plays (tTop has one in-year play "
                         "but three overall) and capped at the limit")

    def test_the_worker_pushes_the_discovery_filter_into_sql(self):
        """The wiring half: the lists must come from the filtered, capped
        query - not from hydrating every entity ever played and filtering in
        Python (3x per recalc, every 15 minutes of active listening)."""
        db = self._makeDb(self._DISCOVERY_TRACKS, self._discoveryEntries())

        with patch.object(db, "getSongsStats", wraps=db.getSongsStats) as songsSpy, \
                patch.object(db, "getArtistsStats", wraps=db.getArtistsStats) as artistsSpy, \
                patch.object(db, "getAlbumsStats", wraps=db.getAlbumsStats) as albumsSpy:
            db._calculateAndSaveWrapped(2026, _utc(2026), _utc(2027),
                                        max_played_at=_ts(2026, 12, 31))

        from Database.workers.wrapped_worker import WRAPPED_LIST_LIMIT
        for spy in (songsSpy, artistsSpy, albumsSpy):
            filteredCalls = [c for c in spy.call_args_list
                             if c.kwargs.get("firstListenedStart") == _utc(2026)
                             and c.kwargs.get("firstListenedEnd") == _utc(2027)
                             and c.kwargs.get("limit") == WRAPPED_LIST_LIMIT]
            self.assertEqual(len(filteredCalls), 1, spy.call_args_list)
            unboundedCalls = [c for c in spy.call_args_list
                              if not c.args and "startDate" not in c.kwargs
                              and "firstListenedStart" not in c.kwargs]
            self.assertEqual(unboundedCalls, [],
                             "no call may hydrate the whole lifetime library")


class TestFirstListenWindowFilter(DatabaseTestCase):
    """The stats queries' first-listen window: keeps only groups whose
    FIRST-EVER listen (MIN over every play the aggregate sees) falls in
    [start, end), while counts stay lifetime. This is the Wrapped discovery
    lists' question, answered in SQL."""

    def _db(self):
        tracks = {
            "t1": {"id": "t1", "name": "Song 1", "artists": [{"id": "a1", "name": "Artist 1"}]},
            "t2": {"id": "t2", "name": "Song 2", "artists": [{"id": "a2", "name": "Artist 2"}]},
        }
        entries = (
            [{"id": "t1", "playedAt": _ts(2025, 12, 20, h), "timePlayed": NON_SKIP_MS} for h in (1, 2)]
            + [{"id": "t1", "playedAt": _ts(2026, 1, 10, h), "timePlayed": NON_SKIP_MS} for h in (1, 2, 3)]
            + [{"id": "t2", "playedAt": _ts(2026, 2, 1), "timePlayed": NON_SKIP_MS}]
        )
        return self._makeDb(tracks, entries)

    def test_songs_are_filtered_by_first_listen_and_keep_lifetime_counts(self):
        db = self._db()

        in2026 = db.getSongsStats(sortBy="plays",
                                  firstListenedStart=_utc(2026), firstListenedEnd=_utc(2027))
        self.assertEqual([s["id"] for s in in2026], ["t2"])

        in2025 = db.getSongsStats(sortBy="plays",
                                  firstListenedStart=_utc(2025), firstListenedEnd=_utc(2026))
        self.assertEqual([s["id"] for s in in2025], ["t1"])
        self.assertEqual(in2025[0]["plays"], 5,
                         "the window selects WHICH songs, never which plays count")

    def test_artists_and_albums_share_the_same_window_semantics(self):
        db = self._db()

        artists2025 = db.getArtistsStats(firstListenedStart=_utc(2025), firstListenedEnd=_utc(2026))
        self.assertEqual([a["id"] for a in artists2025], ["a1"])
        self.assertEqual(artists2025[0]["plays"], 5)

        albums2025 = db.getAlbumsStats(sortBy="plays",
                                       firstListenedStart=_utc(2025), firstListenedEnd=_utc(2026))
        albums2026 = db.getAlbumsStats(sortBy="plays",
                                       firstListenedStart=_utc(2026), firstListenedEnd=_utc(2027))
        #< every fixture track fabricates its own album, so the split mirrors
        #  the songs': t1's album discovered 2025, t2's 2026
        self.assertEqual(len(albums2025), 1)
        self.assertEqual(albums2025[0]["plays"], 5)
        self.assertEqual(len(albums2026), 1)

    def test_the_window_respects_the_limit_and_order(self):
        db = self._db()

        capped = db.getSongsStats(sortBy="plays", limit=1,
                                  firstListenedStart=_utc(2025), firstListenedEnd=_utc(2027))
        self.assertEqual([s["id"] for s in capped], ["t1"])   #< 5 lifetime plays beats 1


if __name__ == "__main__":
    unittest.main()
