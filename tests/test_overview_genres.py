"""The genre-backfill progress card + per-user Genre Data column on /overview,
and the admin-only inherited-genres toggle."""
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp
from test_charts_genres import coverageDict


class OverviewGenresTestBase(unittest.TestCase):
    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    _MOCK_STATS = {"tracks": 10, "artists": 5, "albums": 3, "plays": 100,
                   "total_time_ms": 36000000, "db_size_bytes": 1048576}

    _MOCK_USERS = [
        {
            "username": "alice", "email": "alice@example.com",
            "cookies_json": '{"sp_dc": "123"}',
            "spotify_client_id": "client_id", "spotify_refresh_token": "refresh_token",
            "lastfm_api_key": "enc:v1:something",
            "created_at": 1718000000.0,
        },
        {
            "username": "bob", "email": "bob@example.com",
            "cookies_json": '{"sp_dc": "456"}',
            "spotify_client_id": None, "spotify_refresh_token": None,
            "lastfm_api_key": None,
            "created_at": 1718000001.0,
        },
    ]

    def _usersDetailsSideEffect(self):
        def fake(username=None):
            return [u for u in self._MOCK_USERS if username is None or u["username"] == username]
        return fake

    def _makeDb(self, coverage=None, workerStatus=None):
        db = MagicMock()
        db.getListenerHealth.return_value = {"status": "HEALTHY", "error_count": 0,
                                             "last_error": None, "seconds_since_last_poll": 5}
        if coverage is not None:
            db.getGenreCoverage.return_value = coverage
        if workerStatus is not None:
            db.getLastfmWorkerStatus.return_value = workerStatus
        return db

    def _getOverview(self, dash, db, isAdmin=False):
        with patch.object(dash.repo, 'getGlobalDatabaseStats', return_value=self._MOCK_STATS), \
             patch.object(dash.repo, 'getAllUsersDetails', side_effect=self._usersDetailsSideEffect()), \
             patch.object(dash.repo, 'isAdmin', return_value=isAdmin), \
             patch.object(dash.repo, 'getPlaysCount', return_value=123), \
             patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_user_db', return_value=db):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get("/overview")


class TestOverviewGenreCard(OverviewGenresTestBase):
    def test_guest_page_renders_without_the_progress_card(self):
        dash = self._makeApp()
        with patch.object(dash.repo, 'getGlobalDatabaseStats', return_value=self._MOCK_STATS):
            resp = dash.app.test_client().get("/overview")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"Genre Backfill Progress", resp.data)

    def test_logged_in_user_sees_their_progress_card(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(29, 90, 45))

        resp = self._getOverview(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Genre Backfill Progress", resp.data)
        self.assertIn(b"29", resp.data)
        self.assertIn(b"90", resp.data)
        self.assertIn(b"45", resp.data)
        self.assertIn(b"Last.fm", resp.data)

    def test_unstubbed_magicmock_db_degrades_to_zeros(self):
        dash = self._makeApp()
        resp = self._getOverview(dash, self._makeDb())
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Genre Backfill Progress", resp.data)
        self.assertIn(b"NO API KEY", resp.data)   #< sanitized worker status defaults

    def test_worker_badges_reflect_the_real_status(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          workerStatus={"configured": True, "running": True})
        resp = self._getOverview(dash, db)
        self.assertIn(b"WORKER RUNNING", resp.data)
        self.assertIn(b"UNLOCKED", resp.data)

    def test_users_table_gets_a_genre_data_column(self):
        dash = self._makeApp()
        resp = self._getOverview(dash, self._makeDb(), isAdmin=True)
        body = resp.data.decode()
        self.assertIn("Genre Data", body)
        # alice has a stored key, bob doesn't.
        aliceRow = body[body.find(">alice<"):body.find(">bob<")]
        self.assertIn("CONFIGURED", aliceRow)
        bobRow = body[body.find(">bob<"):]
        self.assertIn("NOT CONFIGURED", bobRow)


class TestInheritedGenresToggle(OverviewGenresTestBase):
    def test_toggle_form_is_admin_only(self):
        dash = self._makeApp()
        respAdmin = self._getOverview(dash, self._makeDb(), isAdmin=True)
        self.assertIn(b"genre_settings", respAdmin.data)
        respUser = self._getOverview(dash, self._makeDb(), isAdmin=False)
        self.assertNotIn(b"genre_settings", respUser.data)

    def _post(self, dash, isAdmin, data, loggedIn=True):
        with patch.object(dash.repo, 'isAdmin', return_value=isAdmin), \
             patch.object(dash, 'is_user_logged_in', return_value=loggedIn), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=self._makeDb()):
            client = dash.app.test_client()
            if loggedIn:
                with client.session_transaction() as sess:
                    sess['email'] = 'alice@example.com'
                    sess['username'] = 'alice'
            return client.post("/overview/genre_settings", data=data)

    def test_admin_can_flip_the_setting(self):
        dash = self._makeApp()
        self.assertTrue(dash.repo.isInheritedGenresEnabled())

        resp = self._post(dash, isAdmin=True, data={})   #< unchecked checkbox = disable

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/overview", resp.headers["Location"])
        self.assertFalse(dash.repo.isInheritedGenresEnabled())

        resp = self._post(dash, isAdmin=True, data={"include_inherited": "1"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(dash.repo.isInheritedGenresEnabled())

    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, isAdmin=False, data={"include_inherited": "1"})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(dash.repo.isInheritedGenresEnabled())   #< unchanged

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._post(dash, isAdmin=False, data={}, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])


if __name__ == "__main__":
    unittest.main()
