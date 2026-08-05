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
from _app_factory import AppTestCase

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


def _healthyListenerMock():
    """A fake Listener whose contamination/login-failure flags are explicitly
    clear - a bare MagicMock's auto-created attributes are truthy, which would
    make Database.startListener treat it as contaminated or login-failed."""
    listener = MagicMock()
    listener.contaminationDetected = False
    listener.loginFailed = False
    return listener



class TestStartupReloginFromDatabaseCookies(AppTestCase):
    """AppTestCase, not a bare TestCase: _ensureAllUsersLogin() activates real
    users, and every activation starts that user's periodic workers -
    self._makeApp() is the one that shuts them down again at teardown."""

    def test_ensure_all_users_login_starts_listener_with_db_stored_cookies(self):
        app = self._makeApp()
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
        app = self._makeApp()
        app.repo.upsertUser("bob", "bob@example.com")  # no setUserCookies call

        with patch("Database.database.Listener") as mockListenerClass, \
             patch("Database.database.AutoImporter") as mockAutoImporterClass:
            mockAutoImporterClass.return_value = MagicMock()
            app._ensureAllUsersLogin()

        self.assertNotIn("bob", app.user_databases)
        mockListenerClass.assert_not_called()

    def test_multiple_returning_users_each_get_their_own_cookies(self):
        app = self._makeApp()
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
        app = self._makeApp()
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
        app = self._makeApp()
        app.repo.upsertUser("leakuser", "leakuser@example.com")
        app.repo.setUserCookies("leakuser", {"sp_dc": "broken"})

        createdDatabases = []
        realDatabase = appModule.Database

        def recordingDatabase(*args, **kwargs):
            db = realDatabase(*args, **kwargs)
            createdDatabases.append(db)
            return db

        with patch("dashboard.user_registry.Database", side_effect=recordingDatabase), \
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
        app = self._makeApp()
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

    def test_a_login_failed_listener_is_not_restarted_every_pass(self):
        """Bad or mismatched cookies never recover by retrying, and this loop
        runs every 5 minutes forever - so a restart here would re-attempt the
        same failing Spotify login ~288 times a day per affected user, which is
        how an instance argues itself into bot-detection. Only a fresh
        re-login (_refresh_user_session, which rebuilds the listener) may retry.

        Driven through a stand-in Database because the guard reads nothing but
        the listener flags and the reported health."""
        app = self._makeApp()
        app.repo.upsertUser("expired", "expired@example.com")
        app.repo.setUserCookies("expired", {"sp_dc": "stale"})
        db = MagicMock()
        db.listener.loginFailed = True
        db.listener.contaminationDetected = False
        db.getListenerHealth.return_value = {"status": "DEAD"}

        with patch.object(app, "get_user_db", return_value=db):
            app._ensureAllUsersLogin()

        db.startListener.assert_not_called()

    def test_a_contaminated_listener_is_not_restarted_either(self):
        """The other half of the same guard: cookies that authenticate as a
        different Spotify account are just as permanent as ones that don't
        authenticate at all."""
        app = self._makeApp()
        app.repo.upsertUser("mixed", "mixed@example.com")
        app.repo.setUserCookies("mixed", {"sp_dc": "someone-elses"})
        db = MagicMock()
        db.listener.loginFailed = False
        db.listener.contaminationDetected = True
        db.getListenerHealth.return_value = {"status": "DEAD"}

        with patch.object(app, "get_user_db", return_value=db):
            app._ensureAllUsersLogin()

        db.startListener.assert_not_called()

    def test_a_dead_listener_with_good_credentials_is_still_restarted(self):
        """The guard has to be specifically about credentials. Without this,
        widening it to "DEAD means leave it alone" would look correct against
        the two tests above while silently ending recovery from the ordinary
        crash-and-restart case the loop exists for."""
        app = self._makeApp()
        app.repo.upsertUser("crashed", "crashed@example.com")
        app.repo.setUserCookies("crashed", {"sp_dc": "good"})
        db = MagicMock()
        db.listener.loginFailed = False
        db.listener.contaminationDetected = False
        db.getListenerHealth.return_value = {"status": "DEAD"}

        with patch.object(app, "get_user_db", return_value=db):
            app._ensureAllUsersLogin()

        db.startListener.assert_called_once_with(email="crashed@example.com")

    def test_milestone_detection_still_runs_for_a_login_failed_user(self):
        """Milestones are derived entirely from stored play history - nothing in
        detectMilestones touches Spotify. A user whose cookies expired still has
        every play they ever recorded, and an import-only account never had
        cookies to begin with, so folding detection under the credential guard
        would strand exactly the users with the most history and no way to
        notice. Detection sits deliberately outside that guard; this pins it."""
        app = self._makeApp()
        app.repo.upsertUser("expired", "expired@example.com")
        app.repo.setUserCookies("expired", {"sp_dc": "stale"})
        db = MagicMock()
        db.listener.loginFailed = True
        db.listener.contaminationDetected = False
        db.getListenerHealth.return_value = {"status": "DEAD"}

        with patch.object(app, "get_user_db", return_value=db), \
             patch.object(app, "_detectMilestonesSafely") as mockDetect:
            app._ensureAllUsersLogin()

        mockDetect.assert_called_once_with(db, "expired")

    def test_second_call_does_not_recreate_already_running_databases(self):
        """_checkLoginLoop() re-runs this every 5 minutes - a user already
        holding a live Database/listener must not be reconstructed."""
        app = self._makeApp()
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


