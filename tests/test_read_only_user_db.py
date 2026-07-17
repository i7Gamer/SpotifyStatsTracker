"""_getReadOnlyUserDb() and get_user_db()'s activation-guard: a public
share-link view must never trigger a live Spotify listener/auto-importer for
an anonymous GET, but the owner's next real login must still activate that
same cached instance instead of silently skipping activation forever."""
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class ReadOnlyUserDbTestCase(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        app = SpotifyDashboardApp()
        app.repo.upsertUser("alice", "alice@example.com")
        return app


class TestGetReadOnlyUserDb(ReadOnlyUserDbTestCase):
    def test_cold_user_gets_a_db_without_activation(self):
        app = self._makeApp()

        with patch('app.Database', side_effect=lambda *a, **k: MagicMock()):
            db = app._getReadOnlyUserDb("alice")

        self.assertIsNotNone(db)
        db.startAutoImporter.assert_not_called()
        db.resetProgress.assert_not_called()
        db.startListener.assert_not_called()
        self.assertIs(app.user_databases["alice"], db)
        self.assertNotIn("alice", app._activatedUsers)

    def test_second_call_reuses_the_same_instance(self):
        app = self._makeApp()

        with patch('app.Database', side_effect=lambda *a, **k: MagicMock()) as mock_database:
            db1 = app._getReadOnlyUserDb("alice")
            db2 = app._getReadOnlyUserDb("alice")

        self.assertIs(db1, db2)
        mock_database.assert_called_once()

    def test_reuses_an_already_active_instance_instead_of_building_a_readonly_one(self):
        app = self._makeApp()

        with patch('app.Database', side_effect=lambda *a, **k: MagicMock()) as mock_database:
            activeDb = app.get_user_db("alice", "alice@example.com")
            readOnlyDb = app._getReadOnlyUserDb("alice")

        self.assertIs(activeDb, readOnlyDb)
        mock_database.assert_called_once()
        activeDb.startListener.assert_called_once()


class TestActivationGuardOnRealLogin(ReadOnlyUserDbTestCase):
    def test_real_login_activates_a_previously_read_only_instance_in_place(self):
        app = self._makeApp()

        with patch('app.Database', side_effect=lambda *a, **k: MagicMock()) as mock_database:
            readOnlyDb = app._getReadOnlyUserDb("alice")
            readOnlyDb.startListener.assert_not_called()

            activatedDb = app.get_user_db("alice", "alice@example.com")

        self.assertIs(activatedDb, readOnlyDb)   #< same object, not a fresh one
        mock_database.assert_called_once()   #< never reconstructed
        activatedDb.startAutoImporter.assert_called_once()
        activatedDb.resetProgress.assert_called_once()
        activatedDb.startListener.assert_called_once_with(email="alice@example.com")
        self.assertIn("alice", app._activatedUsers)

    def test_activation_only_happens_once_across_repeated_real_logins(self):
        app = self._makeApp()

        with patch('app.Database', side_effect=lambda *a, **k: MagicMock()):
            app._getReadOnlyUserDb("alice")
            db1 = app.get_user_db("alice", "alice@example.com")
            db2 = app.get_user_db("alice", "alice@example.com")

        self.assertIs(db1, db2)
        db1.startListener.assert_called_once()

    def test_normal_login_with_no_prior_read_only_view_still_activates_once(self):
        """Regression guard for the common path (no share link ever viewed) -
        the activation-guard rework must not change existing behavior here."""
        app = self._makeApp()

        with patch('app.Database', side_effect=lambda *a, **k: MagicMock()) as mock_database:
            db1 = app.get_user_db("alice", "alice@example.com")
            db2 = app.get_user_db("alice", "alice@example.com")

        self.assertIs(db1, db2)
        mock_database.assert_called_once()
        db1.startListener.assert_called_once()


class TestActivationFailureHandling(ReadOnlyUserDbTestCase):
    def test_fresh_construction_failure_is_not_left_reachable(self):
        def _makeBrokenDb(*a, **k):
            db = MagicMock()
            db.startListener.side_effect = RuntimeError("boom")
            return db

        app = self._makeApp()

        with patch('app.Database', side_effect=_makeBrokenDb):
            with self.assertRaises(RuntimeError):
                app.get_user_db("alice", "alice@example.com")

        self.assertNotIn("alice", app.user_databases)
        self.assertNotIn("alice", app._activatedUsers)

    def test_upgrade_activation_failure_removes_the_dead_instance_from_both_caches(self):
        app = self._makeApp()

        with patch('app.Database', side_effect=lambda *a, **k: MagicMock()):
            readOnlyDb = app._getReadOnlyUserDb("alice")
            readOnlyDb.startListener.side_effect = RuntimeError("bad cookies")

            with self.assertRaises(RuntimeError):
                app.get_user_db("alice", "alice@example.com")

        readOnlyDb.stop.assert_called_once()
        self.assertNotIn("alice", app.user_databases)
        self.assertNotIn("alice", app._activatedUsers)

    def test_a_later_call_after_upgrade_failure_reconstructs_fresh(self):
        app = self._makeApp()

        with patch('app.Database', side_effect=lambda *a, **k: MagicMock()) as mock_database:
            readOnlyDb = app._getReadOnlyUserDb("alice")
            readOnlyDb.startListener.side_effect = RuntimeError("bad cookies")
            with self.assertRaises(RuntimeError):
                app.get_user_db("alice", "alice@example.com")

            newDb = app.get_user_db("alice", "alice@example.com")

        self.assertIsNot(newDb, readOnlyDb)
        self.assertEqual(mock_database.call_count, 2)
        newDb.startListener.assert_called_once()


if __name__ == "__main__":
    unittest.main()
