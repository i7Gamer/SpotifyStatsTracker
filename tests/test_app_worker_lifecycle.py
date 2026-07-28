"""Background-worker startup is a lifecycle step, not a construction side-effect.

Constructing SpotifyDashboardApp used to start four background workers (backup,
email, version-check, login-check) plus - via checkLogin_thread's synchronous
first pass - one Spotify listener per user. That made `SpotifyDashboardApp()`
unusable without a patch stack, and rebound the process-global EMAIL_WORKER
singleton to whichever app was built last: under the parallel test runner a job
queued by one test could be processed against another test's temp database.

startWorkers() now owns that, so construction is inert and the workers start
exactly once, when an entry point asks for them.
"""
import os
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp

_SECRET_KEY_PATCH = "app.SpotifyDashboardApp._get_or_create_secret_key"


@contextmanager
def _appWithStubbedWorkers():
    """A real SpotifyDashboardApp with every worker-start seam mocked but NOT
    called for it - unlike _app_factory.makeApp, which patches the thread
    starters out entirely and so can't observe whether __init__ invoked them.

    Yields (dashboard, seams) where seams maps a readable name to the mock.
    The patches stay active for the whole block so startWorkers() is observable
    too."""
    with patch(_SECRET_KEY_PATCH, return_value="test-secret-key"), \
         patch("app.migrateIfNeeded"), \
         patch("app.Path.exists", return_value=False), \
         patch("app.BackupWorker.start") as backupStart, \
         patch("app.EMAIL_WORKER") as emailWorker, \
         patch("app.SpotifyDashboardApp.startVersionCheck_thread") as versionStart, \
         patch("app.SpotifyDashboardApp.checkLogin_thread") as loginStart:
        dashboard = SpotifyDashboardApp()
        yield dashboard, {
            "backup": backupStart,
            "emailBind": emailWorker.bind_repo,
            "emailStart": emailWorker.start,
            "version": versionStart,
            "login": loginStart,
        }


class TestConstructionStartsNoWorkers(unittest.TestCase):
    def test_construction_starts_no_worker(self):
        with _appWithStubbedWorkers() as (_dashboard, seams):
            for name, seam in seams.items():
                self.assertEqual(seam.call_count, 0,
                                 f"{name} was started during __init__")

    def test_construction_still_builds_the_backup_worker(self):
        """Only .start() is deferred - the worker itself is still constructed
        eagerly, because it reads its interval/retention from admin settings
        and /admin's Worker Health panel reads dashboard.backupWorker."""
        with _appWithStubbedWorkers() as (dashboard, _seams):
            self.assertIsNotNone(dashboard.backupWorker)

    def test_construction_does_not_rebind_the_email_worker_singleton(self):
        """EMAIL_WORKER is process-global; binding it at construction time made
        every app ever built in a test session fight over its repo."""
        with _appWithStubbedWorkers() as (_dashboard, seams):
            seams["emailBind"].assert_not_called()

    def test_routes_are_still_registered_by_construction(self):
        """Route registration is NOT a worker - a constructed app must be
        request-ready without startWorkers()."""
        with _appWithStubbedWorkers() as (dashboard, _seams):
            self.assertIn("/login", {r.rule for r in dashboard.app.url_map.iter_rules()})


class TestStartWorkers(unittest.TestCase):
    def test_start_workers_starts_every_worker(self):
        with _appWithStubbedWorkers() as (dashboard, seams):
            dashboard.startWorkers()

            for name, seam in seams.items():
                self.assertEqual(seam.call_count, 1, f"{name} was not started")

    def test_start_workers_binds_the_repo_before_starting_the_email_worker(self):
        """A started EmailWorker polls immediately, so an unbound repo would
        make its first jobs open throwaway connections."""
        with _appWithStubbedWorkers() as (dashboard, seams):
            dashboard.startWorkers()

            seams["emailBind"].assert_called_once_with(dashboard.repo)

    def test_start_workers_is_idempotent(self):
        """wsgi.py and run() both call it; a double call must not spawn a
        second login-check loop (neither checkLogin_thread nor
        startVersionCheck_thread guards against that on its own)."""
        with _appWithStubbedWorkers() as (dashboard, seams):
            dashboard.startWorkers()
            dashboard.startWorkers()

            for name, seam in seams.items():
                self.assertEqual(seam.call_count, 1, f"{name} was started twice")


class TestShutdownWithoutStart(unittest.TestCase):
    def test_shutdown_is_safe_when_workers_never_started(self):
        """Every test that builds an app and never starts it still ends up
        calling shutdown() from a tearDown."""
        with _appWithStubbedWorkers() as (dashboard, _seams):
            dashboard.user_databases = {}

            dashboard.shutdown()  #< must not raise

            self.assertTrue(dashboard._stop_event.is_set())

    def test_shutdown_stops_started_workers(self):
        with _appWithStubbedWorkers() as (dashboard, _seams):
            dashboard.startWorkers()
            dashboard.backupWorker = MagicMock()
            dashboard.user_databases = {}

            dashboard.shutdown()

            dashboard.backupWorker.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
