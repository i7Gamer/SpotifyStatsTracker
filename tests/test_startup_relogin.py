"""After a restart, every user who previously logged in must have their
listener started automatically using the Spotify session cookies already
stored in the database - no re-login through the web UI should be required.
"""
import json
import sys
import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as appModule
from app import SpotifyDashboardApp

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


def _healthyListenerMock():
    """A fake Listener whose contamination flag is explicitly clear - a bare
    MagicMock's auto-created contaminationDetected attribute is truthy, which
    would make Database.startListener treat it as contaminated."""
    listener = MagicMock()
    listener.contaminationDetected = False
    return listener


def _makeApp():
    with patch(_SECRET_KEY_PATCH, return_value='test-secret-key'), \
         patch('app.SpotifyDashboardApp.startVersionCheck_thread'), \
         patch('app.SpotifyDashboardApp.checkLogin_thread'), \
         patch('app.migrateIfNeeded'):
        return SpotifyDashboardApp()


class TestStartupReloginFromDatabaseCookies(unittest.TestCase):
    def test_ensure_all_users_login_starts_listener_with_db_stored_cookies(self):
        app = _makeApp()
        # Simulate a user who completed login before the "reboot" - their
        # username/email/cookies are already durably in the database, with no
        # web request involved this time around.
        app.repo.upsertUser("alice", "alice@example.com")
        app.repo.setUserCookies("alice", {"sp_dc": "abc123", "sp_key": "def456"})

        capturedCookiesPayloads = []
        listenerInstances = []

        def fakeListener(cookiesFile, email=None, **kwargs):
            # The temp cookies file is deleted right after this constructor
            # returns, so its content has to be captured now, not afterward.
            capturedCookiesPayloads.append(json.loads(Path(cookiesFile).read_text(encoding="utf-8")))
            listener = _healthyListenerMock()
            listenerInstances.append(listener)
            return listener

        with patch("Database.database.Listener", side_effect=fakeListener), \
             patch("Database.database.AutoImporter") as mockAutoImporterClass:
            mockAutoImporterClass.return_value = MagicMock()
            app._ensureAllUsersLogin()

        self.assertIn("alice", app.user_databases)
        self.assertEqual(len(capturedCookiesPayloads), 1)
        self.assertEqual(
            capturedCookiesPayloads[0],
            [{"identifier": "alice@example.com", "cookies": {"sp_dc": "abc123", "sp_key": "def456"}}],
        )
        listenerInstances[0].startListener_thread.assert_called_once()

    def test_user_with_no_cookies_yet_is_not_logged_in_automatically(self):
        """A user row with no cookies (e.g. mid-migration, never actually
        logged in) must not get a listener started for it."""
        app = _makeApp()
        app.repo.upsertUser("bob", "bob@example.com")  # no setUserCookies call

        with patch("Database.database.Listener") as mockListenerClass, \
             patch("Database.database.AutoImporter") as mockAutoImporterClass:
            mockAutoImporterClass.return_value = MagicMock()
            app._ensureAllUsersLogin()

        self.assertNotIn("bob", app.user_databases)
        mockListenerClass.assert_not_called()

    def test_multiple_returning_users_each_get_their_own_cookies(self):
        app = _makeApp()
        app.repo.upsertUser("alice", "alice@example.com")
        app.repo.setUserCookies("alice", {"sp_dc": "alice-cookie"})
        app.repo.upsertUser("bob", "bob@example.com")
        app.repo.setUserCookies("bob", {"sp_dc": "bob-cookie"})

        capturedByEmail = {}

        def fakeListener(cookiesFile, email=None, **kwargs):
            capturedByEmail[email] = json.loads(Path(cookiesFile).read_text(encoding="utf-8"))[0]["cookies"]
            return _healthyListenerMock()

        with patch("Database.database.Listener", side_effect=fakeListener), \
             patch("Database.database.AutoImporter") as mockAutoImporterClass:
            mockAutoImporterClass.return_value = MagicMock()
            app._ensureAllUsersLogin()

        self.assertEqual(capturedByEmail, {
            "alice@example.com": {"sp_dc": "alice-cookie"},
            "bob@example.com": {"sp_dc": "bob-cookie"},
        })

    def test_one_users_failure_does_not_block_the_rest(self):
        """A single user whose get_user_db() call raises (e.g. a corrupt cookie
        blob, a Listener construction error) must not stop every user after it
        in the list from getting their listener started - the whole loop used
        to be wrapped in one try/except that aborted on the first failure."""
        app = _makeApp()
        app.repo.upsertUser("alice", "alice@example.com")
        app.repo.setUserCookies("alice", {"sp_dc": "broken"})
        app.repo.upsertUser("bob", "bob@example.com")
        app.repo.setUserCookies("bob", {"sp_dc": "bob-cookie"})

        def fakeListener(cookiesFile, email=None, **kwargs):
            if email == "alice@example.com":
                raise RuntimeError("boom")
            return _healthyListenerMock()

        with patch("Database.database.Listener", side_effect=fakeListener), \
             patch("Database.database.AutoImporter") as mockAutoImporterClass:
            mockAutoImporterClass.return_value = MagicMock()
            app._ensureAllUsersLogin()  # must not raise

        self.assertNotIn("alice", app.user_databases)
        self.assertIn("bob", app.user_databases)

    def test_failed_listener_start_stops_orphaned_background_workers(self):
        """Database.__init__ starts the wrapped worker and metadata backfiller
        immediately, but get_user_db() only stores the instance in
        user_databases as its very last step. When a later step raised (seen
        in production: Listener construction failing on a Spotify 504), the
        instance was dropped unreferenced with all of its threads still
        running - and every retry (the 5-minute _checkLoginLoop, any web
        request) leaked another full set, so one user ended up with 4 wrapped
        workers recalculating their stats every couple of minutes. The
        failure path must stop the orphan's workers before propagating."""
        app = _makeApp()
        app.repo.upsertUser("leakuser", "leakuser@example.com")
        app.repo.setUserCookies("leakuser", {"sp_dc": "broken"})

        createdDatabases = []
        realDatabase = appModule.Database

        def recordingDatabase(*args, **kwargs):
            db = realDatabase(*args, **kwargs)
            createdDatabases.append(db)
            return db

        with patch("app.Database", side_effect=recordingDatabase), \
             patch("Database.database.Listener",
                   side_effect=RuntimeError("Could not GET https://open.spotify.com/. Status Code: 504")), \
             patch("Database.database.AutoImporter") as mockAutoImporterClass:
            mockAutoImporterClass.return_value = MagicMock()
            app._ensureAllUsersLogin()  # must not raise

        self.assertNotIn("leakuser", app.user_databases)
        self.assertEqual(len(createdDatabases), 1)
        liveThreadNames = {t.name for t in threading.enumerate()}
        self.assertNotIn("wrapped-worker-leakuser", liveThreadNames)
        self.assertNotIn("metadata-backfiller-leakuser", liveThreadNames)
        createdDatabases[0].autoImporter.wd.stop.assert_called()

    def test_retry_after_failed_start_does_not_stack_workers(self):
        """Once a previously-failing user finally comes up, exactly one
        wrapped worker may be running for them - not the failed attempts'
        workers plus the live one, each recalculating on its own offset
        15-minute schedule."""
        app = _makeApp()
        app.repo.upsertUser("retryuser", "retryuser@example.com")
        app.repo.setUserCookies("retryuser", {"sp_dc": "cookie"})

        attempts = []

        def flakyListener(cookiesFile, email=None, **kwargs):
            attempts.append(email)
            if len(attempts) == 1:
                raise RuntimeError("Could not GET https://open.spotify.com/. Status Code: 504")
            return _healthyListenerMock()

        with patch("Database.database.Listener", side_effect=flakyListener), \
             patch("Database.database.AutoImporter") as mockAutoImporterClass:
            mockAutoImporterClass.return_value = MagicMock()
            app._ensureAllUsersLogin()  #< first pass: listener startup fails
            app._ensureAllUsersLogin()  #< retry pass (5 minutes later in production): succeeds

        self.assertIn("retryuser", app.user_databases)
        self.addCleanup(app.user_databases["retryuser"].stop)
        workerThreads = [t for t in threading.enumerate() if t.name == "wrapped-worker-retryuser"]
        self.assertEqual(len(workerThreads), 1)

    def test_second_call_does_not_recreate_already_running_databases(self):
        """_checkLoginLoop() re-runs this every 5 minutes - a user already
        holding a live Database/listener must not be reconstructed."""
        app = _makeApp()
        app.repo.upsertUser("alice", "alice@example.com")
        app.repo.setUserCookies("alice", {"sp_dc": "abc123"})

        with patch("Database.database.Listener", return_value=_healthyListenerMock()), \
             patch("Database.database.AutoImporter") as mockAutoImporterClass:
            mockAutoImporterClass.return_value = MagicMock()
            app._ensureAllUsersLogin()
            firstDb = app.user_databases["alice"]

            app._ensureAllUsersLogin()
            secondDb = app.user_databases["alice"]

        self.assertIs(firstDb, secondDb)


if __name__ == "__main__":
    unittest.main()
