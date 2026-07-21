import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import tempfile
from pathlib import Path
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp
from _app_factory import AppTestCase
from Database.repository import Repository

class TestOverviewRoute(AppTestCase):
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
            "lastfm_api_key": None,
            "created_at": 1718000000.0
        },
        {
            "username": "bob",
            "email": "bob@example.com",
            "cookies_json": '{"sp_dc": "456"}',
            "spotify_client_id": None,
            "spotify_refresh_token": None,
            "lastfm_api_key": None,
            "created_at": 1718000001.0
        },
    ]

    def _usersDetailsSideEffect(self):
        """Respects getAllUsersDetails' username filter, so response-content
        assertions reflect what the route actually requested."""
        def fake(username=None):
            return [u for u in self._MOCK_USERS if username is None or u["username"] == username]
        return fake

    def _getOverviewAs(self, dash):
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
             patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_user_db', return_value=mock_db):

            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'

            return client.get("/overview")

    def test_overview_shows_only_the_logged_in_users_own_status(self):
        """The full multi-user table (with per-account admin controls) lives
        on /admin now - /overview shows only a "your state" summary, no
        usernames at all, and never bob's (the other account's) data."""
        dash = self._makeApp()

        resp = self._getOverviewAs(dash)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Your Sync Status", resp.data)
        self.assertNotIn(b"bob", resp.data)
        self.assertNotIn(b"Registered Users & Sync Status", resp.data)
        self.assertIn(b"HEALTHY", resp.data)
        self.assertIn(b"CONFIGURED", resp.data)

    def test_overview_relabels_the_status_badges(self):
        """The own-status widget uses "Spotify API Backfill"/"Last.fm API
        Backfill" (renamed from the old table's "API Backfill"/"Genre
        Data"), matching the labels used on /admin's users table."""
        dash = self._makeApp()

        resp = self._getOverviewAs(dash)
        body = resp.data.decode()

        self.assertIn("Spotify API Backfill", body)
        self.assertIn("Last.fm API Backfill", body)

    def test_overview_status_qualifier_shows_regardless_of_admin_status(self):
        """A "(disabled)" qualifier on a badge label warns that the toggle
        is off instance-wide - it must show for every logged-in user, not
        just an admin (the widget itself is no longer admin-gated)."""
        dash = self._makeApp()
        dash.repo.setSpotifyApiBackfillEnabled(False)

        resp = self._getOverviewAs(dash)
        body = resp.data.decode()

        self.assertIn("Spotify API Backfill", body)
        self.assertIn("(disabled)", body)

    def test_overview_user_sync_status_one_line_layout(self):
        """User sync status section should place title and status items on the same line using sync-status-card-body on desktop."""
        dash = self._makeApp()

        resp = self._getOverviewAs(dash)
        body = resp.data.decode()

        self.assertIn('class="sync-status-card-body"', body)
        self.assertIn('class="sync-status-row"', body)
        self.assertIn('class="sync-status-item"', body)


    def test_overview_shows_needs_reauth_badge_instead_of_configured(self):
        """The 'your status' widget must show the same needs-reauth signal
        the /admin table shows for other users - it reads the identical
        getAllUsersDetails() row, and was blind to spotify_needs_reauth."""
        dash = self._makeApp()
        mock_stats = {"tracks": 10, "artists": 5, "albums": 3, "plays": 100, "total_time_ms": 36000000, "db_size_bytes": 1048576}
        mock_users = [dict(self._MOCK_USERS[0], spotify_needs_reauth=True)]
        mock_db = MagicMock()
        mock_db.getListenerHealth.return_value = {"status": "HEALTHY", "error_count": 0,
                                                    "last_error": None, "seconds_since_last_poll": 5}

        with patch.object(dash.repo, 'getGlobalDatabaseStats', return_value=mock_stats), \
             patch.object(dash.repo, 'getAllUsersDetails', return_value=mock_users), \
             patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=mock_db):

            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'

            resp = client.get("/overview")
            body = resp.data.decode()

        self.assertIn("NEEDS RE-AUTH", body)
        self.assertNotIn(">CONFIGURED<", body)

    def test_overview_does_not_start_listener_for_a_cookie_less_viewer(self):
        """get_user_db() constructs a live Database (starts the listener,
        auto-importer, and metadata/wrapped background threads). A logged-in
        user with no cookies configured must still see their own status as
        "Not Configured" without that lookup being used for the sync-status
        badge."""
        dash = self._makeApp()

        mock_stats = {"tracks": 0, "artists": 0, "albums": 0, "plays": 0, "total_time_ms": 0, "db_size_bytes": 0}
        mock_users = [
            {
                "username": "orphan",
                "email": "orphan@example.com",
                "cookies_json": None,
                "spotify_client_id": None,
                "spotify_refresh_token": None,
                "lastfm_api_key": None,
                "created_at": None,
            },
        ]
        mock_db = MagicMock()

        with patch.object(dash.repo, 'getGlobalDatabaseStats', return_value=mock_stats), \
             patch.object(dash.repo, 'getAllUsersDetails', return_value=mock_users), \
             patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='orphan'), \
             patch.object(dash, 'get_user_db', return_value=mock_db):

            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'orphan@example.com'

            resp = client.get("/overview")

            self.assertEqual(resp.status_code, 200)
            self.assertIn(b"NOT CONFIGURED", resp.data)
            mock_db.getListenerHealth.assert_not_called()


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

        # Create a test file in the media directory (conftest's _isolateMediaDir
        # points this at a per-test tmp_path, not the real Data/Media cache)
        import Database.database as databaseModule
        media_dir = databaseModule.MEDIA_DIR
        media_dir.mkdir(parents=True, exist_ok=True)
        test_file = media_dir / "test_image.jpeg"
        test_content = b"test" * 256  # Create a file with ~1KB of data
        test_file.write_bytes(test_content)

        try:
            # _calculateFolderSize() caches per path for
            # MEDIA_FOLDER_SIZE_CACHE_TTL_SECONDS (see
            # tests/test_folder_size_cache.py) - clear it so this second call
            # actually rescans the folder instead of reusing the pre-file size.
            import Database.queries.settings as settingsModule
            settingsModule._folderSizeCache.clear()

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
