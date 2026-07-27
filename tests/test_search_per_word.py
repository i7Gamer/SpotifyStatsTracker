"""Search matches every WORD somewhere, not the whole string in one field.

The bug: the entire query was one LIKE '%...%' pattern matched against each field
separately, so the most natural way to search - "artist song" - could never match,
because no single field contains both. On a real library, "Luis Despacito"
returned nothing while "Despacito" returned nine rows.

The rule now is AND across words, OR across fields: every word must appear
somewhere (title, album, playlist, or any credited artist), not all in the same
one. Consequences that matter and are pinned below:
  - multi-word queries return a SUPERSET of the old behaviour, never less
  - a word matching nothing still yields nothing (AND, not OR)
  - word order is irrelevant
  - LIKE's own wildcards stay escaped PER WORD
"""
import datetime
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest
import Database.utils as utilsModule


def _track(trackId, name, artistName, albumName):
    return normalizeTrackForTest({
        "id": trackId, "name": name,
        "artists": [{"id": f"ar_{artistName}", "name": artistName}],
        "imageId": f"al_{albumName}",
        "album": {"id": f"al_{albumName}", "name": albumName, "url": "u",
                  "imageId": f"al_{albumName}", "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
    })


class PerWordSearchTestCase(DatabaseTestCase):
    """One track whose name, artist and album are all distinct words, so a query
    can be assembled from any combination of them."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        patcher.start()
        self.addCleanup(patcher.stop)
        tracks = {
            "t1": _track("t1", "Despacito", "Luis Fonsi", "Vida"),
            "t2": _track("t2", "Yesterday", "The Beatles", "Help"),
            "t3": _track("t3", "Percent 50%", "Odd_Name", "Sign Album"),
        }
        entries = [{"id": "t1", "playedAt": 100 + i, "timePlayed": 200000} for i in range(3)]
        entries += [{"id": "t2", "playedAt": 200 + i, "timePlayed": 200000} for i in range(2)]
        entries += [{"id": "t3", "playedAt": 300, "timePlayed": 200000}]
        self.db = self._makeDb(tracks, entries)

    def _songIds(self, query):
        return sorted(s["id"] for s in self.db.getTopSongs(searchQuery=query, limit=50))

    def _playIds(self, query):
        return sorted({e["id"] for e in self.db.searchEntries(query, count=50)})


class TestTheReportedBug(PerWordSearchTestCase):
    def test_artist_then_song_finds_the_track(self):
        """The exact shape that used to find nothing."""
        self.assertEqual(self._songIds("Luis Despacito"), ["t1"])

    def test_song_then_artist_finds_it_too(self):
        """Word order is irrelevant - people type it either way."""
        self.assertEqual(self._songIds("Despacito Luis"), ["t1"])

    def test_song_plus_album_finds_the_track(self):
        self.assertEqual(self._songIds("Despacito Vida"), ["t1"])

    def test_artist_plus_album_finds_the_track(self):
        self.assertEqual(self._songIds("Fonsi Vida"), ["t1"])

    def test_the_history_search_gets_the_same_treatment(self):
        """/history searches plays, through a different query - it must agree."""
        self.assertEqual(self._playIds("Luis Despacito"), ["t1"])
        self.assertEqual(self._playIds("Despacito Luis"), ["t1"])


class TestItStillNarrows(PerWordSearchTestCase):
    def test_every_word_must_match_something(self):
        """AND, not OR: a word present nowhere rules the row out, even when the
        other words match perfectly."""
        self.assertEqual(self._songIds("Despacito Beatles"), [])

    def test_a_word_matching_nothing_at_all_yields_nothing(self):
        self.assertEqual(self._songIds("Despacito xylophone"), [])

    def test_one_word_behaves_exactly_as_before(self):
        self.assertEqual(self._songIds("Despacito"), ["t1"])
        self.assertEqual(self._songIds("Beatles"), ["t2"])

    def test_a_word_shared_by_two_tracks_returns_both(self):
        #< "a" appears in Despacito/Vida and Yesterday/Beatles
        self.assertEqual(self._songIds("a"), ["t1", "t2", "t3"])

    def test_adding_a_word_can_only_narrow(self):
        broad = self._songIds("a")
        narrowed = self._songIds("a Despacito")

        self.assertTrue(set(narrowed).issubset(set(broad)))
        self.assertEqual(narrowed, ["t1"])


class TestWhitespaceAndWildcards(PerWordSearchTestCase):
    def test_repeated_and_surrounding_spaces_add_no_empty_word(self):
        """An empty word would become '%%' and match everything."""
        self.assertEqual(self._songIds("   Luis    Despacito  "), ["t1"])

    def test_a_whitespace_only_query_matches_everything(self):
        """No words -> no clause, which is what an empty search has always meant."""
        self.assertEqual(self._songIds("   "), ["t1", "t2", "t3"])
        self.assertEqual(self._songIds(""), ["t1", "t2", "t3"])

    def test_a_literal_percent_is_still_escaped(self):
        """LIKE's own wildcard, typed by the user, must match as text - per word."""
        self.assertEqual(self._songIds("50%"), ["t3"])
        self.assertEqual(self._songIds("%"), ["t3"])   #< not "every row"

    def test_a_literal_underscore_is_still_escaped(self):
        self.assertEqual(self._songIds("Odd_Name"), ["t3"])
        #< "_" is LIKE's single-char wildcard; unescaped it would match every row
        self.assertEqual(self._songIds("_"), ["t3"])

    def test_escaping_survives_being_split(self):
        self.assertEqual(self._songIds("Percent 50%"), ["t3"])


class TestSearchWords(unittest.TestCase):
    """The splitter itself, so the rule is readable without a database."""

    def setUp(self):
        from Database.queries.plays import PlayQueries
        self.split = PlayQueries.searchWords

    def test_it_splits_on_whitespace(self):
        self.assertEqual(self.split("luis despacito"), ["luis", "despacito"])

    def test_it_drops_empties_from_repeated_whitespace(self):
        self.assertEqual(self.split("  luis \t\n despacito "), ["luis", "despacito"])

    def test_blank_and_none_yield_no_words(self):
        self.assertEqual(self.split(""), [])
        self.assertEqual(self.split("   "), [])
        self.assertEqual(self.split(None), [])


class TestOtherPagesSearchPerWord(PerWordSearchTestCase):
    """Top Albums matches album name or a credited artist; Top Artists matches the
    artist name only (see getArtistAggregates). Both go through the same helper."""

    def test_albums_match_album_plus_artist_words(self):
        albums = sorted(a["id"] for a in self.db.getTopAlbums(searchQuery="Vida Fonsi", limit=50))

        self.assertEqual(albums, ["al_Vida"])

    def test_albums_still_narrow_on_a_missing_word(self):
        self.assertEqual(self.db.getTopAlbums(searchQuery="Vida Beatles", limit=50), [])

    def test_artists_match_multiple_words_of_one_name(self):
        artists = sorted(a["id"] for a in self.db.getTopArtists(searchQuery="Luis Fonsi", limit=50))

        self.assertEqual(artists, ["ar_Luis Fonsi"])

    def test_artists_match_those_words_in_either_order(self):
        artists = sorted(a["id"] for a in self.db.getTopArtists(searchQuery="Fonsi Luis", limit=50))

        self.assertEqual(artists, ["ar_Luis Fonsi"])


if __name__ == "__main__":
    unittest.main()
