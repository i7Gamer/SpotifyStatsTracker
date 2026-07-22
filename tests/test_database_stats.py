import datetime
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
import Database.utils as utilsModule


class TestGetArtistsStatsDoesNotMutateCache(DatabaseTestCase):
    def _sampleData(self):
        artist = {"name": "Artist A", "id": "a1"}
        track = {"id": "track1", "artists": [artist], "name": "Song One"}
        tracks = {"track1": track}
        entries = [
            {"id": "track1", "playedAt": 1000, "timePlayed": 5000},
            {"id": "track1", "playedAt": 2000, "timePlayed": 5000},
        ]
        return tracks, entries, artist

    def test_artist_dict_in_tracks_cache_is_unmodified_after_stats_call(self):
        tracks, entries, artist = self._sampleData()
        db = self._makeDb(tracks, entries)

        db.getArtistsStats()

        # The artist dict the caller originally passed in must not have picked up
        # derived, per-request-only fields - those belong only in the returned
        # stats list. Every Database read reconstructs fresh dicts from the DB, so
        # there's no shared cache left to leak into.
        self.assertNotIn("plays", artist)
        self.assertNotIn("totalTimeListened", artist)
        self.assertNotIn("uniqueSongs", artist)
        self.assertNotIn("uniqueSongCount", artist)
        self.assertNotIn("firstListenedAt", artist)
        self.assertEqual(artist, {"name": "Artist A", "id": "a1"})

    def test_returned_stats_are_still_correct(self):
        tracks, entries, artist = self._sampleData()
        db = self._makeDb(tracks, entries)

        stats = db.getArtistsStats()

        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["name"], "Artist A")
        self.assertEqual(stats[0]["plays"], 2)
        self.assertEqual(stats[0]["totalTimeListened"], 10000)
        self.assertEqual(stats[0]["uniqueSongCount"], 1)

    def test_repeated_calls_do_not_accumulate_stale_state(self):
        """A second call must not be polluted by fields left over from the first."""
        tracks, entries, artist = self._sampleData()
        db = self._makeDb(tracks, entries)

        firstStats = db.getArtistsStats()
        secondStats = db.getArtistsStats()

        self.assertEqual(firstStats[0]["plays"], secondStats[0]["plays"])
        self.assertEqual(firstStats[0]["totalTimeListened"], secondStats[0]["totalTimeListened"])


class TestGetEntriesFromNew(DatabaseTestCase):
    """Slicing edge cases: a startIndex at/past the end must yield an empty page,
    not wrap around to a negative index and return the whole history."""

    def _makeDbWithEntries(self, entryCount):
        entries = [
            {"id": f"t{i}", "playedAt": i, "timePlayed": 1000}
            for i in range(entryCount)
        ]
        return self._makeDb({}, entries), entries

    def test_returns_all_entries_newest_first(self):
        db, entries = self._makeDbWithEntries(3)
        result = db.getEntriesFromNew(fullPagination=False)
        self.assertEqual([e["id"] for e in result], [e["id"] for e in reversed(entries)])

    def test_returns_requested_page(self):
        db, entries = self._makeDbWithEntries(5)
        result = db.getEntriesFromNew(count=2, startIndex=2, fullPagination=False)
        self.assertEqual([e["id"] for e in result], ["t2", "t1"])

    def test_start_index_at_end_returns_empty(self):
        db, _ = self._makeDbWithEntries(3)
        result = db.getEntriesFromNew(startIndex=3, fullPagination=False)
        self.assertEqual(result, [])

    def test_start_index_past_end_returns_empty(self):
        db, _ = self._makeDbWithEntries(3)
        result = db.getEntriesFromNew(count=2, startIndex=10, fullPagination=False)
        self.assertEqual(result, [])

    def test_empty_database_returns_empty(self):
        db, _ = self._makeDbWithEntries(0)
        self.assertEqual(db.getEntriesFromNew(fullPagination=False), [])
        self.assertEqual(db.getEntriesFromNew(count=5, fullPagination=False), [])

    def test_count_larger_than_remaining_returns_rest(self):
        db, _ = self._makeDbWithEntries(3)
        result = db.getEntriesFromNew(count=10, startIndex=1, fullPagination=False)
        self.assertEqual([e["id"] for e in result], ["t1", "t0"])


