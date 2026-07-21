import unittest
from unittest.mock import patch
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp
from _app_factory import AppTestCase


class TestEmbedSongTextElementsMissingAlbum(AppTestCase):
    """_embedSongTextElements() must not assume song['album'] is always a dict -
    Repository._songRowToDict() can legitimately return album=None when the
    LEFT JOIN to albums finds no matching row (see
    tests/test_repository.py::test_missing_album_row_falls_back_like_get_track).
    A song in that state must still render instead of crashing every page that
    lists it (dashboard, top songs, song/artist/album detail, wrapped, ...)."""

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

    def test_present_album_with_unknown_release_date_gets_blank_text(self):
        """releaseDate=0 is the app-wide sentinel for "unknown" (used by
        synthetic tracks and albums the metadata backfiller hasn't reached
        yet - see Repository.upsertTrack/_createSyntheticTrack) - it must
        render as blank, not as the Unix epoch date."""
        dash = self._makeApp()
        song = self._song(album={"releaseDate": 0})

        result = dash._embedSongTextElements(song)

        self.assertIn("releaseDateText", result["album"])
        self.assertEqual(result["releaseDateText"], "")

    def test_present_album_with_known_release_date_gets_release_date_text(self):
        dash = self._makeApp()
        song = self._song(album={"releaseDate": 946684800})   #< 2000-01-01

        result = dash._embedSongTextElements(song)

        self.assertNotEqual(result["releaseDateText"], "")


class TestEmbedAlbumTextElementsReleaseDate(AppTestCase):
    """_embedAlbumTextElements() backs the Top Albums, Wrapped, and
    album-detail pages - same unknown-release-date sentinel (releaseDate=0,
    the value Repository.upsertTrack/_createSyntheticTrack use for an album
    with no known release date) as _embedSongTextElements()."""

    def _album(self, **overrides):
        album = {"id": "alb1", "name": "Album One", "artists": []}
        album.update(overrides)
        return album

    def test_unknown_release_date_gets_blank_text(self):
        dash = self._makeApp()
        with dash.app.app_context():
            result = dash._embedAlbumTextElements(self._album(releaseDate=0))

        self.assertEqual(result["releaseDateText"], "")

    def test_missing_release_date_key_gets_blank_text(self):
        dash = self._makeApp()
        with dash.app.app_context():
            result = dash._embedAlbumTextElements(self._album())

        self.assertEqual(result["releaseDateText"], "")

    def test_known_release_date_gets_release_date_text(self):
        dash = self._makeApp()
        with dash.app.app_context():
            result = dash._embedAlbumTextElements(self._album(releaseDate=946684800))   #< 2000-01-01

        self.assertNotEqual(result["releaseDateText"], "")


if __name__ == "__main__":
    unittest.main()
