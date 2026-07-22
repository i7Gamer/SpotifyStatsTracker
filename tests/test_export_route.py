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
from _app_factory import AppTestCase
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


class _AppTestBase(AppTestCase):
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


class TestExportBehavioralFields(DatabaseTestCase, _AppTestBase):
    """Behavioral columns round-trip under Spotify's own key names; skip
    events follow the plays as sub-threshold entries (JSON only)."""

    EXTRAS = {
        "platform": "ios", "conn_country": "CH", "reason_start": "clickrow",
        "reason_end": "trackdone", "shuffle": 1, "skipped": 0, "offline": 1, "incognito": 0,
    }

    def _exportItems(self, db):
        dash = self._makeApp()
        resp = self._get(dash, db, "/export-history?format=json")
        return json.loads(resp.get_data(as_text=True))

    def test_behavioral_fields_use_spotify_key_names(self):
        db = self._makeDb(_TRACKS, [], username="alice")
        db.repo.insertPlay("alice", "t1", 1700000000, 200000, extras=self.EXTRAS)
        db.repo.commit()

        item = self._exportItems(db)[0]

        self.assertEqual(item["platform"], "ios")
        self.assertEqual(item["conn_country"], "CH")
        self.assertEqual(item["reason_start"], "clickrow")
        self.assertEqual(item["reason_end"], "trackdone")
        self.assertIs(item["shuffle"], True)
        self.assertIs(item["skipped"], False)
        self.assertIs(item["offline"], True)
        self.assertIs(item["incognito_mode"], False)
        self.assertNotIn("incognito", item)   #< only the Spotify key name
        # Offline plays carry their corrected start so a reimport reconstructs it
        self.assertEqual(item["offline_timestamp"], 1700000000)

    def test_entries_without_behavioral_fields_omit_the_keys(self):
        db = self._makeDb(_TRACKS, _ENTRIES, username="alice")

        item = self._exportItems(db)[0]

        for key in ("platform", "conn_country", "reason_start", "reason_end",
                    "shuffle", "skipped", "offline", "incognito_mode", "offline_timestamp"):
            self.assertNotIn(key, item)

    def test_online_play_has_no_offline_timestamp(self):
        db = self._makeDb(_TRACKS, [], username="alice")
        db.repo.insertPlay("alice", "t1", 1700000000, 200000, extras={"offline": 0, "platform": "ios"})
        db.repo.commit()

        item = self._exportItems(db)[0]

        self.assertIs(item["offline"], False)
        self.assertNotIn("offline_timestamp", item)

    def test_skips_are_exported_after_plays(self):
        db = self._makeDb(_TRACKS, _ENTRIES, username="alice")
        db.repo.insertPlay("alice", "t2", 1700009000, 400, is_skip=1, extras={"reason_end": "fwdbtn"})
        db.repo.commit()

        items = self._exportItems(db)

        self.assertEqual(len(items), 3)
        skipItem = items[-1]   #< skips follow every play
        self.assertEqual(skipItem["ms_played"], 400)
        self.assertEqual(skipItem["spotify_track_uri"], "spotify:track:t2")
        self.assertEqual(skipItem["reason_end"], "fwdbtn")

    def test_csv_export_stays_plays_only(self):
        db = self._makeDb(_TRACKS, _ENTRIES, username="alice")
        db.repo.insertPlay("alice", "t2", 1700009000, 400, is_skip=1)
        db.repo.commit()

        dash = self._makeApp()
        resp = self._get(dash, db, "/export-history?format=csv")
        rows = list(csv.reader(io.StringIO(resp.get_data(as_text=True))))

        self.assertEqual(len(rows), 3)   #< header + the 2 plays, no skip row


class TestSkipAndOfflineRoundTrip(DatabaseTestCase, _AppTestBase):
    def test_offline_play_and_skip_survive_a_reimport(self):
        sourceDb = self._makeDb(_TRACKS, [], username="alice")
        sourceDb.repo.insertPlay("alice", "t1", 1700000000, 200000,
                                 extras={"platform": "ios", "offline": 1, "reason_end": "trackdone"})
        sourceDb.repo.insertPlay("alice", "t2", 1700005000, 400, is_skip=1,
                                 extras={"reason_end": "fwdbtn", "skipped": 1})
        sourceDb.repo.commit()

        dash = self._makeApp()
        exportedJson = self._get(dash, sourceDb, "/export-history?format=json").get_data(as_text=True)

        bareImporter = Importer.__new__(Importer)
        bareImporter.sp = MagicMock()
        targetDb = self._makeDb(_TRACKS, [], username="bob")
        with patch("Database.database.Importer", return_value=bareImporter):
            targetDb.importHistory(exportedJson)

        playRows = targetDb.repo._conn().execute(
            "SELECT * FROM plays WHERE username='bob' AND is_skip=0").fetchall()
        self.assertEqual(len(playRows), 1)
        play = dict(playRows[0])
        self.assertEqual(play["played_at"], 1700000000)   #< corrected offline start survives
        self.assertEqual(play["platform"], "ios")
        self.assertEqual(play["offline"], 1)
        self.assertEqual(play["reason_end"], "trackdone")

        skipRows = targetDb.repo._conn().execute(
            "SELECT * FROM plays WHERE username='bob' AND is_skip=1").fetchall()
        self.assertEqual(len(skipRows), 1)
        skip = dict(skipRows[0])
        self.assertEqual(skip["track_id"], "t2")
        self.assertEqual(skip["played_at"], 1700005000)
        self.assertEqual(skip["time_played"], 400)
        self.assertEqual(skip["reason_end"], "fwdbtn")


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
