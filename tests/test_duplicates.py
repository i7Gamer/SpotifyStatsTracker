import json
import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import threading
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Ensure real database import
if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.database import Database
from app import SpotifyDashboardApp


def _meta(trackId, playedAt, timePlayed=1000):
    """Minimal importer-yielded metadata: entry fields + track metadata."""
    return {
        "id": trackId,
        "playedAt": playedAt,
        "timePlayed": timePlayed,
        "playedFrom": None,
        "name": f"Song {trackId}",
        "artists": [],
    }


class TestDatabaseDeduplication(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)

        db = Database.__new__(Database)
        db.fileLock = threading.RLock()
        db.entriesCache = [
            {"id": "e1", "playedAt": 100, "timePlayed": 1000},
            {"id": "e2", "playedAt": 300, "timePlayed": 1000},
            {"id": "e1", "playedAt": 100, "timePlayed": 1000},  # Duplicate on numeric ts
            {"id": "e3", "playedAt": "2026-07-10T19:00:00Z", "timePlayed": 1000},
            {"id": "e3", "playedAt": "2026-07-10T19:00:00Z", "timePlayed": 1000},  # Duplicate on ISO string ts
        ]
        db.tracksCache = {
            "e1": {"id": "e1", "name": "Song e1", "artists": []},
            "e2": {"id": "e2", "name": "Song e2", "artists": []},
            "e3": {"id": "e3", "name": "Song e3", "artists": []},
        }
        db.playlistsCache = {"album": {}, "playlist": {}}
        db.entriesPath = tmp / "entries.json"
        db.tracksPath = tmp / "tracks.json"
        db.progressPath = tmp / "progress.json"
        db.cookiesFile = None
        db.email = None
        db.saveImagesFromTrack = MagicMock()
        self.db = db

    def _mockImporter(self, generatorFactory, parsedCount=2):
        importer = MagicMock()
        importer._convertToList.return_value = ([{}] * parsedCount, "spotifyAcountExport")
        importer.importHistory.return_value = generatorFactory()
        return importer

    def test_deduplicate_removes_duplicates_successfully(self):
        """deduplicate() must clean up existing duplicates in the cache and on disk."""
        # Initial call
        removed = self.db.deduplicate()
        self.assertEqual(removed, 2)
        
        # Verify cache has only unique entries
        expected_entries = [
            {"id": "e1", "playedAt": 100, "timePlayed": 1000},
            {"id": "e2", "playedAt": 300, "timePlayed": 1000},
            {"id": "e3", "playedAt": "2026-07-10T19:00:00Z", "timePlayed": 1000},
        ]
        self.assertEqual(self.db.entriesCache, expected_entries)

        # Verify it was saved to disk
        on_disk = json.loads(self.db.entriesPath.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, expected_entries)

        # A second deduplicate call should do nothing and return 0
        self.assertEqual(self.db.deduplicate(), 0)

    def test_import_history_ignores_already_existing_entries(self):
        """importHistory must not add entries that are already in the database."""
        # Clean the duplicates first so we start clean
        self.db.deduplicate()

        def gen():
            # i1 is new, e1 already exists (id=e1, playedAt=100)
            yield _meta("i1", 200)
            yield _meta("e1", 100)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            self.db.importHistory("raw export")

        # e1 must NOT be duplicated. Final entries should be:
        # e3 (ts=1773255600 or string), e1 (100), i1 (200), e2 (300)
        # Note: the sorting is by timestamp, "2026-07-10T19:00:00Z" -> 1773255600, so it goes last
        played_ats = [e["playedAt"] for e in self.db.entriesCache]
        self.assertEqual(len(self.db.entriesCache), 4)
        self.assertIn(100, played_ats)
        self.assertIn(200, played_ats)
        self.assertIn(300, played_ats)

    def test_import_history_ignores_duplicates_in_the_import_source_itself(self):
        """importHistory must not import duplicate entries present in the source file."""
        self.db.deduplicate()

        def gen():
            yield _meta("i1", 200)
            yield _meta("i1", 200)  # Duplicate within source

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            self.db.importHistory("raw export")

        played_ats = [e["playedAt"] for e in self.db.entriesCache]
        # Should only have one instance of i1 at 200
        self.assertEqual(played_ats.count(200), 1)
        self.assertEqual(len(self.db.entriesCache), 4)


class TestAppDeduplicateOnStartup(unittest.TestCase):
    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Database')
    def test_get_user_db_calls_deduplicate_on_startup(self, mock_db_class, mock_migrate, mock_check, mock_version, mock_secret):
        """get_user_db must trigger deduplicate() when instantiating a user's Database."""
        mock_db_instance = MagicMock()
        mock_db_class.return_value = mock_db_instance

        app = SpotifyDashboardApp()
        app.cookiesFile = Path("/dummy/cookies.json")

        app.get_user_db("test_user", "test@example.com")

        mock_db_instance.deduplicate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
