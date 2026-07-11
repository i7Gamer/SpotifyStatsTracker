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

    def test_deduplicate_is_a_noop_returning_zero(self):
        """The plays.UNIQUE(username, track_id, played_at) constraint makes a
        duplicate impossible to insert in the first place, so there's nothing left
        for deduplicate() to clean up - it's kept only so existing callers
        (app.py's startup path) don't break."""
        entries = [
            {"id": "e1", "playedAt": 100, "timePlayed": 1000},
            {"id": "e2", "playedAt": 300, "timePlayed": 1000},
        ]
        db = self._makeDb({}, entries)

        self.assertEqual(db.deduplicate(), 0)
        self.assertEqual(db.getEntriesCount(), 2)

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


class TestAppDeduplicateOnStartup(DatabaseTestCase):
    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Database')
    def test_get_user_db_calls_deduplicate_on_startup(self, mock_db_class, mock_migrate, mock_check, mock_version, mock_secret):
        """get_user_db must trigger deduplicate() when instantiating a user's Database."""
        from app import SpotifyDashboardApp

        mock_db_instance = MagicMock()
        mock_db_class.return_value = mock_db_instance

        app = SpotifyDashboardApp()

        app.get_user_db("test_user", "test@example.com")

        mock_db_instance.deduplicate.assert_called_once()


if __name__ == "__main__":
    import unittest
    unittest.main()
