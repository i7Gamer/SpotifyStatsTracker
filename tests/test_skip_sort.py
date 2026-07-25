"""Sorting the Top Songs/Albums/Artists pages by skips.

Two things matter here beyond "it sorts":

1. The skip-ordered path must not touch getSongsPage / getArtistAggregates /
   getAlbumsPage. Their WHERE is_skip=0 is load-bearing for their query plans
   (an is_skip partial index was measured to regress top-songs 2x), which is
   why this is a separate query rather than a skips column on those.
2. Ordering is the same shrunk skip RATE the Charts top-N uses, so "most
   skipped" means one thing across the app. Nothing is filtered out: a paged
   list must not silently omit rows the pagination already counted, which is
   what shrinkage buys over the minimum-encounters floor it replaced.
"""
import datetime
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest
import Database.utils as utilsModule


def _ts(hour=0):
    return int(datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc).timestamp()) + hour * 3600


class SkipSortTestCase(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = self._makeDb({}, [])
        self._hour = 0

    def _seed(self, trackId, plays, skips, artistId="art1", artistName="Artist One", albumId="alb1"):
        self.db.repo.upsertTrack(normalizeTrackForTest({
            "id": trackId, "name": f"Song {trackId}", "imageId": albumId,
            "artists": [{"id": artistId, "name": artistName}],
        }))
        for _ in range(plays):
            self.db.repo.insertPlay(self.db.user, trackId, _ts(self._hour), 200000, is_skip=0)
            self._hour += 1
        for _ in range(skips):
            self.db.repo.insertPlay(self.db.user, trackId, _ts(self._hour), 2000, is_skip=1)
            self._hour += 1
        self.db.repo.commit()


class TestSongsSortedBySkips(SkipSortTestCase):
    def test_orders_by_rate_not_by_raw_count(self):
        """`many` is skipped more times in absolute terms, but `always` is
        skipped a far higher share of the times it comes up - and a share is
        what "most skipped" should mean."""
        self._seed("bulk", plays=200, skips=0)   #< the rest of the library, so the average is normal
        self._seed("many", plays=50, skips=20)   #< 20 skips, 29%
        self._seed("always", plays=2, skips=18)  #< 18 skips, 90%

        ranked = self.db.getTopSongs(by="skips", limit=10)

        self.assertEqual([s["id"] for s in ranked], ["always", "many"])

    def test_nothing_is_filtered_out_by_low_volume(self):
        """A single skip is enough to appear. Shrinkage tempers where such a row
        RANKS; it never hides one, because the page count includes it."""
        self._seed("barely", plays=0, skips=1)

        self.assertEqual([s["id"] for s in self.db.getTopSongs(by="skips", limit=10)], ["barely"])

    def test_never_skipped_songs_are_excluded(self):
        self._seed("loved", plays=30, skips=0)

        self.assertEqual(self.db.getTopSongs(by="skips", limit=10), [])

    def test_cards_get_the_fields_the_template_renders(self):
        self._seed("t1", plays=4, skips=6)

        entry = self.db.getTopSongs(by="skips", limit=10)[0]

        self.assertEqual(entry["skips"], 6)
        self.assertEqual(entry["plays"], 4)
        self.assertEqual(entry["skipPercent"], 60.0)
        self.assertEqual(entry["name"], "Song t1")
        self.assertEqual(entry["totalTimeListened"], 4 * 200000)
        self.assertIsNotNone(entry["firstListenedAt"])

    def test_a_skip_only_song_reports_its_first_encounter(self):
        """It has no real listen to date, so the first skip stands in rather
        than leaving the card's date blank or at the epoch."""
        self._seed("skipped", plays=0, skips=3)

        self.assertGreater(self.db.getTopSongs(by="skips", limit=10)[0]["firstListenedAt"], 0)

    def test_paging_covers_every_row_exactly_once(self):
        for index in range(7):
            self._seed(f"t{index}", plays=1, skips=index + 1)

        firstPage = self.db.getTopSongs(by="skips", limit=3, offset=0)
        secondPage = self.db.getTopSongs(by="skips", limit=3, offset=3)
        thirdPage = self.db.getTopSongs(by="skips", limit=3, offset=6)

        ids = [s["id"] for s in firstPage + secondPage + thirdPage]
        self.assertEqual(len(ids), 7)
        self.assertEqual(len(set(ids)), 7)

    def test_the_count_matches_what_the_pages_contain(self):
        for index in range(4):
            self._seed(f"t{index}", plays=1, skips=1)
        self._seed("clean", plays=5, skips=0)

        self.assertEqual(self.db.getSongsCount(sortBy="skips"), 4)


class TestTheHotAggregateIsNotUsed(SkipSortTestCase):
    """The reason this feature has its own query path at all."""

    def test_skip_sorting_never_calls_getSongsPage(self):
        self._seed("t1", plays=1, skips=1)

        with patch.object(self.db.repo, "getSongsPage", wraps=self.db.repo.getSongsPage) as hot:
            self.db.getTopSongs(by="skips", limit=10)

        hot.assert_not_called()

    def test_skip_sorting_never_calls_getArtistAggregates(self):
        self._seed("t1", plays=1, skips=1)

        with patch.object(self.db.repo, "getArtistAggregates", wraps=self.db.repo.getArtistAggregates) as hot:
            self.db.getTopArtists(by="skips", limit=10)

        hot.assert_not_called()

    def test_skip_sorting_never_calls_getAlbumsPage(self):
        self._seed("t1", plays=1, skips=1)

        with patch.object(self.db.repo, "getAlbumsPage", wraps=self.db.repo.getAlbumsPage) as hot:
            self.db.getTopAlbums(by="skips", limit=10)

        hot.assert_not_called()

    def test_every_other_sort_still_uses_the_aggregate(self):
        self._seed("t1", plays=3, skips=1)

        with patch.object(self.db.repo, "getSongsPage", wraps=self.db.repo.getSongsPage) as hot:
            self.db.getTopSongs(by="plays", limit=10)

        hot.assert_called_once()

    def test_normal_sorts_are_unaffected_by_skip_rows(self):
        """Regression guard on the untouched path: skips must stay invisible to
        the play-ranked list."""
        self._seed("t1", plays=3, skips=99)
        self._seed("t2", plays=5, skips=0)

        ranked = self.db.getTopSongs(by="plays", limit=10)

        self.assertEqual([s["id"] for s in ranked], ["t2", "t1"])
        self.assertEqual(ranked[0]["plays"], 5)


class TestArtistsAndAlbumsSortedBySkips(SkipSortTestCase):
    def test_artists_order_by_skip_rate(self):
        self._seed("t1", plays=1, skips=5, artistId="skipped", artistName="Skipped")
        self._seed("t2", plays=20, skips=1, artistId="loved", artistName="Loved")

        ranked = self.db.getTopArtists(by="skips", limit=10)

        self.assertEqual([a["name"] for a in ranked], ["Skipped", "Loved"])
        self.assertEqual(ranked[0]["skips"], 5)

    def test_albums_order_by_skip_rate_so_length_does_not_decide(self):
        """The reason albums rank by rate: a long record collects more skips
        just by having more tracks. `long` has 20 skips to `short`'s 8, but it
        is skipped a quarter of the time against `short`'s four fifths."""
        self._seed("t1", plays=30, skips=10, albumId="long")
        self._seed("t2", plays=30, skips=10, albumId="long")
        self._seed("t3", plays=2, skips=8, albumId="short")

        ranked = self.db.getTopAlbums(by="skips", limit=10)

        self.assertEqual([a["id"] for a in ranked], ["short", "long"])
        self.assertEqual(ranked[1]["skips"], 20)   #< the loser still has the bigger raw count

    def test_album_skips_aggregate_across_its_tracks(self):
        self._seed("t1", plays=1, skips=2, albumId="alb1")
        self._seed("t2", plays=1, skips=3, albumId="alb1")

        entry = self.db.getTopAlbums(by="skips", limit=10)[0]

        self.assertEqual(entry["skips"], 5)
        self.assertEqual(entry["uniqueSongCount"], 2)

    def test_counts_match_for_artists_and_albums(self):
        self._seed("t1", plays=1, skips=1, artistId="a1", albumId="alb1")
        self._seed("t2", plays=1, skips=1, artistId="a2", albumId="alb2")

        self.assertEqual(self.db.getArtistsCount(sortBy="skips"), 2)
        self.assertEqual(self.db.getAlbumsCount(sortBy="skips"), 2)


class TestSortParamValidation(unittest.TestCase):
    def test_skips_is_an_accepted_sort_value(self):
        from config import VALID_SORT_BY

        self.assertIn("skips", VALID_SORT_BY)

    def test_the_ranking_weight_reaches_sql_as_a_bound_param(self):
        """The predecessor took an `orderBy` string that was interpolated into
        the ORDER BY and so had to be whitelisted. The ranking is now one fixed
        expression and the only caller-supplied value is bound, so a hostile
        priorWeight is data, never syntax."""
        from Database.repository import Repository
        from pathlib import Path

        repo = Repository(Path(":memory:"))
        self.addCleanup(repo.connectionManager.close)

        self.assertEqual(repo._shrunkSkipRateSql().count("?"), 2)
        self.assertEqual(repo.getMostSkippedTracks("alice", priorWeight="1); DROP TABLE plays --"), [])


if __name__ == "__main__":
    unittest.main()
