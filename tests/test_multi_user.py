import unittest
from unittest.mock import patch, MagicMock, mock_open
import sys
import os
import threading
from pathlib import Path

# Ensure we can import app.py
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# NOTE: this file deliberately does NOT swap Database modules for MagicMocks in
# sys.modules. Side effects are avoided with per-test patches (app.Database,
# app.migrateIfNeeded, threads, _get_or_create_secret_key) instead - a
# module-level mock/restore here would poison the patch("Database.database...")
# targets of test files that run after this one, which used to silently send
# tests to the real network.
from app import SpotifyDashboardApp

# Patch target for the Flask secret key so instantiating the app in tests never
# regenerates the real secrets/flask_secret_key.txt (mocked Path.exists would
# otherwise force a rewrite, invalidating live sessions).
_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'

class TestMultiUser(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    @patch('app.Path.read_text')
    def test_get_username_for_email_timorzipa(self, mock_read_text, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        # Mock secrets map not existing
        mock_exists.return_value = False
        app = SpotifyDashboardApp()
        username = app.get_username_for_email("timorzipa@gmail.com")
        self.assertIsNone(username)

    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    @patch('app.Path.read_text')
    def test_get_username_for_email_from_map(self, mock_read_text, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = True
        mock_read_text.return_value = '{"test@example.com": "test_user"}'

        app = SpotifyDashboardApp()
        username = app.get_username_for_email("test@example.com")
        self.assertEqual(username, "test_user")

    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    @patch('app.Path.mkdir')
    @patch('app.Path.write_text')
    def test_get_or_create_user(self, mock_write_text, mock_mkdir, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        # Everything does not exist
        mock_exists.return_value = False
        app = SpotifyDashboardApp()

        username = app.get_or_create_user("john.doe@test.com")
        self.assertEqual(username, "johndoe")

    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.Database')   #< get_user_db must not build a real Database (files, threads, network)
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def test_get_user_db_cache(self, mock_exists, mock_migrate, mock_check, mock_version, mock_database, mock_secret):
        mock_exists.return_value = False
        app = SpotifyDashboardApp()

        db1 = app.get_user_db("Tzur", "timorzipa@gmail.com")
        db2 = app.get_user_db("Tzur", "timorzipa@gmail.com")

        self.assertIs(db1, db2) # Should be the exact same object from cache
        mock_database.assert_called_once()

    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    def test_migrate_legacy_database(self, mock_migrate, mock_check, mock_version, mock_secret):
        import tempfile
        import shutil

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            app = SpotifyDashboardApp()
            app.baseDir = tmp_path

            # Create a legacy directory with entries.json
            legacy_dir = tmp_path / "Database" / "Users" / "Tzur"
            legacy_dir.mkdir(parents=True, exist_ok=True)
            legacy_entries = legacy_dir / "entries.json"
            legacy_entries.write_text('[{"id": "track_1"}]', encoding="utf-8")

            # Run migration to target "timorzipa"
            app._migrate_legacy_database_if_needed("timorzipa")

            # Target directory should now exist and have the copied entries.json
            target_dir = tmp_path / "Database" / "Users" / "timorzipa"
            target_entries = target_dir / "entries.json"

            self.assertTrue(target_entries.exists())
            self.assertEqual(target_entries.read_text(encoding="utf-8"), '[{"id": "track_1"}]')

            # Legacy directory should be removed/cleaned up
            self.assertFalse(legacy_dir.exists())

    def test_get_or_create_secret_key_persists_random_value(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            dash = SpotifyDashboardApp.__new__(SpotifyDashboardApp)
            dash.baseDir = Path(tmpdir)

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FLASK_SECRET_KEY", None)
                key1 = dash._get_or_create_secret_key()
                key2 = dash._get_or_create_secret_key()

            self.assertEqual(key1, key2)
            self.assertNotEqual(key1, "spotify-stats-tracker-secret")
            self.assertGreaterEqual(len(key1), 32)

    def test_get_or_create_secret_key_prefers_env_var(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            dash = SpotifyDashboardApp.__new__(SpotifyDashboardApp)
            dash.baseDir = Path(tmpdir)

            with patch.dict(os.environ, {"FLASK_SECRET_KEY": "my-env-secret"}):
                key = dash._get_or_create_secret_key()

            self.assertEqual(key, "my-env-secret")


class TestImageRouteAuthorization(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def test_serve_track_image_denies_mismatched_user(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get('/img/bob/tracks/1.jpeg')
        self.assertEqual(resp.status_code, 404)

    def test_serve_track_image_denies_unauthenticated(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=False):
            resp = client.get('/img/alice/tracks/1.jpeg')
        self.assertEqual(resp.status_code, 404)

    @patch('app.send_from_directory')
    def test_serve_track_image_allows_matching_user(self, mock_send):
        mock_send.return_value = "OK"
        dash = self._makeApp()
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get('/img/alice/tracks/1.jpeg')
        self.assertEqual(resp.status_code, 200)
        mock_send.assert_called_once()

    def test_serve_artist_image_denies_mismatched_user(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get('/img/bob/artists/1.jpeg')
        self.assertEqual(resp.status_code, 404)

    def test_serve_image_rejects_path_traversal_filename(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get('/img/alice/tracks/..%5C..%5Csecret.txt')
        self.assertEqual(resp.status_code, 404)


class TestSessionLockScope(unittest.TestCase):
    """_session_lock exists to protect users_map.json / cookies.json reads and
    writes from corruption under concurrent access - it must not also serialize
    unrelated, slow work (like a live Spotify network call) across all users."""

    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    def _makeApp(self, mock_migrate, mock_check, mock_version):
        import tempfile
        app = SpotifyDashboardApp.__new__(SpotifyDashboardApp)
        app.baseDir = Path(tempfile.mkdtemp())
        app.cookiesFile = app.baseDir / "secrets" / "cookies.json"
        app.user_databases = {}
        app._db_lock = threading.RLock()
        app._session_lock = threading.RLock()
        app._migration_lock = threading.RLock()
        app._login_cache = {}
        return app


    def test_slow_listener_check_does_not_block_unrelated_session_lookups(self):
        import json
        import time

        dash = self._makeApp()
        dash.cookiesFile.parent.mkdir(parents=True, exist_ok=True)
        dash.cookiesFile.write_text(json.dumps([{"identifier": "alice@example.com"}]), encoding="utf-8")

        usersMapFile = dash.baseDir / "secrets" / "users_map.json"
        usersMapFile.write_text(json.dumps({"alice@example.com": "alice"}), encoding="utf-8")

        slowDb = MagicMock()
        slowDb.isListenerLoggedIn.side_effect = lambda: time.sleep(0.3) or True
        dash.user_databases["alice"] = slowDb

        thread = threading.Thread(target=lambda: dash.is_user_logged_in("alice@example.com"))
        thread.start()
        time.sleep(0.05)  # let the slow call start and take the lock

        start = time.time()
        dash.get_username_for_email("bob@example.com")  # unrelated user, no network involved
        elapsed = time.time() - start

        thread.join()

        self.assertLess(
            elapsed, 0.2,
            "an unrelated session lookup blocked on another user's live listener check "
            "- the session lock's critical section is too broad"
        )

    def test_two_new_users_do_not_migrate_the_same_legacy_source_concurrently(self):
        """get_or_create_user() no longer runs legacy migration under the (now
        narrower) session lock, so it needs its own lock: the legacy sources are
        fixed, shared paths, so two different brand-new users logging in around the
        same time could otherwise both race to migrate the same source."""
        import time

        dash = self._makeApp()

        concurrentCount = {"current": 0, "max": 0}
        countLock = threading.Lock()

        def fakeMigrate(username):
            with countLock:
                concurrentCount["current"] += 1
                concurrentCount["max"] = max(concurrentCount["max"], concurrentCount["current"])
            time.sleep(0.1)
            with countLock:
                concurrentCount["current"] -= 1

        with patch.object(dash, 'get_username_for_email', return_value=None), \
             patch.object(dash, '_migrate_legacy_database_if_needed', side_effect=fakeMigrate):

            threads = [
                threading.Thread(target=dash.get_or_create_user, args=(f"user{i}@example.com",))
                for i in range(3)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(concurrentCount["max"], 1, "legacy migration ran concurrently for different users")


class TestLoginCookieVerification(unittest.TestCase):
    """Login must verify that the submitted cookies actually belong to the claimed
    email before persisting them. Without this, anyone can claim any email with
    arbitrary cookies and be handed that user's database (and clobber the real
    user's stored session)."""

    # Keep tests from regenerating the real secrets/flask_secret_key.txt
    # (the mocked Path.exists below would otherwise force a rewrite).
    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _postLogin(self, dash, email="alice@example.com", cookies="sp_dc=abc"):
        client = dash.app.test_client()
        resp = client.post("/login", data={"step": "2", "email": email, "cookies": cookies})
        return resp, client

    def test_login_rejects_unverified_cookies(self):
        dash = self._makeApp()
        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=False), \
             patch('app.saveSession') as mock_save:
            resp, client = self._postLogin(dash)

        self.assertEqual(resp.status_code, 200)  #< re-renders login page instead of redirecting
        mock_save.assert_not_called()
        with client.session_transaction() as sess:
            self.assertNotIn('email', sess)

    def test_login_accepts_verified_cookies(self):
        dash = self._makeApp()
        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch('app.saveSession') as mock_save, \
             patch.object(dash, 'get_or_create_user', return_value='alice'), \
             patch.object(dash, 'get_user_db'):
            resp, client = self._postLogin(dash)

        self.assertEqual(resp.status_code, 302)
        mock_save.assert_called_once()
        self.assertIs(mock_save.call_args.args[2], dash.cookiesFile)
        with client.session_transaction() as sess:
            self.assertEqual(sess.get('email'), 'alice@example.com')

    def test_verify_accepts_matching_profile_email_case_insensitive(self):
        dash = self._makeApp()
        spotify = MagicMock()
        spotify.isLoggedIn.return_value = True
        spotify.current_user.return_value = {"email": "Alice@Example.com"}
        with patch('app.SpotipyFree') as mock_sf, patch('app.saveSession'):
            mock_sf.Spotify.return_value = spotify
            result = dash._verifyCookiesMatchEmail({"sp_dc": "abc"}, "alice@example.com")
        self.assertTrue(result)

    def test_verify_rejects_mismatched_profile_email(self):
        dash = self._makeApp()
        spotify = MagicMock()
        spotify.isLoggedIn.return_value = True
        spotify.current_user.return_value = {"email": "attacker@evil.com"}
        with patch('app.SpotipyFree') as mock_sf, patch('app.saveSession'):
            mock_sf.Spotify.return_value = spotify
            result = dash._verifyCookiesMatchEmail({"sp_dc": "abc"}, "alice@example.com")
        self.assertFalse(result)

    def test_verify_rejects_when_not_logged_in(self):
        dash = self._makeApp()
        spotify = MagicMock()
        spotify.isLoggedIn.return_value = False
        with patch('app.SpotipyFree') as mock_sf, patch('app.saveSession'):
            mock_sf.Spotify.return_value = spotify
            result = dash._verifyCookiesMatchEmail({"sp_dc": "abc"}, "alice@example.com")
        self.assertFalse(result)

    def test_verify_rejects_on_spotify_error(self):
        dash = self._makeApp()
        with patch('app.SpotipyFree') as mock_sf, patch('app.saveSession'):
            mock_sf.Spotify.side_effect = RuntimeError("network down")
            result = dash._verifyCookiesMatchEmail({"sp_dc": "abc"}, "alice@example.com")
        self.assertFalse(result)

    def test_verify_rejects_empty_inputs(self):
        dash = self._makeApp()
        with patch('app.SpotipyFree') as mock_sf:
            self.assertFalse(dash._verifyCookiesMatchEmail({}, "alice@example.com"))
            self.assertFalse(dash._verifyCookiesMatchEmail({"sp_dc": "abc"}, ""))
            mock_sf.Spotify.assert_not_called()

    def test_verify_never_touches_shared_cookies_file_and_cleans_up_temp(self):
        dash = self._makeApp()
        spotify = MagicMock()
        spotify.isLoggedIn.return_value = True
        spotify.current_user.return_value = {"email": "alice@example.com"}
        with patch('app.SpotipyFree') as mock_sf, patch('app.saveSession') as mock_save:
            mock_sf.Spotify.return_value = spotify
            dash._verifyCookiesMatchEmail({"sp_dc": "abc"}, "alice@example.com")

            tempPath = mock_save.call_args.args[2]
            self.assertNotEqual(str(tempPath), str(dash.cookiesFile))
            self.assertEqual(mock_sf.Spotify.call_args.kwargs.get("cookiesFile"), tempPath)
        self.assertFalse(os.path.exists(tempPath))


if __name__ == '__main__':
    unittest.main()
