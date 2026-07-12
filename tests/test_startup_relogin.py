"""After a restart, every user who previously logged in must have their
listener started automatically using the Spotify session cookies already
stored in the database - no re-login through the web UI should be required.
"""
import json
import sys
import os
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


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

        def fakeListener(cookiesFile, email=None):
            # The temp cookies file is deleted right after this constructor
            # returns, so its content has to be captured now, not afterward.
            capturedCookiesPayloads.append(json.loads(Path(cookiesFile).read_text(encoding="utf-8")))
            listener = MagicMock()
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

        def fakeListener(cookiesFile, email=None):
            capturedByEmail[email] = json.loads(Path(cookiesFile).read_text(encoding="utf-8"))[0]["cookies"]
            return MagicMock()

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

        def fakeListener(cookiesFile, email=None):
            if email == "alice@example.com":
                raise RuntimeError("boom")
            return MagicMock()

        with patch("Database.database.Listener", side_effect=fakeListener), \
             patch("Database.database.AutoImporter") as mockAutoImporterClass:
            mockAutoImporterClass.return_value = MagicMock()
            app._ensureAllUsersLogin()  # must not raise

        self.assertNotIn("alice", app.user_databases)
        self.assertIn("bob", app.user_databases)

    def test_second_call_does_not_recreate_already_running_databases(self):
        """_checkLoginLoop() re-runs this every 5 minutes - a user already
        holding a live Database/listener must not be reconstructed."""
        app = _makeApp()
        app.repo.upsertUser("alice", "alice@example.com")
        app.repo.setUserCookies("alice", {"sp_dc": "abc123"})

        with patch("Database.database.Listener", return_value=MagicMock()), \
             patch("Database.database.AutoImporter") as mockAutoImporterClass:
            mockAutoImporterClass.return_value = MagicMock()
            app._ensureAllUsersLogin()
            firstDb = app.user_databases["alice"]

            app._ensureAllUsersLogin()
            secondDb = app.user_databases["alice"]

        self.assertIs(firstDb, secondDb)


if __name__ == "__main__":
    unittest.main()
