"""Database.refreshLastfmEntity - the admin-triggered "Refresh Last.fm Data"
button's synchronous, single-entity, force-a-fresh-lookup action. Unlike the
background backfillers (tests/test_lastfm_backfiller.py) this bypasses every
"already attempted" gate. The Last.fm client is always mocked (conftest
blocks real sockets anyway)."""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
from Database.lastfm import (
    FetchOutcome, ArtistInfoOutcome, AlbumInfoOutcome,
    OUTCOME_OK, OUTCOME_NOT_FOUND, OUTCOME_TRANSIENT, OUTCOME_INVALID_KEY,
)

OK_EMPTY = FetchOutcome(OUTCOME_OK, [])
ROCK_TAGS = FetchOutcome(OUTCOME_OK, [{"name": "rock", "count": 100},
                                      {"name": "indie rock", "count": 80}])


def _album(albumId, name=None):
    return {"id": albumId, "name": name or albumId, "url": "http://example.com/album",
            "imageId": albumId, "imageUrl": "", "totalTracks": 1, "releaseDate": 0}


class RefreshLastfmEntityTestCase(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        from Database.database import Database
        Database._lastfm_active.clear()
        self.addCleanup(Database._lastfm_active.clear)

    def _makeDbWithPlays(self, username="user1"):
        tracks = {
            "tA": {"id": "tA", "name": "Song A",
                   "artists": [{"id": "aX", "name": "Artist X"}], "album": _album("alP", "Album P")},
        }
        entries = [
            {"id": "tA", "playedAt": 1000, "timePlayed": 5000},
        ]
        return self._makeDb(tracks, entries, username=username)

    def _clientReturning(self, **methodOutcomes):
        client = MagicMock()
        for method, outcome in methodOutcomes.items():
            getattr(client, method).return_value = outcome
        return client

    # ---- no key / not found -------------------------------------------------

    @patch("Database.database.LastfmClient")
    def test_no_api_key_makes_no_client(self, mockClientClass):
        db = self._makeDbWithPlays()
        result = db.refreshLastfmEntity("artist", "aX")
        self.assertEqual(result, {"status": "no_api_key"})
        mockClientClass.assert_not_called()

    def test_unknown_artist_is_not_found(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        result = db.refreshLastfmEntity("artist", "doesNotExist")
        self.assertEqual(result, {"status": "not_found"})

    def test_unknown_track_is_not_found(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        result = db.refreshLastfmEntity("track", "doesNotExist")
        self.assertEqual(result, {"status": "not_found"})

    def test_unknown_album_is_not_found(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        result = db.refreshLastfmEntity("album", "doesNotExist")
        self.assertEqual(result, {"status": "not_found"})

    def test_unknown_kind_raises(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        with self.assertRaises(ValueError):
            db.refreshLastfmEntity("playlist", "aX")

    # ---- artist ---------------------------------------------------------------

    @patch("Database.database.LastfmClient")
    def test_artist_refresh_replaces_genres_and_sets_bio(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.repo.replaceArtistGenres("aX", ["stale genre"])
        db.repo.markArtistsLastfmAttempted(["aX"])   #< already attempted long ago

        client = self._clientReturning(getArtistTopTags=ROCK_TAGS)
        client.getArtistInfo.return_value = ArtistInfoOutcome(OUTCOME_OK, "A fresh bio.")
        mockClientClass.return_value = client

        result = db.refreshLastfmEntity("artist", "aX")

        self.assertEqual(result, {"status": "ok", "name": "Artist X"})
        self.assertEqual(db.repo.getArtistGenres("aX"), ["rock", "indie rock"])
        self.assertEqual(db.getArtistBio("aX"), "A fresh bio.")
        client.getArtistTopTags.assert_called_once_with("Artist X", stop_event=db.lastfm_stop_event)

    @patch("Database.database.LastfmClient")
    def test_artist_refresh_clears_stale_genres_on_a_definitive_empty_result(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.repo.replaceArtistGenres("aX", ["stale genre"])
        db.repo.markArtistsLastfmAttempted(["aX"])

        client = self._clientReturning(getArtistTopTags=OK_EMPTY)
        client.getArtistInfo.return_value = ArtistInfoOutcome(OUTCOME_NOT_FOUND, None)
        mockClientClass.return_value = client

        result = db.refreshLastfmEntity("artist", "aX")

        self.assertEqual(result, {"status": "ok", "name": "Artist X"})
        self.assertEqual(db.repo.getArtistGenres("aX"), [])
        self.assertIsNone(db.getArtistBio("aX"))

    @patch("Database.database.LastfmClient")
    def test_artist_refresh_reports_invalid_key_without_touching_stored_data(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.repo.replaceArtistGenres("aX", ["kept genre"])

        client = self._clientReturning(getArtistTopTags=FetchOutcome(OUTCOME_INVALID_KEY, []))
        mockClientClass.return_value = client

        result = db.refreshLastfmEntity("artist", "aX")

        self.assertEqual(result, {"status": "invalid_key"})
        self.assertEqual(db.repo.getArtistGenres("aX"), ["kept genre"])
        client.getArtistInfo.assert_not_called()

    @patch("Database.database.LastfmClient")
    def test_artist_refresh_reports_transient_without_touching_stored_data(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.repo.replaceArtistGenres("aX", ["kept genre"])

        client = self._clientReturning(getArtistTopTags=FetchOutcome(OUTCOME_TRANSIENT, []))
        mockClientClass.return_value = client

        result = db.refreshLastfmEntity("artist", "aX")

        self.assertEqual(result, {"status": "transient"})
        self.assertEqual(db.repo.getArtistGenres("aX"), ["kept genre"])
        client.getArtistInfo.assert_not_called()

    # ---- album ------------------------------------------------------------

    @patch("Database.database.LastfmClient")
    def test_album_refresh_replaces_genres_and_sets_bio(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.repo.replaceAlbumGenres("alP", ["stale genre"])
        db.repo.markAlbumsLastfmAttempted(["alP"])

        client = self._clientReturning(getAlbumTopTags=ROCK_TAGS)
        client.getAlbumInfo.return_value = AlbumInfoOutcome(OUTCOME_OK, "A fresh album bio.")
        mockClientClass.return_value = client

        result = db.refreshLastfmEntity("album", "alP")

        self.assertEqual(result, {"status": "ok", "name": "Album P"})
        self.assertEqual([g["genre"] for g in db.repo.getAlbumGenres("alP")], ["rock", "indie rock"])
        self.assertFalse(any(g["inherited"] for g in db.repo.getAlbumGenres("alP")))
        self.assertEqual(db.getAlbumBio("alP"), "A fresh album bio.")
        client.getAlbumTopTags.assert_any_call("Artist X", "Album P", stop_event=db.lastfm_stop_event)
        client.getAlbumInfo.assert_called_once_with("Artist X", "Album P")

    def test_album_with_no_resolvable_artist_is_reported(self):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        with db.repo._conn() as conn:
            conn.execute(
                "INSERT INTO albums (id, name, url) VALUES (?, ?, ?)",
                ("orphanAlbum", "Orphan Album", "http://example.com/album"),
            )

        with patch("Database.database.LastfmClient") as mockClientClass:
            result = db.refreshLastfmEntity("album", "orphanAlbum")
            mockClientClass.return_value.getAlbumTopTags.assert_not_called()

        self.assertEqual(result, {"status": "no_artist"})

    # ---- track --------------------------------------------------------------

    @patch("Database.database.LastfmClient")
    def test_track_refresh_replaces_own_genres(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.repo.replaceTrackGenres("tA", ["stale genre"], inherited=False)
        db.repo.markTracksLastfmAttempted(["tA"])

        client = self._clientReturning(getTrackTopTags=ROCK_TAGS)
        mockClientClass.return_value = client

        result = db.refreshLastfmEntity("track", "tA")

        self.assertEqual(result, {"status": "ok", "name": "Song A"})
        self.assertEqual(db.repo.getTrackGenres("tA"),
                         [{"genre": "rock", "inherited": False},
                          {"genre": "indie rock", "inherited": False}])
        client.getTrackTopTags.assert_any_call("Artist X", "Song A", stop_event=db.lastfm_stop_event)

    @patch("Database.database.LastfmClient")
    def test_tagless_track_refresh_falls_back_to_artist_inheritance(self, mockClientClass):
        db = self._makeDbWithPlays()
        db.repo.updateUserLastfmApiKey("user1", "key123")
        db.repo.replaceArtistGenres("aX", ["shoegaze"])
        db.repo.markArtistsLastfmAttempted(["aX"])

        client = self._clientReturning(getTrackTopTags=FetchOutcome(OUTCOME_NOT_FOUND, []))
        mockClientClass.return_value = client

        result = db.refreshLastfmEntity("track", "tA")

        self.assertEqual(result, {"status": "ok", "name": "Song A"})
        self.assertEqual(db.repo.getTrackGenres("tA"), [{"genre": "shoegaze", "inherited": True}])


if __name__ == "__main__":
    unittest.main()
