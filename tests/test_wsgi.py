"""Tests for wsgi.py's shutdown wiring.

wsgi.py is the production entry point (run under waitress) and does not go
through SpotifyDashboardApp.run() - it must independently ensure that every
user's listener/auto-importer threads are stopped when the server stops,
otherwise a SIGINT/SIGTERM to the process leaves them to be force-killed
mid-request during interpreter shutdown (see Database/patches.py and
Database/Listeners/spotifyListener.py for the underlying issue).
"""
import signal
import sys
import os
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def _importWsgiWithMocks():
    """wsgi.py builds a real SpotifyDashboardApp at import time; patch out the
    parts that would touch disk/threads/network before importing (or
    re-importing) it fresh.

    signal.signal among them, which is not cosmetic: a6e296b moved the arming
    to MODULE scope, so every import here installed wsgi's real handler in the
    pytest worker itself. See ProcessSignalsRestored for what that costs.

    startWorkers as a whole, rather than the four things it starts: patching
    its members one at a time is what let backupWorker and EMAIL_WORKER slip
    through for as long as they did, and its own docstring points a caller that
    only wants the WSGI app here. No test that uses this helper needs a worker
    running; the two that care about startWorkers do their own import and patch
    it themselves."""
    if "wsgi" in sys.modules:
        del sys.modules["wsgi"]
    with patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key'), \
         patch('app.SpotifyDashboardApp.startWorkers'), \
         patch('app.migrateIfNeeded'), \
         patch('app.Path.exists', return_value=False), \
         patch('signal.signal'):
        import wsgi
    return wsgi


class ProcessSignalsRestored(unittest.TestCase):
    """Backstop: no test in this module may outlive its own SIGTERM writes.

    Two things here write to the pytest process's real disposition. Importing
    wsgi arms _sigtermHandler (module scope, since a6e296b), and CALLING that
    handler installs SIG_IGN as its first statement. Nothing put either back,
    so once this module had run, the worker was left either ignoring SIGTERM or
    raising KeyboardInterrupt into whatever unrelated test was running when one
    arrived. Windows cannot deliver one so the dev suite never noticed; a
    cancelled GitHub Actions job sends exactly that.

    The individual tests patch signal.signal so they never write in the first
    place - this is the guard that catches the next one that forgets."""

    def setUp(self):
        super().setUp()
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(self._restoreSigterm, previous)

    @staticmethod
    def _restoreSigterm(previous):
        if previous is None:
            return  #< installed from C; there is nothing Python can put back
        try:
            signal.signal(signal.SIGTERM, previous)
        except ValueError:
            pass  #< not the main thread, so nothing was installed here either