class TestGetTopSongsMultiArtist(DatabaseTestCase):
    def test_song_with_multiple_artists_round_trips(self):
        track = {"id": "track1", "artists": [{"id": "a1", "name": "Artist A"}, {"id": "a2", "name": "Artist B"}],
                 "name": "Song One"}
        entries = [{"id": "track1", "playedAt": 1000, "timePlayed": 5000}]
        db = self._makeDb({"track1": track}, entries)

        songs = db.getTopSongs()

        self.assertEqual(len(songs), 1)
        self.assertEqual([a["id"] for a in songs[0]["artists"]], ["a1", "a2"])
        self.assertEqual(songs[0]["plays"], 1)
        self.assertEqual(songs[0]["totalTimeListened"], 5000)

    def test_get_track_is_not_called_per_row_anymore(self):
        """Regression guard against the old N+1 pattern (getSongsStats calling
        repo.getTrack() once per unique song) silently coming back."""
        tracks = {f"t{i}": {"id": f"t{i}", "name": f"Song {i}", "artists": []} for i in range(5)}
        entries = [{"id": f"t{i}", "playedAt": i, "timePlayed": 1000} for i in range(5)]
        db = self._makeDb(tracks, entries)

        with patch.object(db.repo, "getTrack", wraps=db.repo.getTrack) as mockGetTrack:
            songs = db.getTopSongs()

        self.assertEqual(len(songs), 5)
        mockGetTrack.assert_not_called()


class TestGetOverallStats(DatabaseTestCase):
    def _sampleData(self):
        tracks = {
            "t1": {"id": "t1", "name": "Song One", "artists": [{"id": "a1", "name": "Artist A"}]},
            "t2": {"id": "t2", "name": "Song Two", "artists": [{"id": "a1", "name": "Artist A"}]},
        }
        entries = [
            {"id": "t1", "playedAt": 100, "timePlayed": 3000},
            {"id": "t1", "playedAt": 200, "timePlayed": 3000},
            {"id": "t2", "playedAt": 300, "timePlayed": 1000},
        ]
        return tracks, entries

    def test_totals_match_hand_computed_values(self):
        tracks, entries = self._sampleData()
        db = self._makeDb(tracks, entries)

        stats = db.getOverallStats()

        self.assertEqual(stats["totalSongsPlayed"], 3)
        self.assertEqual(stats["totalDurationMs"], 7000)
        self.assertEqual(len(stats["currentTopSongs"]), 1)
        self.assertEqual(stats["currentTopSongs"][0]["id"], "t1")   #< 2 plays beats t2's 1
        self.assertEqual(len(stats["currentTopArtists"]), 1)
        self.assertEqual(stats["currentTopArtists"][0]["id"], "a1")
        self.assertEqual(stats["previousSongsPlayed"], 0)
        self.assertEqual(stats["previousDurationMs"], 0)

    def test_empty_database_returns_zeroed_stats_without_crashing(self):
        db = self._makeDb({}, [])

        stats = db.getOverallStats()

        self.assertEqual(stats["currentTopSongs"], [])
        self.assertEqual(stats["currentTopArtists"], [])
        self.assertEqual(stats["totalSongsPlayed"], 0)
        self.assertEqual(stats["totalDurationMs"], 0)

    def test_play_at_period_boundary_is_not_double_counted(self):
        """_getDateRange documents its result as the half-open interval
        [startDate, endDate) - the previous period is computed as
        [startDate - duration, startDate), immediately adjacent to the
        current [startDate, endDate). A play landing exactly on that shared
        boundary must count in exactly one of the two periods, not both."""
        import datetime
        boundary = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
        tracks = {"t1": {"id": "t1", "name": "Song One", "artists": []}}
        entries = [{"id": "t1", "playedAt": boundary.timestamp(), "timePlayed": 1000}]
        db = self._makeDb(tracks, entries)

        startDate = boundary
        endDate = boundary + datetime.timedelta(days=1)

        stats = db.getOverallStats(startDate, endDate)

        self.assertEqual(stats["totalSongsPlayed"], 1)
        self.assertEqual(stats["previousSongsPlayed"], 0)


