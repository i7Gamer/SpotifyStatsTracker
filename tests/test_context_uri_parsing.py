"""Database.playlistName / Database.updatePlaylists must tolerate malformed
playedFrom context values (no colon, empty) instead of raising ValueError on
tuple unpacking - a corrupted played_from row would otherwise 500 the history
page via app._embedSongTextElements."""
import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase


class TestPlaylistNameUriParsing(DatabaseTestCase):
    def _db(self):
        return self._makeDb(tracks={}, entries=[])

    def test_valid_playlist_uri_resolves_name(self):
        db = self._db()
        db.repo.upsertPlaylistName("pl1", "playlist", "Road Trip Mix")
        self.assertEqual(db.playlistName("playlist:pl1"), "Road Trip Mix")

    def test_none_and_empty_return_none(self):
        db = self._db()
        self.assertIsNone(db.playlistName(None))
        self.assertIsNone(db.playlistName(""))

    def test_colonless_uri_returns_none_instead_of_raising(self):
        db = self._db()
        self.assertIsNone(db.playlistName("playlist"))
        self.assertIsNone(db.playlistName("garbage-no-colon"))


class TestUpdatePlaylistsUriParsing(DatabaseTestCase):
    def _db(self):
        return self._makeDb(tracks={}, entries=[])

    def test_none_is_a_noop(self):
        db = self._db()
        db.updatePlaylists(None)   #< must not raise

    def test_colonless_uri_is_skipped_without_raising(self):
        db = self._db()
        db.updatePlaylists("playlist")
        db.updatePlaylists("garbage-no-colon")
        self.assertFalse(db.repo.playlistKnown("playlist", "playlist"))

    def test_known_playlist_short_circuits(self):
        db = self._db()
        db.repo.upsertPlaylistName("pl1", "playlist", "Existing")
        db.updatePlaylists("playlist:pl1")   #< must not hit the listener
        self.assertEqual(db.repo.getPlaylistName("pl1", "playlist"), "Existing")


if __name__ == "__main__":
    unittest.main()
