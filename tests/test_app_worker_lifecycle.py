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


class TestShutdownStopsTheSharedThreadPools(unittest.TestCase):
    """The three process-wide ThreadPoolExecutors (image download, artist bio,
    album bio) were started by Database and shut down by nobody.

    Nothing cancels them, so CPython's own concurrent.futures atexit hook is
    what eventually stops them - and it puts its sentinel BEHIND the queued
    work and then joins every worker with NO timeout. The whole backlog
    therefore runs after shutdown() has returned and reported the app stopped,
    outside the grace period tests/test_compose_shutdown_budget.py sizes, still
    issuing Last.fm and CDN requests. Under Docker that window ends in SIGKILL
    mid-teardown."""

    def _shutdownWithMockedPools(self):
        """The mocks go in AFTER construction: SpotifyDashboardApp.__init__
        calls Database.configureWorkerPools, which rebuilds all three from the
        admin settings and would discard anything installed earlier."""
        from Database.database import Database
        mocks = (MagicMock(), MagicMock(), MagicMock())
        for mock in mocks:
            # A real int: shutdownWorkerPools sizes each replacement from the
            # pool it retires, and ThreadPoolExecutor(max_workers=<MagicMock>)
            # raises ValueError - which app.py's guard would swallow, leaving
            # the remaining pools untouched and this test quietly half-blind.
            mock._max_workers = 4
        with _appWithStubbedWorkers() as (dashboard, _seams):
            originals = (Database._imageDownloadExecutor,
                         Database._artistBioFetchExecutor,
                         Database._albumBioFetchExecutor)
            (Database._imageDownloadExecutor,
             Database._artistBioFetchExecutor,
             Database._albumBioFetchExecutor) = mocks
            try:
                dashboard.user_databases = {}
                dashboard.shutdown()
            finally:
                (Database._imageDownloadExecutor,
                 Database._artistBioFetchExecutor,
                 Database._albumBioFetchExecutor) = originals
        return mocks

    def test_every_shared_pool_is_shut_down(self):
        for pool in self._shutdownWithMockedPools():
            with self.subTest(pool=pool):
                pool.shutdown.assert_called_once()

    def test_the_pools_are_not_waited_on_and_the_backlog_is_dropped(self):
        """wait=False because shutdown() is already inside a bounded budget,
        and cancel_futures=True because the queued work is best-effort media
        the next page view re-triggers - waiting on it is what the atexit hook
        already does badly."""
        for pool in self._shutdownWithMockedPools():
            with self.subTest(pool=pool):
                self.assertEqual(pool.shutdown.call_args.kwargs,
                                 {"wait": False, "cancel_futures": True})

    def test_the_pools_are_usable_again_afterwards(self):
        """One process outlives many app instances - AppTestCase registers
        shutdown() as a cleanup for every route test - and a stopped
        ThreadPoolExecutor raises on every later submit(). Leaving them shut
        would fail whichever unrelated test next rendered a page that lazily
        fetches an image or a bio."""
        from Database.database import Database
        with _appWithStubbedWorkers() as (dashboard, _seams):
            dashboard.user_databases = {}
            dashboard.shutdown()

            for pool in (Database._imageDownloadExecutor,
                         Database._artistBioFetchExecutor,
                         Database._albumBioFetchExecutor):
                with self.subTest(pool=pool):
                    self.assertEqual(pool.submit(lambda: "alive").result(timeout=5), "alive")

    def test_the_replacement_pools_keep_the_configured_size(self):
        """configureWorkerPools sizes them from admin settings at startup; a
        replacement that silently reverted to the code default would quietly
        undo that for the rest of the process."""
        from Database.database import Database
        with _appWithStubbedWorkers() as (dashboard, _seams):
            sizesBefore = [p._max_workers for p in (Database._imageDownloadExecutor,
                                                    Database._artistBioFetchExecutor,
                                                    Database._albumBioFetchExecutor)]
            dashboard.user_databases = {}
            dashboard.shutdown()

            sizesAfter = [p._max_workers for p in (Database._imageDownloadExecutor,
                                                   Database._artistBioFetchExecutor,
                                                   Database._albumBioFetchExecutor)]

        self.assertEqual(sizesAfter, sizesBefore)

    def test_the_pools_are_retired_after_the_threads_that_feed_them_are_stopped(self):
        """Order is the whole point. Every per-user thread that submits media
        work is alive until _stopDatabasesConcurrently returns (bounded by
        USER_STOP_JOIN_TIMEOUT_SECONDS = 30s), and shutdownWorkerPools installs
        a live REPLACEMENT pool. Retiring the pools first therefore hands that
        whole 30s window a fresh pool nothing will ever stop again - the
        listener's appendTrackData -> saveImagesFromTrack -> submit path queues
        a CDN download onto it, and the only thing left to stop that is the
        interpreter's atexit hook, i.e. exactly what this fix removes."""
        from Database.database import Database
        order = []
        mocks = (MagicMock(), MagicMock(), MagicMock())
        for index, mock in enumerate(mocks):
            mock._max_workers = 4
            mock.shutdown.side_effect = lambda *a, i=index, **k: order.append(f"pool{i}")
        with _appWithStubbedWorkers() as (dashboard, _seams):
            originals = (Database._imageDownloadExecutor,
                         Database._artistBioFetchExecutor,
                         Database._albumBioFetchExecutor)
            (Database._imageDownloadExecutor,
             Database._artistBioFetchExecutor,
             Database._albumBioFetchExecutor) = mocks
            try:
                db = MagicMock()
                db.stop.side_effect = lambda *a, **k: order.append("userStop")
                dashboard.user_databases = {"timo": db}

                dashboard.shutdown()
            finally:
                (Database._imageDownloadExecutor,
                 Database._artistBioFetchExecutor,
                 Database._albumBioFetchExecutor) = originals

        self.assertIn("userStop", order)
        self.assertLess(order.index("userStop"), order.index("pool0"),
                        "the pools must be retired only once nothing can still submit to them")

    def test_a_failing_pool_does_not_abort_the_rest_of_shutdown(self):
        """Same rule the backup/email workers already follow: one member
        raising must not leave the per-user databases unsignalled."""
        from Database.database import Database
        with _appWithStubbedWorkers() as (dashboard, _seams):
            originals = (Database._imageDownloadExecutor, Database._artistBioFetchExecutor)
            failing = MagicMock()
            failing.shutdown.side_effect = RuntimeError("boom")
            survivor = MagicMock()
            survivor._max_workers = 4
            Database._imageDownloadExecutor = failing
            Database._artistBioFetchExecutor = survivor
            try:
                db = MagicMock()
                dashboard.user_databases = {"timo": db}

                dashboard.shutdown()  #< must not raise

                survivor.shutdown.assert_called_once()   #< the REST of the pools
                db.signalStop.assert_called_once()       #< and the rest of shutdown
            finally:
                (Database._imageDownloadExecutor, Database._artistBioFetchExecutor) = originals


if __name__ == "__main__":
    unittest.main()