class TestGetAlbumsStats(DatabaseTestCase):
    def _sampleData(self):
        tracks = {
            "t1": {"id": "t1", "name": "Song One", "artists": [{"id": "a1", "name": "Artist A"}],
                   "imageId": "alb1", "album": {"id": "alb1", "name": "Album One", "url": "u",
                                                 "imageId": "alb1", "imageUrl": "", "totalTracks": 2,
                                                 "releaseDate": 0}},
            "t2": {"id": "t2", "name": "Song Two", "artists": [{"id": "a1", "name": "Artist A"}],
                   "imageId": "alb2", "album": {"id": "alb2", "name": "Album Two", "url": "u",
                                                 "imageId": "alb2", "imageUrl": "", "totalTracks": 1,
                                                 "releaseDate": 0}},
        }
        entries = [
            {"id": "t1", "playedAt": 100, "timePlayed": 3000},
            {"id": "t1", "playedAt": 200, "timePlayed": 3000},
            {"id": "t2", "playedAt": 300, "timePlayed": 1000},
        ]
        return tracks, entries

    def test_get_albums_stats_returns_one_row_per_album(self):
        tracks, entries = self._sampleData()
        db = self._makeDb(tracks, entries)

        albums = db.getAlbumsStats()

        self.assertEqual({a["id"] for a in albums}, {"alb1", "alb2"})
        alb1 = next(a for a in albums if a["id"] == "alb1")
        self.assertEqual(alb1["plays"], 2)
        self.assertEqual(alb1["totalTimeListened"], 6000)

    def test_get_albums_count(self):
        tracks, entries = self._sampleData()
        db = self._makeDb(tracks, entries)

        self.assertEqual(db.getAlbumsCount(), 2)

    def test_get_top_albums_sorted_by_plays(self):
        tracks, entries = self._sampleData()
        db = self._makeDb(tracks, entries)

        topAlbums = db.getTopAlbums(by="plays")

        self.assertEqual([a["id"] for a in topAlbums], ["alb1", "alb2"])

    def test_empty_database_returns_empty(self):
        db = self._makeDb({}, [])

        self.assertEqual(db.getAlbumsStats(), [])
        self.assertEqual(db.getAlbumsCount(), 0)
        self.assertEqual(db.getTopAlbums(), [])


class TestDetailLookups(DatabaseTestCase):
    """getSong/getArtist/getAlbum are the thin single-item lookups the
    song/artist/album detail pages use - reusing the same paged/aggregate
    queries the listing pages already rely on, just narrowed to one id."""

    def _sampleData(self):
        tracks = {
            "t1": {"id": "t1", "name": "Song One", "artists": [{"id": "a1", "name": "Artist A"}],
                   "imageId": "alb1", "album": {"id": "alb1", "name": "Album One", "url": "u",
                                                 "imageId": "alb1", "imageUrl": "", "totalTracks": 1,
                                                 "releaseDate": 0}},
        }
        entries = [
            {"id": "t1", "playedAt": 100, "timePlayed": 3000},
            {"id": "t1", "playedAt": 200, "timePlayed": 3000},
        ]
        return tracks, entries

    def test_get_song_returns_the_matching_track(self):
        tracks, entries = self._sampleData()
        db = self._makeDb(tracks, entries)

        song = db.getSong("t1")

        self.assertEqual(song["id"], "t1")
        self.assertEqual(song["plays"], 2)

    def test_get_song_unknown_returns_none(self):
        db = self._makeDb({}, [])
        self.assertIsNone(db.getSong("missing"))

    def test_get_artist_returns_the_matching_artist(self):
        tracks, entries = self._sampleData()
        db = self._makeDb(tracks, entries)

        artist = db.getArtist("a1")

        self.assertEqual(artist["id"], "a1")
        self.assertEqual(artist["plays"], 2)

    def test_get_artist_unknown_returns_none(self):
        db = self._makeDb({}, [])
        self.assertIsNone(db.getArtist("missing"))

    def test_get_album_returns_the_matching_album(self):
        tracks, entries = self._sampleData()
        db = self._makeDb(tracks, entries)

        album = db.getAlbum("alb1")

        self.assertEqual(album["id"], "alb1")
        self.assertEqual(album["plays"], 2)

    def test_get_album_unknown_returns_none(self):
        db = self._makeDb({}, [])
        self.assertIsNone(db.getAlbum("missing"))


