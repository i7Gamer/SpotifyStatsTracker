"""Background workers start with a random offset.

With several users, every periodic worker used to fire at the same instant
after a restart (version check, login re-check, backup, per-user auto-import
scans) - a thundering herd against the database, the disk, and GitHub. Each
worker now waits a random delay drawn from its own named range before its
first pass, like the metadata backfiller and wrapped worker already did.
The Spotify listener is deliberately exempt: delaying it would lose plays.
"""
import sys
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as appModule
from app import SpotifyDashboardApp
import Database.backup as backupModule
from Database.backup import BackupWorker
import Database.Importers.AutoImporter as autoImporterModule
from Database.Importers.AutoImporter import AutoImporter, Watchdog

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class _AppTestBase(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()


class TestVersionCheckOffset(_AppTestBase):
    def test_first_check_waits_out_a_random_offset(self):
        """With the stop event already set, the loop must exit during its
        startup wait - proving the wait happens BEFORE the first request."""
        dash = self._makeApp()
        dash._stop_event.set()

        with patch("app.random.randint", return_value=42) as mock_randint, \
             patch("app.requests.get") as mock_get:
            dash._versionCheckLoop()

        mock_randint.assert_called_once_with(
            appModule.VERSION_CHECK_MIN_START_DELAY_SECONDS,
            appModule.VERSION_CHECK_MAX_START_DELAY_SECONDS)
        mock_get.assert_not_called()


class TestLoginCheckLoopOffset(_AppTestBase):
    def test_periodic_recheck_waits_out_a_random_offset(self):
        """checkLogin_thread already runs _ensureAllUsersLogin synchronously
        before the thread starts, so the loop's own first pass is redundant
        at boot - it must wait out the offset first."""
        dash = self._makeApp()
        dash._stop_event.set()

        with patch("app.random.randint", return_value=42) as mock_randint, \
             patch.object(dash, "_ensureAllUsersLogin") as mock_ensure:
            dash._checkLoginLoop()

        mock_randint.assert_called_once_with(
            appModule.LOGIN_CHECK_MIN_START_DELAY_SECONDS,
            appModule.LOGIN_CHECK_MAX_START_DELAY_SECONDS)
        mock_ensure.assert_not_called()


class TestBackupWorkerOffset(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        dbPath = Path(self._tmpdir.name) / "spotify_stats.db"
        sqlite3.connect(dbPath).close()
        self.worker = BackupWorker(dbPath=dbPath)

    def test_loop_waits_out_a_random_offset_before_the_first_due_check(self):
        self.worker._stop_event.set()

        with patch("Database.backup.random.randint", return_value=42) as mock_randint, \
             patch.object(self.worker, "isDue") as mock_isDue:
            self.worker._loop()

        mock_randint.assert_called_once_with(
            backupModule.BACKUP_STARTUP_MIN_DELAY_SECONDS,
            backupModule.BACKUP_STARTUP_MAX_DELAY_SECONDS)
        mock_isDue.assert_not_called()


class TestAutoImporterOffset(unittest.TestCase):
    def test_start_passes_a_random_offset_to_the_watchdog(self):
        importer = AutoImporter("/dummy/path", MagicMock())
        importer.wd = MagicMock()

        with patch("Database.Importers.AutoImporter.random.randint", return_value=17) as mock_randint:
            importer.start()

        mock_randint.assert_called_once_with(
            autoImporterModule.AUTO_IMPORT_MIN_START_DELAY_SECONDS,
            autoImporterModule.AUTO_IMPORT_MAX_START_DELAY_SECONDS)
        importer.wd.watchFolder.assert_called_once_with(
            "/dummy/path", importer._handleImport, 5, startupDelaySeconds=17)

    def test_watchdog_startup_delay_runs_before_the_initial_scan(self):
        """With the stop event already set, a delayed watchdog must exit
        without ever touching the disk."""
        wd = Watchdog()
        wd._stop_event.set()
        callback = MagicMock()

        with patch("Database.Importers.AutoImporter.os.listdir") as mock_listdir:
            wd.watchFolder_blocking("/dummy/path", callback, startupDelaySeconds=10)

        mock_listdir.assert_not_called()
        callback.assert_not_called()

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.listdir")
    def test_default_zero_delay_keeps_the_immediate_initial_scan(self, mock_listdir, mock_exists):
        """Direct callers (and every pre-existing test) pass no delay - the
        initial scan must keep happening immediately."""
        mock_exists.return_value = True
        mock_listdir.return_value = ["found.json"]
        wd = Watchdog()
        wd.run = False   #< stop the poll loop; the initial scan still runs
        callback = MagicMock()

        with patch("Database.Importers.AutoImporter.os.path.isfile", return_value=True):
            wd.watchFolder_blocking("/dummy/path", callback, callbackInitialFiles=True)

        callback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
