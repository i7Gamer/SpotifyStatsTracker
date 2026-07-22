import unittest
from unittest.mock import MagicMock, patch
import sqlite3
import time

from Database.db import ConnectionManager
from Database.database import Database
from Database.lastfm import (
    LastfmClient,
    filterTagsToGenres,
    normalizeArtistLookupName,
    GENRE_TAG_ALIASES,
    OUTCOME_OK,
    FetchOutcome,
)


class TestLastfmEnhancements(unittest.TestCase):

    def test_normalize_artist_lookup_name(self):
        # Slash replacement
        res1 = normalizeArtistLookupName("Axwell /\\ Ingrosso")
        self.assertIn("Axwell & Ingrosso", res1)

        res2 = normalizeArtistLookupName("Axwell \\/ Ingrosso")
        self.assertIn("Axwell & Ingrosso", res2)

        # Plus replacement
        res3 = normalizeArtistLookupName("Florence + The Machine")
        self.assertIn("Florence and the Machine", res3)

        # Plain name returns empty list (no transformations needed)
        res4 = normalizeArtistLookupName("Cher")
        self.assertEqual(res4, [])

    def test_expanded_genre_aliases_and_whitelist(self):
        tags = [
            {"name": "synthpop", "count": "100"},
            {"name": "lofi", "count": "90"},
            {"name": "kpop", "count": "80"},
            {"name": "dnb", "count": "70"},
            {"name": "soundtrack", "count": "60"},
        ]
        genres = filterTagsToGenres(tags)
        self.assertIn("synth-pop", genres)
        self.assertIn("lo-fi", genres)
        self.assertIn("k-pop", genres)
        self.assertIn("drum and bass", genres)
        self.assertIn("soundtrack", genres)

    def test_lastfm_client_artist_name_transformation_retry(self):
        client = LastfmClient("dummy_key")

        def mock_fetch(method, params, stop_event, **kwargs):
            artist = params.get("artist")
            if artist == "Florence + The Machine":
                return FetchOutcome(OUTCOME_OK, [])  # Verbatim returns 0 tags
            elif artist == "Florence and the Machine":
                return FetchOutcome(OUTCOME_OK, [{"name": "indie rock", "count": "100"}])
            return FetchOutcome(OUTCOME_OK, [])

        with patch.object(client, "_fetchTopTags", side_effect=mock_fetch):
            outcome = client.getArtistTopTags("Florence + The Machine")
            self.assertEqual(outcome.status, OUTCOME_OK)
            self.assertEqual(len(outcome.tags), 1)
            self.assertEqual(outcome.tags[0]["name"], "indie rock")


class TestMultiArtistFallback(unittest.TestCase):

    def setUp(self):
        self.db_file = ":memory:"
        self.db = Database("testuser", dbPath=self.db_file)
        self.repo = self.db.repo


    def test_get_track_secondary_artists(self):
        conn = self.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb1', 'Album 1', 'http://alb1')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('t1', 'Track 1', 'http://t1', 'alb1')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('a1', 'Primary Artist', 'http://a1')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('a2', 'Feature Artist', 'http://a2')")
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES ('t1', 'a1', 0)")
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES ('t1', 'a2', 1)")

        secondaries = self.repo.getTrackSecondaryArtists("t1")
        self.assertEqual(len(secondaries), 1)
        self.assertEqual(secondaries[0]["artist_id"], "a2")
        self.assertEqual(secondaries[0]["artist_name"], "Feature Artist")

    def test_get_album_candidate_artists(self):
        conn = self.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb1', 'Album 1', 'http://alb1')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('t1', 'Track 1', 'http://t1', 'alb1')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('t2', 'Track 2', 'http://t2', 'alb1')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('a1', 'Various Artists', 'http://a1')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('a2', 'Real Artist 1', 'http://a2')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('a3', 'Real Artist 2', 'http://a3')")
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES ('t1', 'a1', 0)")
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES ('t1', 'a2', 1)")
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES ('t2', 'a2', 0)")
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES ('t2', 'a3', 1)")

        candidates = self.repo.getAlbumCandidateArtists("alb1")
        # a2 appears on both t1 (pos 1) and t2 (pos 0), so it should be candidate #1
        self.assertTrue(len(candidates) >= 2)
        self.assertEqual(candidates[0]["artist_id"], "a2")


    def test_track_genre_inheritance_multi_artist_fallback(self):
        client = LastfmClient("dummy_key")
        conn = self.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb1', 'Album 1', 'http://alb1')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('t1', 'Track 1', 'http://t1', 'alb1')")
            conn.execute("INSERT INTO artists (id, name, url, lastfm_attempted_at) VALUES ('a1', 'Various Artists', 'http://a1', 12345)")
            conn.execute("INSERT INTO artists (id, name, url, lastfm_attempted_at) VALUES ('a2', 'Feature Artist', 'http://a2', 12345)")
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES ('t1', 'a1', 0)")
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES ('t1', 'a2', 1)")
            # a1 has 0 genres; a2 has genres
            conn.execute("INSERT INTO artist_genres (artist_id, genre, position) VALUES ('a2', 'synth-pop', 0)")

        # Call store with inheritance for track t1, where ownGenres=[], primary artist=a1 (which has 0 genres)
        res = self.db._storeLastfmGenresWithInheritance(
            client, "track", "t1", [], "a1", "Various Artists", albumId="alb1"
        )
        self.assertTrue(res)
        genres = self.repo.getTrackGenres("t1")
        self.assertEqual(len(genres), 1)
        self.assertEqual(genres[0]["genre"], "synth-pop")
        self.assertTrue(genres[0]["inherited"])



if __name__ == "__main__":
    unittest.main()
