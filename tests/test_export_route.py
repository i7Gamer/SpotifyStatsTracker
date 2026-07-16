"""GET /export-history - users can get their full play history back out.

The JSON format is shaped like Spotify's own extended streaming history
export, with `ts` as the play's END time (Spotify's convention - the
importer subtracts ms_played back off), so an export from one instance
re-imports cleanly into another through the existing import pipeline.
"""
import csv
import io
import json
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from conftest import DatabaseTestCase, makeDatabaseWithData
from app import SpotifyDashboardApp
from Database.Importers.StreamingHistoryImporter import Importer
from Database.utils import timeToInt

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'

_TRACKS = {
    "t1": {"id": "t1", "name": "First Song", "artists": [{"id": "a1", "name": "Artist One"}]},
    "t2": {"id": "t2", "name": "Second Song", "artists": [{"id": "a2", "name": "Artist Two"}]},
}
_ENTRIES = [
    {"id": "t1", "playedAt": 1700000000, "timePlayed": 200000, "playedFrom": "playlist:pl1"},
    {"id": "t2", "playedAt": 1700005000, "timePlayed": 180000},
]


class _AppTestBase(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _get(self, dash, db, path):
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(path)


class TestExportRoute(DatabaseTestCase, _AppTestBase):
    def _makeSeededDb(self):
        return self._makeDb(_TRACKS, _ENTRIES, username="alice")

    def test_logged_out_redirects_to_login(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        resp = client.get("/export-history")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_json_export_is_a_download_with_all_plays(self):
        dash = self._makeApp()
        resp = self._get(dash, self._makeSeededDb(), "/export-history?format=json")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn(".json", resp.headers["Content-Disposition"])

        items = json.loads(resp.get_data(as_text=True))
        self.assertEqual(len(items), 2)
        byUri = {item["spotify_track_uri"]: item for item in items}
        first = byUri["spotify:track:t1"]
        self.assertEqual(first["master_metadata_track_name"], "First Song")
        self.assertEqual(first["master_metadata_album_artist_name"], "Artist One")
        self.assertEqual(first["ms_played"], 200000)
        # ts is the play's END time (Spotify's extended-export convention).
        self.assertEqual(timeToInt(first["ts"]), 1700000000 + 200000 // 1000)

    def test_csv_export_has_header_and_rows(self):
        dash = self._makeApp()
        resp = self._get(dash, self._makeSeededDb(), "/export-history?format=csv")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(".csv", resp.headers["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(resp.get_data(as_text=True))))
        self.assertEqual(rows[0][:4], ["played_at_utc", "track_name", "artists", "album"])
        self.assertEqual(len(rows), 3)   #< header + 2 plays
        trackNames = {row[1] for row in rows[1:]}
        self.assertEqual(trackNames, {"First Song", "Second Song"})

    def test_unknown_format_falls_back_to_json(self):
        dash = self._makeApp()
        resp = self._get(dash, self._makeSeededDb(), "/export-history?format=xml")
        self.assertEqual(resp.status_code, 200)
        json.loads(resp.get_data(as_text=True))   #< parses as JSON

    def test_empty_history_exports_an_empty_json_list(self):
        dash = self._makeApp()
        resp = self._get(dash, self._makeDb({}, [], username="alice"), "/export-history")
        self.assertEqual(json.loads(resp.get_data(as_text=True)), [])


class TestExportRoundTrip(DatabaseTestCase, _AppTestBase):
    """The whole point of the JSON format: it re-imports through the existing
    pipeline, reproducing the same plays in a fresh database."""

    def test_export_reimports_into_a_fresh_database(self):
        dash = self._makeApp()
        sourceDb = self._makeDb(_TRACKS, _ENTRIES, username="alice")
        resp = self._get(dash, sourceDb, "/export-history?format=json")
        exportedJson = resp.get_data(as_text=True)

        # The export must parse as a recognized Spotify extended export.
        bareImporter = Importer.__new__(Importer)   #< skips Spotify client construction
        bareImporter.sp = MagicMock()
        parsed, exportType = bareImporter._convertToList(exportedJson)
        self.assertEqual(exportType, "spotifyExtendedExport")
        self.assertEqual(len(parsed), 2)

        # Import it into a fresh database seeded with the same catalog (so no
        # metadata lookups are needed) - the plays must come back identical.
        targetDb = self._makeDb(_TRACKS, [], username="bob")
        with patch("Database.database.Importer", return_value=bareImporter):
            targetDb.importHistory(exportedJson)

        imported = {(e["id"], int(e["playedAt"]), e["timePlayed"])
                    for e in targetDb.getEntriesFromOld(fullPagination=False)}
        expected = {(e["id"], e["playedAt"], e["timePlayed"]) for e in _ENTRIES}
        self.assertEqual(imported, expected)


if __name__ == "__main__":
    unittest.main()