class TestTheStartupPassStopsWhenShutdownBegins(AppTestCase):
    """The per-user loop had no stop check of its own.

    startAutoImporter grew one (`Database/workers/listener.py`, "a signalled
    instance never starts a thread again") because a worker begun after the
    shutdown snapshot was taken is a thread nothing will join. This loop is the
    other half of that: it is what CALLS startListener, and on a pass already
    running when shutdown begins it walked every remaining user first - each one
    a get_user_db, possibly a fresh listener, and a milestone pass that runs
    queries. With three users that is small; it is also unbounded in the only
    direction that matters, since the shutdown budget is fixed and the user
    count is not.

    Restarting a listener DURING a shutdown is the specific waste: it is started
    only to be torn down, and if it starts after the snapshot, not torn down at
    all.
    """

    def _twoUsersWithCookies(self, app):
        for name in ("alice", "bob"):
            app.repo.upsertUser(name, f"{name}@example.com")
            app.repo.setUserCookies(name, {"sp_dc": f"{name}-cookie"})

    def test_a_stop_signalled_mid_pass_ends_the_pass(self):
        app = self._makeApp()
        self._twoUsersWithCookies(app)
        seen = []

        realGetUserDb = app.get_user_db

        def stopAfterFirst(username, email, *args, **kwargs):
            seen.append(username)
            #< the shutdown lands while the first user is being processed
            app._stop_event.set()
            return realGetUserDb(username, email, *args, **kwargs)

        with patch("Database.database.Listener", side_effect=lambda *a, **k: _healthyListenerMock()),              patch("Database.database.AutoImporter") as mockAutoImporterClass,              patch.object(app, "get_user_db", side_effect=stopAfterFirst):
            mockAutoImporterClass.return_value = MagicMock()
            app._ensureAllUsersLogin()

        self.assertEqual(len(seen), 1,
                         "the pass kept starting listeners after the stop was signalled")

    def test_an_unsignalled_pass_still_visits_everyone(self):
        """The guard must not become an early return on the normal path - this
        loop is what brings every user's listener up at boot."""
        app = self._makeApp()
        self._twoUsersWithCookies(app)
        seen = []

        realGetUserDb = app.get_user_db

        def record(username, email, *args, **kwargs):
            seen.append(username)
            return realGetUserDb(username, email, *args, **kwargs)

        with patch("Database.database.Listener", side_effect=lambda *a, **k: _healthyListenerMock()),              patch("Database.database.AutoImporter") as mockAutoImporterClass,              patch.object(app, "get_user_db", side_effect=record):
            mockAutoImporterClass.return_value = MagicMock()
            app._ensureAllUsersLogin()

        self.assertEqual(sorted(seen), ["alice", "bob"])


if __name__ == "__main__":
    unittest.main()
