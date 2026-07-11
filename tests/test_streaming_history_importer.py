import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.Importers.StreamingHistoryImporter import Importer

MUSICOLET_CSV = (
    "FILE_PATH,TITLE,ARTIST,ALBUM,ALBUM_ARTIST,COMPOSER,GENRE,YEAR,DURATION_MS,PLAY_COUNT\n"
    "/music/song.mp3,Song One,Artist One,Album One,Artist One,,Pop,2020,200000,1\n"
)

FAKE_TRACK = {
    "id": "track123",
    "name": "Song One",
    "external_urls": {"spotify": "https://open.spotify.com/track/track123"},
    "duration_ms": 200000,
    "explicit": False,
    "disc_number": 1,
    "track_number": 1,
    "external_ids": {"isrc": "ABC123"},
    "album": {
        "id": "album123",
        "name": "Album One",
        "external_urls": {"spotify": "https://open.spotify.com/album/album123"},
        "images": [{"url": "https://example.com/img.jpg"}],
        "total_tracks": 10,
        "release_date": "2020-01-01",
        "artists": [{
            "name": "Artist One",
            "id": "artist123",
            "external_urls": {"spotify": "https://open.spotify.com/artist/artist123"},
        }],
    },
}


class TestMusicoletImport(unittest.TestCase):
    def _mockedImporter(self):
        importer = Importer()
        importer.sp = MagicMock()
        importer.sp.search.return_value = {"tracks": {"items": [FAKE_TRACK]}}
        return importer

    def test_import_history_dispatches_musicolet_without_error(self):
        """importHistory() must accept a progressCallback for every export type,
        including musicoletPremium, without raising a TypeError."""
        importer = self._mockedImporter()
        parsedHistory, exportType = importer._convertToList(MUSICOLET_CSV)
        self.assertEqual(exportType, "musicoletPremium")

        progressCalls = []

        def progressCallback(status, current, total, message):
            progressCalls.append((status, current, total, message))

        result = importer.importHistory(parsedHistory, known=[], exportType=exportType, progressCallback=progressCallback)
        tracks = list(result)

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["name"], "Song One")

    def test_import_musicolet_csv_export_accepts_progressCallback_directly(self):
        importer = self._mockedImporter()
        gen = importer.importMusicoletCSVExport(MUSICOLET_CSV.splitlines()[1:], known=[], progressCallback=lambda *a: None)
        tracks = list(gen)
        self.assertEqual(len(tracks), 1)

    def test_prefetch_progress_callback_is_monotonic(self):
        importer = self._mockedImporter()
        
        # We need two missing tracks to show multiple pre-fetch progress steps
        history = [
            ("Track A", "Artist A", "2023-01-01 00:00:00", 180000, "uriA"),
            ("Track B", "Artist B", "2023-01-01 00:03:00", 180000, "uriB"),
        ]
        
        # Mock SpotipyFree search/track to return valid items
        importer.sp.track.side_effect = [
            {"id": "uriA", "name": "Track A", "external_urls": {"spotify": "http://a"}, "duration_ms": 180000, "album": {"images": []}},
            {"id": "uriB", "name": "Track B", "external_urls": {"spotify": "http://b"}, "duration_ms": 180000, "album": {"images": []}},
        ]
        
        progressCalls = []
        def progressCallback(status, current, total, message):
            progressCalls.append((status, current, total, message))
            
        def dummyDataFunction(item):
            return item
            
        # Run import generator to trigger pre-fetch and yielding
        tracks = list(importer._import(dummyDataFunction, history, known=[], progressCallback=progressCallback))
        
        # Filter progress calls that are pre-fetching
        prefetchCalls = [c for c in progressCalls if "Pre-fetching" in c[3]]
        
        # We expect 2 calls, with current values: 1 and 2
        self.assertEqual(len(prefetchCalls), 2)
        self.assertEqual(prefetchCalls[0][1], 1)
        self.assertEqual(prefetchCalls[1][1], 2)


class TestResolveKnownKey(unittest.TestCase):
    def _importer(self):
        importer = Importer()
        importer.sp = MagicMock()
        return importer

    def test_prefers_trackUri_when_both_keys_are_known(self):
        importer = self._importer()
        known = {"uri1": {"id": "uri1"}, "NameArtist": {"id": "other"}}
        self.assertEqual(importer._resolveKnownKey("uri1", "Name", "Artist", known), "uri1")

    def test_falls_back_to_name_artist_key_when_trackUri_not_known(self):
        """A trackUri absent from the cache must not stop matching by name+artist -
        e.g. a reissue/remaster URI for a song already cached under its name+artist."""
        importer = self._importer()
        known = {"NameArtist": {"id": "cached"}}
        self.assertEqual(importer._resolveKnownKey("uriNotCached", "Name", "Artist", known), "NameArtist")

    def test_returns_none_when_neither_key_is_known(self):
        importer = self._importer()
        self.assertIsNone(importer._resolveKnownKey("uriX", "Name", "Artist", {}))

    def test_returns_none_without_trackUri_or_name_and_artist(self):
        importer = self._importer()
        self.assertIsNone(importer._resolveKnownKey(None, None, None, {"anything": {}}))


class TestFetchTrackMeta(unittest.TestCase):
    def _importer(self):
        importer = Importer()
        importer.sp = MagicMock()
        return importer

    def test_uses_track_lookup_when_trackUri_given(self):
        importer = self._importer()
        importer.sp.track.return_value = {"id": "abc"}
        result = importer._fetchTrackMeta("Song", "Artist", "abc")
        importer.sp.track.assert_called_once_with("abc")
        self.assertEqual(result, {"id": "abc"})

    def test_falls_back_to_search_when_track_lookup_fails(self):
        importer = self._importer()
        importer.sp.track.side_effect = Exception("not found")
        importer.sp.search.return_value = {"tracks": {"items": [FAKE_TRACK]}}
        result = importer._fetchTrackMeta("Song One", "Artist One", "abc")
        self.assertEqual(result, FAKE_TRACK)

    def test_searches_directly_when_no_trackUri(self):
        importer = self._importer()
        importer.sp.search.return_value = {"tracks": {"items": [FAKE_TRACK]}}
        result = importer._fetchTrackMeta("Song One", "Artist One", None)
        importer.sp.track.assert_not_called()
        self.assertEqual(result, FAKE_TRACK)


if __name__ == "__main__":
    unittest.main()
