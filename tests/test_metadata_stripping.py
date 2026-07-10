import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import threading
from pathlib import Path
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.database import Database
from Database.Importers.StreamingHistoryImporter import Importer

FAKE_TRACK_METADATA = {
    "id": "track999",
    "name": "Metadata Only Song",
    "duration_ms": 180000,
    "external_urls": {"spotify": "https://open.spotify.com/track/track999"},
    "album": {
        "id": "album999",
        "name": "Album Name",
        "external_urls": {"spotify": "https://open.spotify.com/album/album999"},
        "images": [{"url": "https://example.com/img.jpg"}],
        "total_tracks": 12,
        "release_date": "2021-05-05",
        "artists": [{
            "name": "Artist Name",
            "id": "artist999",
            "external_urls": {"spotify": "https://open.spotify.com/artist/artist999"},
        }],
    },
}

class TestMetadataStripping(unittest.TestCase):
    def test_save_new_track_from_id_strips_play_fields(self):
        # Create a bare database instance
        db = Database.__new__(Database)
        db.listener = MagicMock()
        db.listener.track.return_value = FAKE_TRACK_METADATA
        db._saveTracks = MagicMock()
        
        tracks = {}
        db._saveNewTrackFromId("track999", tracks, deferSave=True)
        
        self.assertIn("track999", tracks)
        saved_track = tracks["track999"]
        
        # Verify play-specific fields are NOT present
        self.assertNotIn("playedAt", saved_track)
        self.assertNotIn("timePlayed", saved_track)
        self.assertNotIn("playedFrom", saved_track)

    def test_prefetch_missing_tracks_strips_play_fields(self):
        importer = Importer()
        importer.sp = MagicMock()
        importer.sp.track.return_value = FAKE_TRACK_METADATA
        
        missing_tracks = {
            "track999": ("Metadata Only Song", "Artist Name", "track999")
        }
        known = {}
        
        importer._prefetchMissingTracks(missing_tracks, 0, 1, known, progressCallback=None)
        
        self.assertIn("track999", known)
        cached_track = known["track999"]
        
        # Verify play-specific fields are NOT present in the cache
        self.assertNotIn("playedAt", cached_track)
        self.assertNotIn("timePlayed", cached_track)
        self.assertNotIn("playedFrom", cached_track)

if __name__ == "__main__":
    unittest.main()
