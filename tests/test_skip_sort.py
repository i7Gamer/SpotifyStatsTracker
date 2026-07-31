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


class TestASingleTrackLookupSkipsTheRanking(SkipSortTestCase):
    """getSong falls back to this query whenever getSongsPage returns nothing -
    for a skip-only song page, and for EVERY request naming a track the user has
    never played, just before the redirect - and the detail route's shell and
    deferred body each run it once.

    Ranking one row against a prior is pointless work, and the prior is
    deliberately never narrowed by the page's filters: computing it read the
    user's entire history. A trackId filter makes the GROUP BY produce at most
    one row, so it cannot change the answer."""

    def _statementsFor(self, **kwargs):
        conn = self.db.repo._conn()
        captured = []
        conn.set_trace_callback(captured.append)
        try:
            rows = self.db.repo.getMostSkippedTracks(self.db.user, **kwargs)
        finally:
            conn.set_trace_callback(None)
        return rows, " ".join(captured)

    def test_a_single_track_lookup_does_not_read_the_whole_history(self):
        self._seed("t1", plays=1, skips=3)
        self._seed("t2", plays=8, skips=1)

        _, sql = self._statementsFor(trackId="t1", limit=1)

        self.assertNotIn("lib AS", sql)
        self.assertNotIn("SELECT rate FROM lib", sql)

    def test_the_ranked_list_still_uses_the_library_prior(self):
        """The narrowing must not leak into the path the prior exists for."""
        self._seed("t1", plays=1, skips=3)

        _, sql = self._statementsFor(limit=10)

        self.assertIn("lib AS", sql)
        self.assertIn("SELECT rate FROM lib", sql)

    def test_the_row_is_identical_either_way(self):
        """Equivalence against the un-narrowed query rather than restated
        numbers - the ranked list is asked for the same track and must agree
        field for field."""
        self._seed("t1", plays=1, skips=3)
        self._seed("t2", plays=8, skips=1)
        self._seed("t3", plays=2, skips=2)

        narrowed = self.db.repo.getMostSkippedTracks(self.db.user, trackId="t2", limit=1)
        fromRankedList = [row for row in self.db.repo.getMostSkippedTracks(self.db.user, limit=50)
                          if row["track_id"] == "t2"]

        self.assertEqual(narrowed, fromRankedList)

    def test_a_track_with_no_skips_still_comes_back_empty(self):
        """HAVING skips > 0 is what makes getSong's fallback report "no such
        song" for an unplayed id - the narrowing must not change that."""
        self._seed("t1", plays=4, skips=0)

        self.assertEqual(self.db.repo.getMostSkippedTracks(self.db.user, trackId="t1", limit=1), [])
        self.assertEqual(self.db.repo.getMostSkippedTracks(self.db.user, trackId="nope", limit=1), [])


class TestASingleEntityLookupSkipsTheRanking(SkipSortTestCase):
    """getArtist/getAlbum fall back to the skip-ranked queries the same way
    getSong does (skip-only detail pages; the route's shell and deferred body
    each run it once) - and the tracks query got the single-lookup shortcut
    above while its two twins kept computing the whole-history prior to order
    one row against nothing. An artistId/albumId filter makes the GROUP BY
    produce at most one row, so the prior cannot change the answer."""

    def _statementsFor(self, call):
        conn = self.db.repo._conn()
        captured = []
        conn.set_trace_callback(captured.append)
        try:
            rows = call()
        finally:
            conn.set_trace_callback(None)
        return rows, " ".join(captured)

    def test_a_single_artist_lookup_does_not_read_the_whole_history(self):
        self._seed("t1", plays=1, skips=3, artistId="aS")
        self._seed("t2", plays=8, skips=1, artistId="aL")

        _, sql = self._statementsFor(
            lambda: self.db.repo.getMostSkippedArtists(self.db.user, artistId="aS", limit=1))

        self.assertNotIn("lib AS", sql)
        self.assertNotIn("SELECT rate FROM lib", sql)

    def test_a_single_album_lookup_does_not_read_the_whole_history(self):
        self._seed("t1", plays=1, skips=3, albumId="alS", albumName="Album S")
        self._seed("t2", plays=8, skips=1, albumId="alL", albumName="Album L")

        _, sql = self._statementsFor(
            lambda: self.db.repo.getMostSkippedAlbums(self.db.user, albumId="alS", limit=1))

        self.assertNotIn("lib AS", sql)
        self.assertNotIn("SELECT rate FROM lib", sql)

    def test_the_ranked_lists_still_use_the_library_prior(self):
        """The narrowing must not leak into the path the prior exists for."""
        self._seed("t1", plays=1, skips=3, albumName="Album One")

        _, artistSql = self._statementsFor(
            lambda: self.db.repo.getMostSkippedArtists(self.db.user, limit=10))
        _, albumSql = self._statementsFor(
            lambda: self.db.repo.getMostSkippedAlbums(self.db.user, limit=10))

        self.assertIn("lib AS", artistSql)
        self.assertIn("lib AS", albumSql)

    def test_the_row_is_identical_either_way(self):
        """Same equivalence contract the tracks shortcut pins: the ranked list
        asked for the same entity must agree field for field."""
        self._seed("t1", plays=1, skips=3, artistId="aS", artistName="Skipped",
                   albumId="alS", albumName="Album S")
        self._seed("t2", plays=8, skips=1, artistId="aL", artistName="Loved",
                   albumId="alL", albumName="Album L")

        narrowedArtist = self.db.repo.getMostSkippedArtists(self.db.user, artistId="aL", limit=1)
        fromArtistList = [r for r in self.db.repo.getMostSkippedArtists(self.db.user, limit=50)
                          if r["id"] == "aL"]
        self.assertEqual(narrowedArtist, fromArtistList)

        narrowedAlbum = self.db.repo.getMostSkippedAlbums(self.db.user, albumId="alL", limit=1)
        fromAlbumList = [r for r in self.db.repo.getMostSkippedAlbums(self.db.user, limit=50)
                         if r["id"] == "alL"]
        self.assertEqual(narrowedAlbum, fromAlbumList)


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