class TestGetPlayTotals(DatabaseTestCase):
    def test_returns_count_and_sum(self):
        tracks = {"t1": {"id": "t1", "name": "Song One", "artists": []}}
        entries = [
            {"id": "t1", "playedAt": 100, "timePlayed": 1000},
            {"id": "t1", "playedAt": 200, "timePlayed": 2000},
        ]
        db = self._makeDb(tracks, entries)

        count, total = db.getPlayTotals()

        self.assertEqual(count, 2)
        self.assertEqual(total, 3000)

    def test_empty_database_returns_zero(self):
        db = self._makeDb({}, [])
        self.assertEqual(db.getPlayTotals(), (0, 0))


class TestNewChartsStats(DatabaseTestCase):
    def test_get_explicit_ratio(self):
        tracks = {
            "t1": {"id": "t1", "name": "Explicit Song", "artists": [], "explicit": 1},
            "t2": {"id": "t2", "name": "Clean Song", "artists": [], "explicit": 0}
        }
        entries = [
            {"id": "t1", "playedAt": 100, "timePlayed": 1000},
            {"id": "t2", "playedAt": 200, "timePlayed": 2000},
            {"id": "t2", "playedAt": 300, "timePlayed": 2000},
        ]
        db = self._makeDb(tracks, entries)
        ratio = db.getExplicitRatio()
        self.assertEqual(ratio["explicit"], 1)
        self.assertEqual(ratio["clean"], 2)

    def test_get_release_decade_distribution(self):
        tracks = {
            "t1": {"id": "t1", "name": "Song One", "artists": [], "releaseDate": 1609459200},  # 2021 -> 2020s
            "t2": {"id": "t2", "name": "Song Two", "artists": [], "releaseDate": 946684800},   # 2000 -> 2000s
            "t3": {"id": "t3", "name": "Song Three", "artists": [], "releaseDate": -315619200}  # 1960 -> 1960s
        }
        entries = [
            {"id": "t1", "playedAt": 100, "timePlayed": 1000},
            {"id": "t2", "playedAt": 200, "timePlayed": 2000},
            {"id": "t3", "playedAt": 300, "timePlayed": 2000},
        ]
        db = self._makeDb(tracks, entries)
        decades = db.getReleaseDecadeDistribution()
        self.assertEqual(decades, {"1960s": 1, "2000s": 1, "2020s": 1})

    def test_release_decade_uses_the_calendar_date_not_the_app_timezone(self):
        """Release dates are stored as midnight-UTC timestamps of a calendar
        date, so the decade must come from the UTC year. The old Python
        conversion applied the app timezone, which shifted every Jan 1
        release into the previous year - and previous DECADE for years
        ending in 0 - whenever the offset was negative."""
        tracks = {
            #< 2020-01-01T00:00Z: the exact boundary case
            "t1": {"id": "t1", "name": "Song", "artists": [], "releaseDate": 1577836800},
        }
        entries = [{"id": "t1", "playedAt": 100, "timePlayed": 1000}]
        with patch.object(utilsModule, "tz", datetime.timezone(datetime.timedelta(hours=-5))):
            db = self._makeDb(tracks, entries)

            decades = db.getReleaseDecadeDistribution()

        self.assertEqual(decades, {"2020s": 1})

    def test_get_completion_stats(self):
        tracks = {
            "t1": {"id": "t1", "name": "Song One", "artists": [], "duration": 100000},
            "t2": {"id": "t2", "name": "Song Two", "artists": [], "duration": 100000}
        }
        entries = [
            {"id": "t1", "playedAt": 200, "timePlayed": 85000},   #< complete (>=80%)
            {"id": "t2", "playedAt": 300, "timePlayed": 50000},   #< partial (<80%)
        ]
        db = self._makeDb(tracks, entries)
        db.repo.insertPlay("testuser", "t1", 100, 2000, is_skip=1)   #< a skip (is_skip=1 row)
        db.repo.commit()
        stats = db.getCompletionStats()
        self.assertEqual(stats["skips"], 1)
        self.assertEqual(stats["completes"], 1)
        self.assertEqual(stats["partials"], 1)

    def test_completion_stats_boundaries(self):
        """Among real plays (is_skip=0): exactly 80% of the duration IS a
        complete, and unknown (<=0) durations always count as complete. The
        skips bucket is purely the is_skip=1 rows (no separate 30s line)."""
        tracks = {
            "zero": {"id": "zero", "name": "No Duration", "artists": [], "duration": 0},
            "t1": {"id": "t1", "name": "Song", "artists": [], "duration": 100000},
        }
        entries = [
            {"id": "zero", "playedAt": 100, "timePlayed": 30000},   #< unknown duration -> complete
            {"id": "t1", "playedAt": 300, "timePlayed": 80000},     #< exactly 80% -> complete
            {"id": "t1", "playedAt": 400, "timePlayed": 79999},     #< just under 80% -> partial
        ]
        db = self._makeDb(tracks, entries)
        db.repo.insertPlay("testuser", "t1", 200, 2000, is_skip=1)   #< the only skip
        db.repo.commit()

        stats = db.getCompletionStats()

        self.assertEqual(stats, {"skips": 1, "completes": 2, "partials": 1})

    def test_completion_stats_empty_database_returns_zeros(self):
        db = self._makeDb({}, [])
        self.assertEqual(db.getCompletionStats(), {"skips": 0, "completes": 0, "partials": 0})

    def test_completion_stats_counts_is_skip_rows(self):
        """The skips bucket the Charts/Compare Skip Rate is built from is just
        the is_skip=1 rows in plays."""
        tracks = {"t1": {"id": "t1", "name": "Song", "artists": [], "duration": 100000}}
        entries = [{"id": "t1", "playedAt": 100, "timePlayed": 85000}]  #< 1 complete
        db = self._makeDb(tracks, entries)
        db.repo.insertPlay("testuser", "t1", 200, 400, is_skip=1)
        db.repo.insertPlay("testuser", "t1", 300, 400, is_skip=1)
        db.repo.commit()

        stats = db.getCompletionStats()

        self.assertEqual(stats, {"skips": 2, "completes": 1, "partials": 0})

    def test_completion_stats_skips_respect_date_range_and_user(self):
        tracks = {"t1": {"id": "t1", "name": "Song", "artists": [], "duration": 100000}}
        entries = [{"id": "t1", "playedAt": 1000, "timePlayed": 85000}]
        db = self._makeDb(tracks, entries)
        db.repo.upsertUser("otheruser", "other@example.com")
        db.repo.insertPlay("testuser", "t1", 500, 400, is_skip=1)     #< before the range below
        db.repo.insertPlay("otheruser", "t1", 1500, 400, is_skip=1)   #< in range, wrong user
        db.repo.commit()

        startDate = datetime.datetime.fromtimestamp(900, tz=datetime.timezone.utc)
        stats = db.getCompletionStats(startDate=startDate)

        self.assertEqual(stats, {"skips": 0, "completes": 1, "partials": 0})

    def test_early_abandon_is_partial_not_skip(self):
        """A real play abandoned early (above the default 5s skip threshold) is
        a partial, not a skip - completion stats no longer uses a separate 30s
        line, only is_skip plus the 80% complete ratio."""
        tracks = {"t1": {"id": "t1", "name": "Song", "artists": [], "duration": 100000}}
        entries = [
            {"id": "t1", "playedAt": 100, "timePlayed": 15000},   #< 15s > 5s -> real play, <80% -> partial
            {"id": "t1", "playedAt": 200, "timePlayed": 85000},   #< complete
        ]
        db = self._makeDb(tracks, entries)

        stats = db.getCompletionStats()

        self.assertEqual(stats, {"skips": 0, "completes": 1, "partials": 1})

    def test_explicit_ratio_empty_database_returns_zeros(self):
        db = self._makeDb({}, [])
        self.assertEqual(db.getExplicitRatio(), {"explicit": 0, "clean": 0})


if __name__ == "__main__":
    import unittest
    unittest.main()
