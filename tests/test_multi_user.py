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
    def test_get_username_for_email_timorzipa(self, mock_migrate, mock_check, mock_version, mock_secret):
        app = SpotifyDashboardApp()
        username = app.get_username_for_email("timorzipa@gmail.com")
        self.assertIsNone(username)

    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    def test_get_username_for_email_from_map(self, mock_migrate, mock_check, mock_version, mock_secret):
        app = SpotifyDashboardApp()
        app.repo.upsertUser("test_user", "test@example.com")

        username = app.get_username_for_email("test@example.com")
        self.assertEqual(username, "test_user")

    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    def test_get_or_create_user(self, mock_migrate, mock_check, mock_version, mock_secret):
        app = SpotifyDashboardApp()

        username = app.get_or_create_user("john.doe@test.com")
        self.assertEqual(username, "johndoe")

    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    def test_get_or_create_user_adopts_orphaned_username_with_no_email(self, mock_migrate, mock_check, mock_version, mock_secret):
        """A username that already exists with no email on record (e.g. a
        migration whose users_map.json didn't know this user's email) must be
        claimed by the first login that sanitizes to it, not shadowed by a new
        sibling account that leaves its existing history stranded."""
        app = SpotifyDashboardApp()
        app.repo.upsertUser("timorzipa", None)

        username = app.get_or_create_user("timorzipa@gmail.com")

        self.assertEqual(username, "timorzipa")
        self.assertEqual(app.repo.getUsernameForEmail("timorzipa@gmail.com"), "timorzipa")

    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    def test_get_or_create_user_still_suffixes_on_a_real_email_collision(self, mock_migrate, mock_check, mock_version, mock_secret):
        """A username that already belongs to a DIFFERENT, known email must not
        be claimed - only a truly orphaned (no-email) username is fair game."""
        app = SpotifyDashboardApp()
        app.repo.upsertUser("alice", "alice@other.com")

        username = app.get_or_create_user("alice@example.com")

        self.assertEqual(username, "alice_1")
        self.assertEqual(app.repo.getUsernameForEmail("alice@other.com"), "alice")
        self.assertEqual(app.repo.getUsernameForEmail("alice@example.com"), "alice_1")

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

    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    def test_shutdown_stops_all_user_databases(self, mock_migrate, mock_check, mock_version, mock_secret):
        app = SpotifyDashboardApp()
        db1 = MagicMock()
        db2 = MagicMock()
        app.user_databases = {"user1": db1, "user2": db2}

        app.shutdown()

        db1.stop.assert_called_once()
        db2.stop.assert_called_once()

    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    def test_shutdown_continues_after_one_database_fails_to_stop(self, mock_migrate, mock_check, mock_version, mock_secret):
        """One user's listener failing to stop cleanly must not block the rest
        from being stopped during app shutdown."""
        app = SpotifyDashboardApp()
        db1 = MagicMock()
        db1.user = "user1"
        db1.stop.side_effect = Exception("boom")
        db2 = MagicMock()
        app.user_databases = {"user1": db1, "user2": db2}

        app.shutdown()  # should not raise

        db2.stop.assert_called_once()


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

    @patch('routes.media.send_from_directory')
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
    """_session_lock now only protects get_or_create_user()'s check-then-create
    username sequence (the one place that still has a real race: two concurrent
    first-time logins picking the same candidate username). Reads (is_user_logged_in,
    get_username_for_email) go straight to the database and don't need it - a slow
    network call in one must not block unrelated lookups or a concurrent
    get_or_create_user for someone else."""

    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    def _makeApp(self, mock_migrate, mock_check, mock_version):
        import tempfile
        from Database.repository import Repository
        app = SpotifyDashboardApp.__new__(SpotifyDashboardApp)
        app.baseDir = Path(tempfile.mkdtemp())
        app.repo = Repository(app.baseDir / "test.db")
        app.user_databases = {}
        app._db_lock = threading.RLock()
        app._session_lock = threading.RLock()
        app._login_cache = {}
        return app

    def test_slow_listener_check_does_not_block_unrelated_session_lookups(self):
        import time

        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")
        dash.repo.setUserCookies("alice", {"sp_dc": "fake"})

        slowDb = MagicMock()
        slowDb.isListenerLoggedIn.side_effect = lambda: time.sleep(0.3) or True
        dash.user_databases["alice"] = slowDb

        thread = threading.Thread(target=lambda: dash.is_user_logged_in("alice@example.com"))
        thread.start()
        time.sleep(0.05)  # let the slow call start

        start = time.time()
        dash.get_username_for_email("bob@example.com")  # unrelated user, no network involved
        elapsed = time.time() - start

        thread.join()

        self.assertLess(
            elapsed, 0.2,
            "an unrelated session lookup blocked on another user's live listener check"
        )

    def test_two_new_users_do_not_race_on_the_same_candidate_username(self):
        """Two different brand-new emails that sanitize to the same username
        prefix must not both win the uniqueness check and collide - the whole
        check-then-create sequence in get_or_create_user is what _session_lock
        protects now."""
        dash = self._makeApp()

        results = {}

        def create(email):
            results[email] = dash.get_or_create_user(email)

        emails = [f"alice+{i}@example.com" for i in range(5)]  # all sanitize to "alice" + suffix
        threads = [threading.Thread(target=create, args=(email,)) for email in emails]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        usernames = list(results.values())
        self.assertEqual(len(usernames), len(set(usernames)), "two emails were assigned the same username")
        for email, username in results.items():
            self.assertEqual(dash.repo.getUsernameForEmail(email), username)


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
             patch.object(dash, 'get_or_create_user', return_value='alice'), \
             patch.object(dash, 'get_user_db'), \
             patch.object(dash.repo, 'setUserCookies') as mock_set_cookies:
            resp, client = self._postLogin(dash)

        self.assertEqual(resp.status_code, 302)
        mock_set_cookies.assert_called_once()
        self.assertEqual(mock_set_cookies.call_args.args[0], 'alice')
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

    def test_verify_cleans_up_its_temp_cookies_file(self):
        dash = self._makeApp()
        spotify = MagicMock()
        spotify.isLoggedIn.return_value = True
        spotify.current_user.return_value = {"email": "alice@example.com"}
        with patch('app.SpotipyFree') as mock_sf, patch('app.saveSession') as mock_save:
            mock_sf.Spotify.return_value = spotify
            dash._verifyCookiesMatchEmail({"sp_dc": "abc"}, "alice@example.com")

            tempPath = mock_save.call_args.args[2]
            self.assertEqual(mock_sf.Spotify.call_args.kwargs.get("cookiesFile"), tempPath)
        self.assertFalse(os.path.exists(tempPath))


if __name__ == '__main__':
    unittest.main()
