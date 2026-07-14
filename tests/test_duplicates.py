import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest


def _meta(trackId, playedAt, timePlayed=1000):
    """A full importer-yielded item: entry fields + enough track metadata to
    satisfy Repository.upsertTrack (mirrors what Client.formatTrack produces)."""
    track = normalizeTrackForTest({"id": trackId, "name": f"Song {trackId}", "artists": []})
    track["playedAt"] = playedAt
    track["timePlayed"] = timePlayed
    track["playedFrom"] = None
    return track


class TestDatabaseDeduplication(DatabaseTestCase):
    def _mockImporter(self, generatorFactory, parsedCount=2):
        importer = MagicMock()
        importer._convertToList.return_value = ([{}] * parsedCount, "spotifyAcountExport")
        importer.importHistory.return_value = generatorFactory()
        return importer


    def test_import_history_ignores_already_existing_entries(self):
        """importHistory must not add entries that are already in the database."""
        entries = [
            {"id": "e1", "playedAt": 100, "timePlayed": 1000},
            {"id": "e2", "playedAt": 300, "timePlayed": 1000},
        ]
        db = self._makeDb({}, entries)

        def gen():
            # i1 is new, e1 already exists (id=e1, playedAt=100)
            yield _meta("i1", 200)
            yield _meta("e1", 100)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        playedAts = [e["playedAt"] for e in db.getEntriesFromNew(fullPagination=False)]
        self.assertEqual(len(playedAts), 3)
        self.assertIn(100, playedAts)
        self.assertIn(200, playedAts)
        self.assertIn(300, playedAts)

    def test_import_history_ignores_duplicates_in_the_import_source_itself(self):
        """importHistory must not import duplicate entries present in the source file."""
        db = self._makeDb({}, [])

        def gen():
            yield _meta("i1", 200)
            yield _meta("i1", 200)  # Duplicate within source

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        playedAts = [e["playedAt"] for e in db.getEntriesFromNew(fullPagination=False)]
        self.assertEqual(playedAts.count(200), 1)
        self.assertEqual(len(playedAts), 1)

    def test_import_updates_both_fields_when_single_duplicate_with_different_data(self):
        """When import finds exactly one duplicate within tolerance window with
        different time_played or played_at, it should update the existing play
        with the imported data (treating import as source of truth)."""
        # DB: play at playedAt=100 with timePlayed=5000
        entries = [{"id": "track_x", "playedAt": 100, "timePlayed": 5000}]
        db = self._makeDb({}, entries)

        def gen():
            # Import: same track at playedAt=105 (within tolerance) with timePlayed=6000
            yield _meta("track_x", 105, timePlayed=6000)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        # Should have 1 entry (updated, not new)
        plays = db.getEntriesFromNew(fullPagination=False)
        self.assertEqual(len(plays), 1)
        # Should be updated to import's values
        self.assertEqual(plays[0]["playedAt"], 105)
        self.assertEqual(plays[0]["timePlayed"], 6000)

    def test_import_skips_when_single_duplicate_with_same_data(self):
        """When import finds exactly one duplicate with identical data,
        skip without updating (no change needed)."""
        entries = [{"id": "track_x", "playedAt": 100, "timePlayed": 5000}]
        db = self._makeDb({}, entries)

        def gen():
            # Import: same track at same timestamp with same duration
            yield _meta("track_x", 100, timePlayed=5000)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        plays = db.getEntriesFromNew(fullPagination=False)
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0]["playedAt"], 100)
        self.assertEqual(plays[0]["timePlayed"], 5000)

    def test_import_skips_when_multiple_duplicates_in_window(self):
        """When import finds multiple duplicates within tolerance window,
        skip to avoid ambiguity (don't guess which play to update)."""
        # DB: two plays of same track within close time
        entries = [
            {"id": "track_x", "playedAt": 100, "timePlayed": 5000},
            {"id": "track_x", "playedAt": 110, "timePlayed": 4500},
        ]
        db = self._makeDb({}, entries)

        def gen():
            # Import: same track at playedAt=105 (within tolerance of both)
            # with timePlayed=6000, which differs from both DB entries
            yield _meta("track_x", 105, timePlayed=6000)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        plays = db.getEntriesFromNew(fullPagination=False)
        # Should still have both original entries (nothing updated)
        self.assertEqual(len(plays), 2)
        play_times = sorted([p["timePlayed"] for p in plays])
        self.assertEqual(play_times, [4500, 5000])

    def test_import_inserts_when_no_duplicate_in_window(self):
        """When import finds no duplicate within tolerance window,
        insert as new entry (unchanged behavior)."""
        entries = [{"id": "track_x", "playedAt": 200, "timePlayed": 5000}]
        db = self._makeDb({}, entries)

        def gen():
            # Import: same track at playedAt=100 (far outside tolerance window)
            yield _meta("track_x", 100, timePlayed=6000)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        plays = db.getEntriesFromNew(fullPagination=False)
        # Should have both entries (new one inserted)
        self.assertEqual(len(plays), 2)
        played_ats = sorted([p["playedAt"] for p in plays])
        self.assertEqual(played_ats, [100, 200])



if __name__ == "__main__":
    import unittest
    unittest.main()
