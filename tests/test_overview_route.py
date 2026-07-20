import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import tempfile
from pathlib import Path
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp
from Database.repository import Repository

class TestOverviewRoute(unittest.TestCase):
    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def test_overview_guest_access(self):
        dash = self._makeApp()
        
        mock_stats = {"tracks": 10, "artists": 5, "albums": 3, "plays": 100, "total_time_ms": 36000000, "db_size_bytes": 1048576}
        with patch.object(dash.repo, 'getGlobalDatabaseStats', return_value=mock_stats):
            client = dash.app.test_client()
            resp = client.get("/overview")
            
            self.assertEqual(resp.status_code, 200)
            self.assertIn(b"10", resp.data) # Tracks
            self.assertIn(b"5", resp.data)  # Artists
            self.assertIn(b"3", resp.data)  # Albums
            self.assertIn(b"100", resp.data) # Plays
            self.assertNotIn(b"Registered Users & Sync Status", resp.data)

    _MOCK_USERS = [
        {
            "username": "alice",
            "email": "alice@example.com",
            "cookies_json": '{"sp_dc": "123"}',
            "spotify_client_id": "client_id",
            "spotify_refresh_token": "refresh_token",
            "created_at": 1718000000.0
        },
        {
            "username": "bob",
            "email": "bob@example.com",
            "cookies_json": '{"sp_dc": "456"}',
            "spotify_client_id": None,
            "spotify_refresh_token": None,
            "created_at": 1718000001.0
        },
    ]

    def _usersDetailsSideEffect(self):
        """Respects getAllUsersDetails' username filter, so response-content
        assertions reflect what the route actually requested."""
        def fake(username=None):
            return [u for u in self._MOCK_USERS if username is None or u["username"] == username]
        return fake

    def _getOverviewAs(self, dash, isAdmin):
        mock_stats = {"tracks": 10, "artists": 5, "albums": 3, "plays": 100, "total_time_ms": 36000000, "db_size_bytes": 1048576}
        mock_db = MagicMock()
        mock_db.getListenerHealth.return_value = {
            "status": "HEALTHY",
            "error_count": 0,
            "last_error": None,
            "seconds_since_last_poll": 5
        }

        with patch.object(dash.repo, 'getGlobalDatabaseStats', return_value=mock_stats), \
             patch.object(dash.repo, 'getAllUsersDetails', side_effect=self._usersDetailsSideEffect()), \
             patch.object(dash.repo, 'isAdmin', return_value=isAdmin), \
             patch.object(dash.repo, 'getPlaysCount', return_value=123), \
             patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_user_db', return_value=mock_db):

            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'

            return client.get("/overview")

    def test_overview_admin_sees_every_user(self):
        dash = self._makeApp()

        resp = self._getOverviewAs(dash, isAdmin=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Registered Users & Sync Status", resp.data)
        self.assertIn(b"alice", resp.data)
        self.assertIn(b"bob", resp.data)
        self.assertIn(b"HEALTHY", resp.data)
        self.assertIn(b"CONFIGURED", resp.data)
        self.assertIn(b"123", resp.data)

    def test_overview_admin_sees_total_skips_column(self):
        dash = self._makeApp()
        with patch.object(dash.repo, 'getSkipCount', return_value=42):
            resp = self._getOverviewAs(dash, isAdmin=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Total Skips", resp.data)
        self.assertIn(b"42", resp.data)

    def test_overview_last_user_row_has_no_bottom_border(self):
        """Every row but the last gets a separator border; the last row's
        border would otherwise double up against the table's own bottom
        edge."""
        dash = self._makeApp()

        resp = self._getOverviewAs(dash, isAdmin=True)

        body = resp.data.decode()
        aliceRowStart = body.find("<tr", body.find(">alice<") - 200)
        bobRowStart = body.find("<tr", body.find(">bob<") - 200)
        aliceRow = body[aliceRowStart:body.find(">alice<")]
        bobRow = body[bobRowStart:body.find(">bob<")]
        self.assertIn("border-bottom", aliceRow)
        self.assertNotIn("border-bottom", bobRow)

    def test_overview_non_admin_sees_only_their_own_row(self):
        """The per-user table (usernames, sync state, play counts of OTHER
        accounts) is admin-only - a regular user still sees their own sync
        status, but nobody else's."""
        dash = self._makeApp()

        resp = self._getOverviewAs(dash, isAdmin=False)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Your Sync Status", resp.data)
        self.assertIn(b"alice", resp.data)
        self.assertNotIn(b"bob", resp.data)

    def test_overview_does_not_start_listener_for_cookie_less_users(self):
        """get_user_db() constructs a live Database (starts the listener,
        auto-importer, and metadata/wrapped background threads) - it must
        never be called just to render a row for a user who has never
        logged in (cookies_json is None), only to report their status as
        "Not Configured"."""
        dash = self._makeApp()

        mock_stats = {"tracks": 0, "artists": 0, "albums": 0, "plays": 0, "total_time_ms": 0, "db_size_bytes": 0}
        mock_users = [
            {
                "username": "alice",
                "email": "alice@example.com",
                "cookies_json": '{"sp_dc": "123"}',
                "spotify_client_id": None,
                "spotify_refresh_token": None,
                "created_at": None,
            },
            {
                "username": "orphan",
                "email": "orphan@example.com",
                "cookies_json": None,
                "spotify_client_id": None,
                "spotify_refresh_token": None,
                "created_at": None,
            },
        ]

        mock_db = MagicMock()
        mock_db.getListenerHealth.return_value = {
            "status": "HEALTHY",
            "error_count": 0,
            "last_error": None,
            "seconds_since_last_poll": 1,
        }

        with patch.object(dash.repo, 'getGlobalDatabaseStats', return_value=mock_stats), \
             patch.object(dash.repo, 'getAllUsersDetails', return_value=mock_users), \
             patch.object(dash.repo, 'isAdmin', return_value=True), \
             patch.object(dash.repo, 'getPlaysCount', return_value=0), \
             patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_user_db', return_value=mock_db) as mock_get_user_db:

            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'

            resp = client.get("/overview")

            self.assertEqual(resp.status_code, 200)
            self.assertIn(b"orphan", resp.data)   #< the user's row itself still renders

            calledUsernames = [call.args[0] for call in mock_get_user_db.call_args_list]
            self.assertNotIn("orphan", calledUsernames)
            self.assertIn("alice", calledUsernames)   #< the logged-in viewer's own db lookup is still expected


class TestOverviewDatabaseStats(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")

    def tearDown(self):
        self.repo.connectionManager.close()
        self._tmpdir.cleanup()

    def test_repository_get_global_stats(self):
        stats = self.repo.getGlobalDatabaseStats()
        self.assertEqual(stats["tracks"], 0)
        self.assertEqual(stats["artists"], 0)
        self.assertEqual(stats["albums"], 0)
        self.assertEqual(stats["plays"], 0)
        self.assertEqual(stats["total_time_ms"], 0)
        self.assertGreaterEqual(stats["db_size_bytes"], 0)

        conn = self.repo._conn()
        with conn:
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('a1', 'Artist 1', '')")
            conn.execute("INSERT INTO albums (id, name, url, total_tracks) VALUES ('al1', 'Album 1', '', 1)")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('t1', 'Track 1', '', 'al1')")
            conn.execute("INSERT INTO users (username, email, created_at) VALUES ('u1', 'u1@example.com', 123.0)")
            conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) VALUES ('u1', 't1', 12345.6, 2000)")

        stats = self.repo.getGlobalDatabaseStats()
        self.assertEqual(stats["tracks"], 1)
        self.assertEqual(stats["artists"], 1)
        self.assertEqual(stats["albums"], 1)
        self.assertEqual(stats["plays"], 1)
        self.assertEqual(stats["total_time_ms"], 2000)
        self.assertGreater(stats["db_size_bytes"], 0)

    def test_repository_get_global_stats_includes_media_folder(self):
        """Verify that db_size_bytes includes both database and media folder sizes."""
        stats_before = self.repo.getGlobalDatabaseStats()
        db_size_before = stats_before["db_size_bytes"]

        # Create a test file in the media directory
        media_dir = Path(__file__).resolve().parent.parent / "Database" / "Data" / "Media"
        media_dir.mkdir(parents=True, exist_ok=True)
        test_file = media_dir / "test_image.jpeg"
        test_content = b"test" * 256  # Create a file with ~1KB of data
        test_file.write_bytes(test_content)

        try:
            stats_after = self.repo.getGlobalDatabaseStats()
            db_size_after = stats_after["db_size_bytes"]
            # The size should have increased by at least the size of our test file
            self.assertGreaterEqual(db_size_after - db_size_before, len(test_content))
        finally:
            # Cleanup
            if test_file.exists():
                test_file.unlink()

    def test_repository_get_all_users_details(self):
        users = self.repo.getAllUsersDetails()
        self.assertEqual(len(users), 0)

        self.repo.upsertUser("u1", "u1@example.com", 123.0)
        self.repo.setUserCookies("u1", {"sp_dc": "cookie_val"})
        self.repo.updateUserSpotifyCredentials("u1", "client", "secret", "refresh")

        users = self.repo.getAllUsersDetails()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["username"], "u1")
        self.assertEqual(users[0]["email"], "u1@example.com")
        self.assertEqual(users[0]["spotify_client_id"], "client")
        # The overview page only checks token PRESENCE (bool) - this listing
        # deliberately returns the raw stored value, which is encrypted at
        # rest (see Database/secret_store.py), never the decrypted secret.
        from Database.secret_store import ENCRYPTED_PREFIX
        self.assertTrue(users[0]["spotify_refresh_token"])
        self.assertTrue(users[0]["spotify_refresh_token"].startswith(ENCRYPTED_PREFIX))
