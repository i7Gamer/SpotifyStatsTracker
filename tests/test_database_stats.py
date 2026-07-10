import unittest
from unittest.mock import MagicMock
import sys
import os
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# See tests/test_database_images.py for why this guard is needed: other test
# modules replace Database.database with a MagicMock at import time, and unittest
# discover imports every test file before running any of them.
if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.database import Database


def _bareDatabaseWithData(tracks, entries):
    """A Database instance with just enough state for getArtistsStats/getSongsStats,
    skipping the heavy __init__ (autoimporter/listener setup) and file I/O by
    pre-seeding the in-memory caches directly."""
    db = Database.__new__(Database)
    db.fileLock = threading.RLock()
    db.tracksCache = tracks
    db.entriesCache = entries
    db.playlistsCache = None
    return db


class TestGetArtistsStatsDoesNotMutateCache(unittest.TestCase):
    def _sampleData(self):
        artist = {"name": "Artist A", "id": "a1"}
        track = {"id": "track1", "artists": [artist], "name": "Song One"}
        tracks = {"track1": track}
        entries = [
            {"id": "track1", "playedAt": 1000, "timePlayed": 5000},
            {"id": "track1", "playedAt": 2000, "timePlayed": 5000},
        ]
        return tracks, entries, artist

    def test_artist_dict_in_tracks_cache_is_unmodified_after_stats_call(self):
        tracks, entries, artist = self._sampleData()
        db = _bareDatabaseWithData(tracks, entries)

        db.getArtistsStats()

        # The artist dict living inside the shared tracks cache must not have picked
        # up derived, per-request-only fields - those belong only in the returned
        # stats list, not in the cached/persistable track metadata.
        self.assertNotIn("plays", artist)
        self.assertNotIn("totalTimeListened", artist)
        self.assertNotIn("uniqueSongs", artist)
        self.assertNotIn("uniqueSongCount", artist)
        self.assertNotIn("firstListenedAt", artist)
        self.assertEqual(artist, {"name": "Artist A", "id": "a1"})

    def test_returned_stats_are_still_correct(self):
        tracks, entries, artist = self._sampleData()
        db = _bareDatabaseWithData(tracks, entries)

        stats = db.getArtistsStats()

        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["name"], "Artist A")
        self.assertEqual(stats[0]["plays"], 2)
        self.assertEqual(stats[0]["totalTimeListened"], 10000)
        self.assertEqual(stats[0]["uniqueSongCount"], 1)

    def test_repeated_calls_do_not_accumulate_stale_state(self):
        """A second call must not be polluted by fields left over from the first."""
        tracks, entries, artist = self._sampleData()
        db = _bareDatabaseWithData(tracks, entries)

        firstStats = db.getArtistsStats()
        secondStats = db.getArtistsStats()

        self.assertEqual(firstStats[0]["plays"], secondStats[0]["plays"])
        self.assertEqual(firstStats[0]["totalTimeListened"], secondStats[0]["totalTimeListened"])


if __name__ == "__main__":
    unittest.main()
