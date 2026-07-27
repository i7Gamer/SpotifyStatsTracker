"""A song search resolves matching track ids first, then narrows the aggregate.

The search predicate used to sit inside the per-play aggregate, so its artist
EXISTS was re-evaluated for each grouped track rather than once against the
catalog. That made the cost structural, not proportional: on a real 131k-play
library the search page took ~850ms and took the same 847ms for a term matching
NOTHING. Resolving the ids first: 847ms -> 216ms for "love", 846 -> 199 for a term
with no matches, and the count 696 -> 210.

Because it is a plan change and not a behaviour change, the assertions that matter
are the equivalence ones - the resolver applies the very same condition the
aggregate used to apply inline. The shape assertions exist because a plan change
is exactly what silently evaporates on revert with every result-equivalence test
still green (see tests/test_narrowed_query_equivalence.py for the same lesson).
"""
import datetime
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest
import Database.utils as utilsModule


def _track(trackId, name, artistName, albumName, albumId=None):
    albumId = albumId or f"al_{albumName}"
    return normalizeTrackForTest({
        "id": trackId, "name": name,
        "artists": [{"id": f"ar_{artistName}", "name": artistName}],
        "imageId": albumId,
        "album": {"id": albumId, "name": albumName, "url": "u",
                  "imageId": albumId, "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
    })


class TwoPhaseSearchTestCase(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        patcher.start()
        self.addCleanup(patcher.stop)
        tracks = {
            "t1": _track("t1", "Alpha Song", "Alpha Artist", "Alpha Album"),
            "t2": _track("t2", "Beta Song", "Beta Artist", "Beta Album"),
            "t3": _track("t3", "Gamma Song", "Alpha Artist", "Gamma Album"),
        }
        entries = []
        for trackId, plays in (("t1", 5), ("t2", 3), ("t3", 1)):
            entries += [{"id": trackId, "playedAt": 1000 + len(entries) + i, "timePlayed": 200000}
                        for i in range(plays)]
        self.db = self._makeDb(tracks, entries)
        self.repo = self.db.repo
        self.user = self.db.user


class TestTheResolverAgreesWithTheAggregate(TwoPhaseSearchTestCase):
    def test_it_finds_a_track_by_a_word_of_its_own_name(self):
        self.assertEqual(sorted(self.repo.getMatchingTrackIds("Gamma")), ["t3"])

    def test_words_may_land_in_different_fields_of_the_same_track(self):
        """"Alpha Song" reaches t3 as well as t1: t3 has Alpha in its ARTIST and
        Song in its NAME. That is the per-word rule working, not a leak - and it is
        the behaviour the resolver has to reproduce exactly, since the aggregate it
        replaced applied the same condition."""
        self.assertEqual(sorted(self.repo.getMatchingTrackIds("Alpha Song")), ["t1", "t3"])

    def test_it_finds_tracks_by_album(self):
        self.assertEqual(sorted(self.repo.getMatchingTrackIds("Beta Album")), ["t2"])

    def test_it_finds_every_track_of_a_matching_artist(self):
        """t1 and t3 share an artist but nothing else."""
        self.assertEqual(sorted(self.repo.getMatchingTrackIds("Alpha Artist")), ["t1", "t3"])

    def test_it_is_catalog_wide_not_per_user(self):
        """Deliberate: the aggregate it feeds is already scoped by username, and a
        per-user resolver would have to read plays - the whole point is not to."""
        ids = self.repo.getMatchingTrackIds("Song")

        self.assertEqual(sorted(ids), ["t1", "t2", "t3"])

    def test_an_empty_query_resolves_to_nothing(self):
        """Callers guard on it, so this must not be mistaken for "everything"."""
        self.assertEqual(self.repo.getMatchingTrackIds(""), [])
        self.assertEqual(self.repo.getMatchingTrackIds("   "), [])

    def test_the_page_returns_exactly_what_the_resolver_selected(self):
        """The equivalence that makes this a plan change and not a behaviour one:
        the page's rows are the played subset of the resolver's ids, nothing else."""
        for query in ("Alpha", "Song", "Beta Album", "Alpha Artist", "nomatch"):
            with self.subTest(query=query):
                resolved = set(self.repo.getMatchingTrackIds(query))
                page = {s["id"] for s in self.db.getTopSongs(searchQuery=query, limit=50)}

                self.assertTrue(page.issubset(resolved))
                #< every resolved track HAS plays in this fixture, so it is equality
                self.assertEqual(page, resolved)

    def test_the_count_agrees_with_the_page(self):
        """They are separate queries; a pager sized off a different population is
        the bug this pairing prevents."""
        for query in ("Alpha", "Song", "nomatch"):
            with self.subTest(query=query):
                page = self.db.getTopSongs(searchQuery=query, limit=50)

                self.assertEqual(self.db.getSongsCount(searchQuery=query), len(page))


class TestTheNarrowingIsActuallyApplied(TwoPhaseSearchTestCase):
    """Shape assertions. Without these the optimisation can be reverted with every
    equivalence test above still passing."""

    def _sqlFor(self, call):
        conn = self.repo._conn()
        captured = []
        conn.set_trace_callback(captured.append)
        try:
            call()
        finally:
            conn.set_trace_callback(None)
        return " ".join(captured)

    def test_the_page_narrows_by_a_json_id_set(self):
        sql = self._sqlFor(lambda: self.db.getTopSongs(searchQuery="Alpha", limit=50))

        self.assertIn("json_each", sql)

    def test_the_count_narrows_by_a_json_id_set(self):
        sql = self._sqlFor(lambda: self.db.getSongsCount(searchQuery="Alpha"))

        self.assertIn("json_each", sql)

    def test_the_aggregate_no_longer_carries_the_artist_exists(self):
        """That subquery is what cost the time - it belongs in the catalog phase,
        evaluated once, not per grouped track."""
        sql = self._sqlFor(lambda: self.db.getTopSongs(searchQuery="Alpha", limit=50))
        aggregate = sql[sql.index("FROM plays p"):] if "FROM plays p" in sql else sql

        self.assertNotIn("ta3", aggregate)

    def test_an_unsearched_page_carries_no_narrowing(self):
        """Negative control: the assertions above would pass for any SQL if the
        clause were emitted unconditionally."""
        sql = self._sqlFor(lambda: self.db.getTopSongs(limit=50))

        self.assertNotIn("json_each", sql)

    def test_a_term_matching_nothing_short_circuits(self):
        """An empty id set becomes `AND 0`, so SQLite skips the aggregate instead
        of scanning the history to find nothing."""
        sql = self._sqlFor(lambda: self.db.getTopSongs(searchQuery="nomatch", limit=50))

        self.assertIn("AND 0", sql)
        self.assertEqual(self.db.getTopSongs(searchQuery="nomatch", limit=50), [])


class TestSearchCombinedWithOtherFilters(TwoPhaseSearchTestCase):
    """The search id set and the tag filter's id set are separate clauses on the
    same column, so they must intersect rather than one replacing the other."""

    def test_search_and_a_tag_filter_intersect(self):
        tagged = ["t1", "t2"]

        songs = self.db.getTopSongs(searchQuery="Alpha Artist", trackIds=tagged, limit=50)

        #< "Alpha Artist" reaches t1+t3; the tag reaches t1+t2; only t1 is in both
        self.assertEqual([s["id"] for s in songs], ["t1"])

    def test_a_disjoint_tag_and_search_return_nothing(self):
        songs = self.db.getTopSongs(searchQuery="Beta", trackIds=["t1", "t3"], limit=50)

        self.assertEqual(songs, [])

    def test_search_still_composes_with_full_plays_only(self):
        songs = self.db.getTopSongs(searchQuery="Alpha Artist", fullPlaysOnly=True, limit=50)

        self.assertEqual(sorted(s["id"] for s in songs), ["t1", "t3"])

    def test_search_still_composes_with_a_date_range(self):
        start = datetime.datetime.fromtimestamp(0, tz=datetime.timezone.utc)
        end = datetime.datetime.fromtimestamp(2000, tz=datetime.timezone.utc)

        songs = self.db.getTopSongs(startDate=start, endDate=end,
                                    searchQuery="Alpha Artist", limit=50)

        self.assertEqual(sorted(s["id"] for s in songs), ["t1", "t3"])


class TestTheHistorySearch(TwoPhaseSearchTestCase):
    """/history matches what was played AND where it was played FROM, so its
    narrowing needs two id sets. This is the half that lost three joins - including
    a LEFT JOIN playlists matched on substr(played_from, instr(...)), which no index
    could serve - so the playlist behaviour is what needs pinning hardest.

    The page was never actually fast: it looked fast for common terms because
    LIMIT 50 stops early. "radiohead" took 886ms to return zero rows."""

    def setUp(self):
        super().setUp()
        self.repo.upsertPlaylistName("pl1", "playlist", "Roadtrip Mix")
        self.repo.upsertPlaylistName("pl2", "playlist", "Study Beats")
        self.repo.commit()
        #< a play of a track that matches NOTHING by name, sourced from a playlist
        #  whose name does - only the played_from side can find it
        self.repo.insertPlay(self.user, "t2", 5000, 200000, "playlist:pl1")
        self.repo.commit()

    def _found(self, query):
        return sorted(e["id"] for e in self.db.searchEntries(query, count=50))

    def test_a_play_is_found_by_the_playlist_it_came_from(self):
        self.assertIn("t2", self._found("Roadtrip"))

    def test_a_playlist_match_finds_only_plays_sourced_from_it(self):
        """t2 has other plays with no played_from; only the sourced one qualifies,
        so the count is what distinguishes a correct played_from filter."""
        self.assertEqual(self.db.searchEntriesCount("Roadtrip"), 1)

    def test_a_non_matching_playlist_finds_nothing(self):
        self.assertEqual(self._found("Study"), [])

    def test_track_and_playlist_matches_are_ORed_not_ANDed(self):
        """"Beta" matches t2 by name (3 plays) and nothing by playlist; the
        playlist-sourced play is a 4th. All four must come back."""
        self.assertEqual(self.db.searchEntriesCount("Beta"), 4)

    def test_a_term_matching_neither_side_finds_nothing(self):
        self.assertEqual(self._found("nomatch"), [])
        self.assertEqual(self.db.searchEntriesCount("nomatch"), 0)

    def test_the_page_and_the_count_agree(self):
        for query in ("Beta", "Roadtrip", "Alpha", "nomatch"):
            with self.subTest(query=query):
                page = self.db.searchEntries(query, count=500)

                self.assertEqual(self.db.searchEntriesCount(query), len(page))

    def test_per_word_search_spans_track_and_playlist(self):
        """"Beta Roadtrip" - one word from the track, one from the playlist. Both
        must be satisfied, and only the sourced play satisfies both."""
        self.assertEqual(self.db.searchEntriesCount("Beta Roadtrip"), 1)

    def test_the_history_query_no_longer_joins_playlists(self):
        """Shape assertion: the substr/instr join is what the id sets replaced, and
        it would be silently reintroduced by anyone "restoring" the old predicate."""
        conn = self.repo._conn()
        captured = []
        conn.set_trace_callback(captured.append)
        try:
            self.db.searchEntries("Beta", count=50)
        finally:
            conn.set_trace_callback(None)
        sql = " ".join(captured)

        self.assertIn("json_each", sql)
        self.assertNotIn("instr(", sql)

    def test_a_playlist_named_search_still_escapes_wildcards(self):
        self.repo.upsertPlaylistName("pl3", "playlist", "100% Focus")
        self.repo.commit()
        self.repo.insertPlay(self.user, "t3", 6000, 200000, "playlist:pl3")
        self.repo.commit()

        self.assertEqual(self.db.searchEntriesCount("100%"), 1)


if __name__ == "__main__":
    unittest.main()
