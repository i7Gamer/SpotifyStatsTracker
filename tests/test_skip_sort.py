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

    def _seed(self, trackId, plays, skips, artistId="art1", artistName="Artist One", albumId="alb1",
               name=None, albumName=None, duration=0, partials=0):
        """`plays` full plays, `partials` half-length plays (neither a skip nor
        a full listen - only visible with a real `duration`), `skips` skips."""
        track = {
            "id": trackId, "name": name or f"Song {trackId}", "imageId": albumId, "duration": duration,
            "artists": [{"id": artistId, "name": artistName}],
        }
        if albumName is not None:
            track["album"] = {"id": albumId, "name": albumName, "url": "http://example.com/album",
                              "imageId": albumId, "imageUrl": "", "totalTracks": 1, "releaseDate": 0}
        self.db.repo.upsertTrack(normalizeTrackForTest(track))
        for timePlayed, count, isSkip in ((duration or 200000, plays, 0),
                                          ((duration or 200000) // 2, partials, 0),
                                          (2000, skips, 1)):
            for _ in range(count):
                self.db.repo.insertPlay(self.db.user, trackId, _ts(self._hour), timePlayed, is_skip=isSkip)
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


class TestTheSkipPageLooksLikeEveryOtherPage(SkipSortTestCase):
    """A card doesn't know which sort produced it, so a skip-ranked row has to
    carry the same keys a plays-ranked one does. Changing the sort should
    reorder the page, never strip fields off it."""

    def test_album_cards_keep_their_artists(self):
        """_track_card.html renders artistLinks(album.artists) whatever the
        sort is - an album that comes back artist-less renders a blank line."""
        self._seed("t1", plays=1, skips=1, artistId="a1", artistName="Real Artist", albumId="alb1")

        entry = self.db.getTopAlbums(by="skips", limit=10)[0]

        self.assertEqual([a["name"] for a in entry["artists"]], ["Real Artist"])

    def test_albums_carry_every_key_the_plays_sort_does(self):
        self._seed("t1", plays=1, skips=1, albumId="alb1")

        skipSorted = self.db.getTopAlbums(by="skips", limit=10)[0]
        playsSorted = self.db.getTopAlbums(by="plays", limit=10)[0]

        self.assertEqual(set(playsSorted) - set(skipSorted), set())

    def test_artists_carry_every_key_the_plays_sort_does(self):
        self._seed("t1", plays=1, skips=1, artistId="a1")

        skipSorted = self.db.getTopArtists(by="skips", limit=10)[0]
        playsSorted = self.db.getTopArtists(by="plays", limit=10)[0]

        self.assertEqual(set(playsSorted) - set(skipSorted), set())

    def test_songs_carry_every_key_the_plays_sort_does(self):
        self._seed("t1", plays=1, skips=1)

        skipSorted = self.db.getTopSongs(by="skips", limit=10)[0]
        playsSorted = self.db.getTopSongs(by="plays", limit=10)[0]

        self.assertEqual(set(playsSorted) - set(skipSorted), set())


class TestTheSkipPageRespectsTheFilters(SkipSortTestCase):
    """The sort control sits next to the search box, the tag dropdown and the
    full-plays checkbox, and changing one is not supposed to disarm the others.
    Every filter the plays-ranked page honours has to reach this query too -
    including into the count, or the pager sizes itself for a different list
    than the one on screen."""

    def test_search_narrows_the_song_list(self):
        self._seed("t1", plays=1, skips=1, name="Alpha")
        self._seed("t2", plays=1, skips=1, name="Beta")

        ranked = self.db.getTopSongs(by="skips", limit=10, searchQuery="Alpha")

        self.assertEqual([s["name"] for s in ranked], ["Alpha"])

    def test_search_matches_the_artist_name_too(self):
        """Same fields getSongsPage's search matches - name, album, artist."""
        self._seed("t1", plays=1, skips=1, artistId="a1", artistName="Portishead")
        self._seed("t2", plays=1, skips=1, artistId="a2", artistName="Someone Else")

        ranked = self.db.getTopSongs(by="skips", limit=10, searchQuery="portis")

        self.assertEqual([s["id"] for s in ranked], ["t1"])

    def test_the_song_count_matches_the_filtered_list(self):
        self._seed("t1", plays=1, skips=1, name="Alpha")
        self._seed("t2", plays=1, skips=1, name="Beta")

        self.assertEqual(self.db.getSongsCount(sortBy="skips", searchQuery="Alpha"), 1)

    def test_the_tag_filter_narrows_the_song_list(self):
        """`trackIds` is how a tag filter reaches the query."""
        self._seed("t1", plays=1, skips=1)
        self._seed("t2", plays=1, skips=1)

        ranked = self.db.getTopSongs(by="skips", limit=10, trackIds=["t1"])

        self.assertEqual([s["id"] for s in ranked], ["t1"])
        self.assertEqual(self.db.getSongsCount(sortBy="skips", trackIds=["t1"]), 1)

    def test_an_empty_tag_filter_matches_nothing(self):
        """Same contract as everywhere else here: [] is an explicit empty set,
        not "no filter" - a tag nothing is tagged with must show nothing."""
        self._seed("t1", plays=1, skips=1)

        self.assertEqual(self.db.getTopSongs(by="skips", limit=10, trackIds=[]), [])
        self.assertEqual(self.db.getSongsCount(sortBy="skips", trackIds=[]), 0)

    def test_entity_scoping_narrows_the_song_list(self):
        """No route asks for this yet (both detail sub-lists pin sortBy=plays),
        but the param exists on this method and silently returning the whole
        library for it is a trap for the next caller."""
        self._seed("t1", plays=1, skips=1, artistId="a1", albumId="alb1")
        self._seed("t2", plays=1, skips=1, artistId="a2", albumId="alb2")
        scoped = lambda **kwargs: [s["id"] for s in self.db.getSongsStats(sortBy="skips", **kwargs)]

        self.assertEqual(scoped(artistId="a1"), ["t1"])
        self.assertEqual(scoped(albumId="alb1"), ["t1"])
        self.assertEqual(scoped(trackId="t2"), ["t2"])

    def test_search_and_tag_narrow_the_album_list(self):
        self._seed("t1", plays=1, skips=1, albumId="alb1", albumName="Kid A")
        self._seed("t2", plays=1, skips=1, albumId="alb2", albumName="Amnesiac")

        self.assertEqual([a["id"] for a in self.db.getTopAlbums(by="skips", searchQuery="kid")], ["alb1"])
        self.assertEqual(self.db.getAlbumsCount(sortBy="skips", searchQuery="kid"), 1)
        self.assertEqual([a["id"] for a in self.db.getTopAlbums(by="skips", albumIds=["alb2"])], ["alb2"])
        self.assertEqual(self.db.getAlbumsCount(sortBy="skips", albumIds=["alb2"]), 1)

    def test_search_and_tag_narrow_the_artist_list(self):
        self._seed("t1", plays=1, skips=1, artistId="a1", artistName="Burial")
        self._seed("t2", plays=1, skips=1, artistId="a2", artistName="Boards of Canada")

        self.assertEqual([a["id"] for a in self.db.getTopArtists(by="skips", searchQuery="buri")], ["a1"])
        self.assertEqual(self.db.getArtistsCount(sortBy="skips", searchQuery="buri"), 1)
        self.assertEqual([a["id"] for a in self.db.getTopArtists(by="skips", artistIds=["a2"])], ["a2"])
        self.assertEqual(self.db.getArtistsCount(sortBy="skips", artistIds=["a2"]), 1)

    def test_full_plays_only_drops_partial_listens_but_never_a_skip(self):
        """The checkbox defaults to ON, so filtering skips out the way it
        filters partial plays would empty the page entirely. A skip is the
        subject of this list; it stays an encounter either way. What the filter
        removes is the half-listen that counted as a play."""
        self._seed("t1", plays=1, skips=1, partials=2, duration=200000)

        unfiltered = self.db.getTopSongs(by="skips", limit=10)[0]
        filtered = self.db.getTopSongs(by="skips", limit=10, fullPlaysOnly=True)[0]

        self.assertEqual((unfiltered["plays"], unfiltered["encounters"]), (3, 4))
        self.assertEqual((filtered["plays"], filtered["encounters"]), (1, 2))
        self.assertEqual(filtered["skips"], 1)

    def test_full_plays_only_moves_the_ranking_not_just_the_numbers(self):
        """`halfhearted` is skipped LESS often than `solid` per encounter, so
        unfiltered it ranks below - but almost none of its plays ever finished,
        and once those stop counting as listens it is the more-skipped of the
        two. The filter has to reach the ordering, not just the numbers on the
        card."""
        self._seed("solid", plays=12, skips=6, duration=200000)          #< 33% of encounters
        self._seed("halfhearted", plays=1, partials=11, skips=4, duration=200000)   #< 25%, or 80% of real listens

        unfiltered = [s["id"] for s in self.db.getTopSongs(by="skips", limit=10)]
        filtered = [s["id"] for s in self.db.getTopSongs(by="skips", limit=10, fullPlaysOnly=True)]

        self.assertEqual(unfiltered, ["solid", "halfhearted"])
        self.assertEqual(filtered, ["halfhearted", "solid"])

    def test_the_library_average_is_not_narrowed_by_the_filters(self):
        """The prior is "this listener's own norm", so it stays whole-library
        however the page is filtered. Narrowing it to the search results would
        make a row's rank depend on what else happened to match the box next to
        it: here the two matches alone average 76% skipped, which is enough to
        carry one-encounter `thin` past `proven` - but against the real library
        average (22%, thanks to `bulk`) it stays where it belongs."""
        self._seed("bulk", plays=100, skips=0, name="Bulk")
        self._seed("thin", plays=0, skips=1, name="Alpha thin")
        self._seed("proven", plays=10, skips=30, name="Alpha proven")

        ranked = [s["id"] for s in self.db.getTopSongs(by="skips", limit=10, searchQuery="Alpha")]

        self.assertEqual(ranked, ["proven", "thin"])


class TestSortParamValidation(unittest.TestCase):
    def test_skips_is_accepted_on_the_top_pages_only(self):
        """Only the three Top pages offer it. Compare and Wrapped share the
        sortBy param but not the skip-ranked query path: Compare would render
        skip-ranked lists nobody asked for, and Wrapped's in-Python re-sort
        would silently order by a key its rows don't carry. A whitelist that
        accepts a value one page can't honour isn't a whitelist."""
        from config import VALID_SORT_BY, TOP_LIST_SORT_BY

        self.assertIn("skips", TOP_LIST_SORT_BY)
        self.assertNotIn("skips", VALID_SORT_BY)
        self.assertTrue(VALID_SORT_BY < TOP_LIST_SORT_BY)   #< the Top pages only ADD to the shared set

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
