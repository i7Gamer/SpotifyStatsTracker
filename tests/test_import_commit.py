import json
import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import threading
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# See tests/test_database_images.py for why this guard is needed.
if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.database import Database


def _meta(trackId, playedAt):
    """Minimal importer-yielded metadata: entry fields + track metadata."""
    return {
        "id": trackId,
        "playedAt": playedAt,
        "timePlayed": 1000,
        "playedFrom": None,
        "name": f"Song {trackId}",
        "artists": [],
    }


class TestImportHistoryCommit(unittest.TestCase):
    """importHistory must commit atomically: a mid-import failure may not leave
    half-imported, unsorted entries in the shared cache (later saves would persist
    them, breaking the sorted-order assumption filterByInterval relies on), and a
    successful import may not drop entries the listener recorded meanwhile."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)

        db = Database.__new__(Database)
        db.fileLock = threading.RLock()
        db.entriesCache = [
            {"id": "e1", "playedAt": 100, "timePlayed": 1000},
            {"id": "e2", "playedAt": 300, "timePlayed": 1000},
        ]
        db.tracksCache = {"e1": {"id": "e1", "name": "Song e1", "artists": []},
                          "e2": {"id": "e2", "name": "Song e2", "artists": []}}
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

    def test_successful_import_merges_and_sorts(self):
        def gen():
            yield _meta("i1", 200)
            yield _meta("i2", 50)   #< out of order on purpose

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            self.db.importHistory("raw export")

        playedAts = [e["playedAt"] for e in self.db.entriesCache]
        self.assertEqual(playedAts, [50, 100, 200, 300])
        self.assertIn("i1", self.db.tracksCache)
        self.assertIn("i2", self.db.tracksCache)

        onDisk = json.loads(self.db.entriesPath.read_text(encoding="utf-8"))
        self.assertEqual([e["playedAt"] for e in onDisk], [50, 100, 200, 300])
        self.assertEqual(json.loads(self.db.progressPath.read_text(encoding="utf-8"))["status"], "complete")

    def test_failed_import_leaves_caches_and_disk_untouched(self):
        def gen():
            yield _meta("i1", 200)
            raise RuntimeError("network died mid-import")

        entriesBefore = [dict(e) for e in self.db.entriesCache]
        tracksBefore = dict(self.db.tracksCache)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            with self.assertRaises(RuntimeError):
                self.db.importHistory("raw export")

        self.assertEqual(self.db.entriesCache, entriesBefore)
        self.assertEqual(self.db.tracksCache, tracksBefore)
        self.assertFalse(self.db.entriesPath.exists(), "a failed import must not persist anything")
        self.assertEqual(json.loads(self.db.progressPath.read_text(encoding="utf-8"))["status"], "failed")

    def test_listener_entries_recorded_during_import_are_kept(self):
        def gen():
            yield _meta("i1", 200)
            # Simulate the listener recording a play while the import is running.
            self.db.appendEntries({"id": "L1", "playedAt": 250, "timePlayed": 500})
            yield _meta("i2", 50)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            self.db.importHistory("raw export")

        ids = [e["id"] for e in self.db.entriesCache]
        self.assertIn("L1", ids)
        self.assertEqual([e["playedAt"] for e in self.db.entriesCache], [50, 100, 200, 250, 300])

    def test_unrecognized_export_is_a_noop(self):
        importer = MagicMock()
        importer._convertToList.return_value = ([], "None")

        with patch("Database.database.Importer", return_value=importer):
            self.db.importHistory("not an export")

        self.assertEqual(len(self.db.entriesCache), 2)
        importer.importHistory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