class TestASkipOnlyEntityHasADetailPage(SkipSortTestCase):
    """The Most Skipped lists qualify on HAVING skips > 0, so an entity you
    encountered once and skipped (plays = 0) is listed and linked. Its detail
    lookup then has to find it, or the link bounces the user straight back to
    the list they clicked from - losing their sort, tag, range and page.

    getSong already carries this fallback; getArtist and getAlbum did not, so
    the very rows their own lists advertise were unreachable."""

    def test_a_skip_only_artist_is_listed(self):
        """Precondition: the list really does advertise the link."""
        self._seed("t1", plays=0, skips=3, artistId="skipped", artistName="Skipped")

        self.assertEqual([a["id"] for a in self.db.getTopArtists(by="skips", limit=10)], ["skipped"])

    def test_a_skip_only_artist_has_a_detail_row(self):
        self._seed("t1", plays=0, skips=3, artistId="skipped", artistName="Skipped")

        artist = self.db.getArtist("skipped")

        self.assertIsNotNone(artist, "the Most Skipped list links here, so the page must render")
        self.assertEqual(artist["name"], "Skipped")
        self.assertEqual(artist["plays"], 0)
        self.assertEqual(artist["skips"], 3)

    def test_a_skip_only_album_is_listed(self):
        self._seed("t1", plays=0, skips=3, albumId="alb1", albumName="Skipped Album")

        self.assertEqual([a["id"] for a in self.db.getTopAlbums(by="skips", limit=10)], ["alb1"])

    def test_a_skip_only_album_has_a_detail_row(self):
        self._seed("t1", plays=0, skips=3, albumId="alb1", albumName="Skipped Album")

        album = self.db.getAlbum("alb1")

        self.assertIsNotNone(album, "the Most Skipped list links here, so the page must render")
        self.assertEqual(album["name"], "Skipped Album")
        self.assertEqual(album["plays"], 0)
        self.assertEqual(album["skips"], 3)

    def test_a_played_artist_still_comes_from_the_play_ranked_query(self):
        """The fallback is a second query, not a relaxation of the first: an
        artist with real plays must still report them."""
        self._seed("t1", plays=4, skips=1, artistId="normal", artistName="Normal")

        artist = self.db.getArtist("normal")

        self.assertEqual(artist["plays"], 4)

    def test_an_unknown_artist_or_album_is_still_missing(self):
        """The redirect has to keep working for an id the user never encountered
        - the fallback must not invent a row."""
        self._seed("t1", plays=1, skips=0, artistId="art1", albumId="alb1")

        self.assertIsNone(self.db.getArtist("nope"))
        self.assertIsNone(self.db.getAlbum("nope"))


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

    def test_the_prior_ignores_the_full_plays_checkbox(self):
        """fac3da3 dropped the tracks join that had excluded partial listens from
        the prior when Full-plays-only was on (it doubled the query, 1.5s -> 3.1s
        on 400k plays, for a second-order ordering shift). That checkbox DEFAULTS
        ON, so this is the ordering nearly every visitor to a Top page sees, and
        the commit shipped with no test - re-adding the join would break nothing.

        The rule now is one rule: the prior is whole-library, always.

        The shrunk rate is ORDER BY only and never leaves the query, so the prior
        is asserted where it IS observable: the lib CTE the two calls emit has to
        be the same SQL, bound the same way. That states the rule directly, rather
        than an ordering that happens to come out equal on this fixture."""
        #< partial listens dominate, so a prior that still respected the checkbox
        #  would measure a very different average in the two calls
        self._seed("bulk", plays=2, skips=0, partials=60, duration=200_000)
        self._seed("mixed", plays=4, skips=6, partials=20, duration=200_000)

        withFilter = self._libCteFor(fullPlaysOnly=True)
        withoutFilter = self._libCteFor(fullPlaysOnly=False)

        self.assertEqual(withFilter, withoutFilter)

    def _libCteFor(self, fullPlaysOnly):
        """The `lib AS (...)` CTE as actually emitted, with its bound values -
        the prior, observably."""
        conn = self.db.repo._conn()
        captured = []
        conn.set_trace_callback(captured.append)
        try:
            self.db.getTopSongs(by="skips", limit=10, fullPlaysOnly=fullPlaysOnly)
        finally:
            conn.set_trace_callback(None)
        #< set_trace_callback expands bound params, so this compares the values too
        sql = next(statement for statement in captured if "lib AS" in statement)
        start = sql.index("lib AS")
        return " ".join(sql[start:sql.index(")", sql.index("FROM plays p", start))].split())

    def test_the_prior_query_never_joins_tracks(self):
        """The reason the prior is whole-library is a measured one, so pin the
        shape too: that join is what cost 1.5s, and the equality test above would
        still pass if someone re-added it and narrowed both halves to match."""
        self._seed("t1", plays=1, skips=1, duration=200_000)

        libClause = self._libCteFor(fullPlaysOnly=True)

        self.assertIn("FROM plays p WHERE", libClause)
        self.assertNotIn("JOIN tracks", libClause)
        self.assertNotIn("time_played", libClause)   #< the full-play predicate's column

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
