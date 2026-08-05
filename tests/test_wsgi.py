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
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def _importWsgiWithMocks():
    """wsgi.py builds a real SpotifyDashboardApp at import time; patch out the
    parts that would touch disk/threads/network before importing (or
    re-importing) it fresh."""
    if "wsgi" in sys.modules:
        del sys.modules["wsgi"]
    with patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key'), \
         patch('app.SpotifyDashboardApp.startVersionCheck_thread'), \
         patch('app.SpotifyDashboardApp.checkLogin_thread'), \
         patch('app.migrateIfNeeded'), \
         patch('app.Path.exists', return_value=False):
        import wsgi
    return wsgi


class TestWsgiSigterm(unittest.TestCase):
    """`docker stop` sends SIGTERM, and in the container this process is PID 1
    (exec-form CMD), where the kernel discards default-action signals outright.
    Python installs no SIGTERM handler of its own and neither does waitress, so
    without wsgi's handler the SIGTERM does nothing: the container sits out the
    whole stop_grace_period and gets SIGKILLed with shutdown() never run."""

    def test_the_handler_raises_keyboard_interrupt(self):
        """SIGTERM takes the exact path Ctrl+C takes: KeyboardInterrupt out of
        serve(), through main()'s finally, into dashboardApp.shutdown()."""
        wsgi = _importWsgiWithMocks()

        with self.assertRaises(KeyboardInterrupt):
            wsgi._sigtermHandler(signal.SIGTERM, None)

    def test_main_installs_it_before_serving(self):
        """The handler must be live before serve() blocks - a stop arriving
        in between is the normal case, not a race. Asserted via mocks: the
        real signal.signal is only legal on the main thread, which a parallel
        test worker is not."""
        wsgi = _importWsgiWithMocks()
        calls = []

        with patch("wsgi.signal.signal",
                   side_effect=lambda *args: calls.append(("signal", args))), \
             patch("waitress.serve",
                   side_effect=lambda *args, **kwargs: calls.append(("serve",))), \
             patch.object(wsgi.dashboardApp, "shutdown"):
            wsgi.main()

        self.assertEqual(("signal", (signal.SIGTERM, wsgi._sigtermHandler)), calls[0])
        self.assertEqual(("serve",), calls[-1])


class TestWsgiShutdown(unittest.TestCase):
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
