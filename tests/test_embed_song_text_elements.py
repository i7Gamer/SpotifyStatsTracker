import unittest
from unittest.mock import patch
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp


class TestEmbedSongTextElementsMissingAlbum(unittest.TestCase):
    """_embedSongTextElements() must not assume song['album'] is always a dict -
    Repository._songRowToDict() can legitimately return album=None when the
    LEFT JOIN to albums finds no matching row (see
    tests/test_repository.py::test_missing_album_row_falls_back_like_get_track).
    A song in that state must still render instead of crashing every page that
    lists it (dashboard, top songs, song/artist/album detail, wrapped, ...)."""

    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _song(self, album):
        return {
            "id": "t1", "name": "Song One", "duration": 200000,
            "artists": [{"name": "Artist A"}], "album": album,
        }

    def test_missing_album_does_not_crash(self):
        dash = self._makeApp()
        song = self._song(album=None)

        result = dash._embedSongTextElements(song)

        self.assertEqual(result["releaseDateText"], "")
        self.assertEqual(result["artistsText"], "Artist A")
        self.assertIsNone(result["album"])

    def test_missing_album_in_a_list_does_not_crash(self):
        dash = self._makeApp()
        songs = [self._song(album=None), self._song(album={"releaseDate": 0})]

        result = dash._embedSongsTextElements(songs)

        self.assertEqual(len(result), 2)

    def test_present_album_still_gets_release_date_text(self):
        dash = self._makeApp()
        song = self._song(album={"releaseDate": 0})

        result = dash._embedSongTextElements(song)

        self.assertIn("releaseDateText", result["album"])
        self.assertNotEqual(result["releaseDateText"], "")


if __name__ == "__main__":
    unittest.main()