class TestWsgiSigterm(ProcessSignalsRestored):
    """`docker stop` sends SIGTERM, and in the container this process is PID 1
    (exec-form CMD), where the kernel discards default-action signals outright.
    Python installs no SIGTERM handler of its own and neither does waitress, so
    without wsgi's handler the SIGTERM does nothing: the container sits out the
    whole stop_grace_period and gets SIGKILLed with shutdown() never run."""

    def test_the_handler_raises_keyboard_interrupt(self):
        """SIGTERM takes the exact path Ctrl+C takes: KeyboardInterrupt out of
        serve(), through main()'s finally, into dashboardApp.shutdown().

        Driven with signal.signal patched - the handler's first statement
        installs SIG_IGN, and unpatched that lands on the pytest process, not
        on wsgi's. The disarm itself is asserted by its own test below; what
        the trailing assertion pins here is that exercising the handler leaves
        this process's disposition exactly as it found it."""
        wsgi = _importWsgiWithMocks()
        before = signal.getsignal(signal.SIGTERM)

        with patch("wsgi.signal.signal"):
            with self.assertRaises(KeyboardInterrupt):
                wsgi._sigtermHandler(signal.SIGTERM, None)

        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_importing_the_module_under_test_starts_no_background_workers(self):
        """wsgi calls startWorkers() at MODULE scope, so importing it here runs
        the real thing unless it is patched out.

        Two of its four workers were never in the helper's patch list.
        backupWorker is per app instance, so every import in this file left
        another "backup-worker" thread running for the rest of the session.
        EMAIL_WORKER is worse for being a module-level SINGLETON: startWorkers
        does bind_repo(self.repo) before starting it, so the survivor polls
        every 2s holding a torn-down test database - and it drains the same
        process-global queue tests/test_email_worker.py asserts on.

        The suite's own leaked-thread guard cannot see either: conftest watches
        per-user "<prefix><user>" threads by name, deliberately, so
        process-wide workers fall outside it.

        startWorkers' docstring names this exact caller - "a caller that only
        wants the WSGI app (a test, a CLI subcommand) would otherwise have to
        patch each one out individually"."""
        before = {id(thread) for thread in threading.enumerate()}

        _importWsgiWithMocks()

        leaked = sorted(t.name for t in threading.enumerate() if id(t) not in before)
        self.assertEqual(leaked, [], f"importing wsgi left threads running: {leaked}")

    def test_importing_the_module_under_test_does_not_arm_this_process(self):
        """The helper's own contract. wsgi arms at module scope, and this
        module imports it fresh in nine places; if the helper stops patching
        signal.signal, every one of them silently starts writing to the pytest
        worker's disposition again."""
        before = signal.getsignal(signal.SIGTERM)

        _importWsgiWithMocks()

        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_it_is_armed_before_the_slow_part_of_startup(self):
        """main() was too late. startWorkers() runs at MODULE scope, and
        checkLogin_thread's first pass is synchronous: it opens every user's
        database and performs a network-bound Spotify login per user, which is
        seconds to minutes. Installing the handler in main() left that entire
        window handled by nothing - so `docker compose up -d` followed by
        `docker compose stop` discarded the SIGTERM exactly as before the fix,
        sat out the full stop_grace_period and got SIGKILLed."""
        calls = []
        if "wsgi" in sys.modules:
            del sys.modules["wsgi"]

        with patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key'), \
             patch('app.SpotifyDashboardApp.startVersionCheck_thread'), \
             patch('app.SpotifyDashboardApp.checkLogin_thread'), \
             patch('app.migrateIfNeeded'), \
             patch('app.Path.exists', return_value=False), \
             patch('signal.signal', side_effect=lambda *a: calls.append(("signal", a[0]))), \
             patch('app.SpotifyDashboardApp.startWorkers',
                   side_effect=lambda: calls.append(("startWorkers",))):
            import wsgi  # noqa: F401

        self.assertIn(("signal", signal.SIGTERM), calls)
        self.assertLess(calls.index(("signal", signal.SIGTERM)),
                        calls.index(("startWorkers",)),
                        "SIGTERM must be handled before the per-user login pass begins")

    def test_a_stop_during_startup_still_shuts_the_workers_down(self):
        """Arming it earlier moves the raise OUTSIDE main()'s try/finally, so
        module scope has to own the same guarantee: whatever startWorkers got
        as far as starting must still be stopped."""
        if "wsgi" in sys.modules:
            del sys.modules["wsgi"]

        with patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key'), \
             patch('app.SpotifyDashboardApp.startVersionCheck_thread'), \
             patch('app.SpotifyDashboardApp.checkLogin_thread'), \
             patch('app.migrateIfNeeded'), \
             patch('app.Path.exists', return_value=False), \
             patch('signal.signal'), \
             patch('app.SpotifyDashboardApp.startWorkers', side_effect=KeyboardInterrupt), \
             patch('app.SpotifyDashboardApp.shutdown') as mockShutdown:
            with self.assertRaises(KeyboardInterrupt):
                import wsgi  # noqa: F401

        mockShutdown.assert_called_once()

    def test_the_handler_disarms_itself_before_raising(self):
        """A second SIGTERM must not land inside the shutdown the first one
        started. Left armed, a repeated `docker stop` raises KeyboardInterrupt
        again inside _stopDatabasesConcurrently's thread.join(), so the
        remaining users are never joined and the traceback escapes main().

        Ignored rather than restored to SIG_DFL: this process is PID 1 in the
        container, where the kernel discards default-action signals - SIG_DFL
        would be a no-op wearing the clothes of an escape hatch. Docker's own
        SIGKILL at the end of stop_grace_period is the real one."""
        wsgi = _importWsgiWithMocks()

        with patch("wsgi.signal.signal") as mockSignal:
            with self.assertRaises(KeyboardInterrupt):
                wsgi._sigtermHandler(signal.SIGTERM, None)

        mockSignal.assert_called_once_with(signal.SIGTERM, signal.SIG_IGN)

    def test_serving_is_covered_without_main_re_arming(self):
        """The handler must be live before serve() blocks - a stop arriving in
        between is the normal case, not a race.

        It used to be main()'s job to install it, and this test asserted that.
        Module-level arming subsumes it: by the time anything can call main()
        the import has already happened, so serve() is covered and main() has
        no signal work left to do. Re-arming here would also undo the
        SIG_IGN a shutdown-in-progress installed."""
        wsgi = _importWsgiWithMocks()
        calls = []

        with patch("wsgi.signal.signal",
                   side_effect=lambda *args: calls.append(("signal", args))), \
             patch("waitress.serve",
                   side_effect=lambda *args, **kwargs: calls.append(("serve",))), \
             patch.object(wsgi.dashboardApp, "shutdown"):
            wsgi.main()

        self.assertEqual([("serve",)], calls)


class TestWsgiShutdown(ProcessSignalsRestored):
    def test_main_stops_all_listeners_after_serve_returns(self):
        wsgi = _importWsgiWithMocks()
        with patch('waitress.serve') as mockServe, \
             patch.object(wsgi.dashboardApp, 'shutdown') as mockShutdown:
            wsgi.main()

        mockServe.assert_called_once()
        mockShutdown.assert_called_once()

    def test_main_stops_all_listeners_even_if_serve_raises(self):
        wsgi = _importWsgiWithMocks()
        with patch('waitress.serve', side_effect=KeyboardInterrupt), \
             patch.object(wsgi.dashboardApp, 'shutdown') as mockShutdown:
            with self.assertRaises(KeyboardInterrupt):
                wsgi.main()

        mockShutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
