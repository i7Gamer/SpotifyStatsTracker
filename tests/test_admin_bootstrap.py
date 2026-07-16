"""Admin bootstrap at app startup.

ADMIN_EMAIL (when set) is authoritative: that user becomes the ONLY admin -
the explicit-configuration path, and the recovery path if the automatic
promotion picked the wrong account. Without it, the earliest-created user is
promoted once if no admin exists yet, so a fresh install converges on the
instance owner (migration 1.17.0 does the same for upgraded databases).
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as appModule
from app import SpotifyDashboardApp
from Database.repository import Repository

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class TestAdminBootstrap(unittest.TestCase):
    def _seedUsers(self):
        """Users written to the (per-test isolated) default database the app
        is about to open."""
        repo = Repository()
        repo.upsertUser("newer", "newer@example.com", createdAt=200.0)
        repo.upsertUser("older", "older@example.com", createdAt=100.0)
        repo.connectionManager.close()

    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def test_earliest_user_is_promoted_when_no_admin_exists(self):
        self._seedUsers()

        dash = self._makeApp()

        self.assertEqual(dash.repo.getAdminUsernames(), ["older"])

    def test_existing_admin_is_left_alone_without_admin_email(self):
        self._seedUsers()
        repo = Repository()
        repo.setUserAdmin("newer", True)
        repo.connectionManager.close()

        dash = self._makeApp()

        self.assertEqual(dash.repo.getAdminUsernames(), ["newer"])

    def test_admin_email_makes_that_user_the_only_admin(self):
        """The recovery path: the automatic promotion picked 'older', but the
        instance owner is 'newer' - setting ADMIN_EMAIL must both promote
        newer and demote older."""
        self._seedUsers()
        repo = Repository()
        repo.setUserAdmin("older", True)
        repo.connectionManager.close()

        with patch.dict(os.environ, {appModule.ADMIN_EMAIL_ENV_VAR: "newer@example.com"}):
            dash = self._makeApp()

        self.assertEqual(dash.repo.getAdminUsernames(), ["newer"])

    def test_admin_email_matching_is_case_insensitive(self):
        self._seedUsers()

        with patch.dict(os.environ, {appModule.ADMIN_EMAIL_ENV_VAR: "Newer@Example.COM"}):
            dash = self._makeApp()

        self.assertIn("newer", dash.repo.getAdminUsernames())

    def test_unknown_admin_email_changes_nothing(self):
        """A typo'd ADMIN_EMAIL must not demote the current admin - losing all
        admins to a typo would be worse than keeping a stale one."""
        self._seedUsers()
        repo = Repository()
        repo.setUserAdmin("older", True)
        repo.connectionManager.close()

        with patch.dict(os.environ, {appModule.ADMIN_EMAIL_ENV_VAR: "nobody@example.com"}):
            dash = self._makeApp()

        self.assertEqual(dash.repo.getAdminUsernames(), ["older"])

    def test_empty_database_bootstraps_nothing(self):
        dash = self._makeApp()
        self.assertEqual(dash.repo.getAdminUsernames(), [])


if __name__ == "__main__":
    unittest.main()
