"""Tests for wsgi.py's shutdown wiring.

wsgi.py is the production entry point (run under waitress) and does not go
through SpotifyDashboardApp.run() - it must independently ensure that every
user's listener/auto-importer threads are stopped when the server stops,
otherwise a SIGINT/SIGTERM to the process leaves them to be force-killed
mid-request during interpreter shutdown (see Database/patches.py and
Database/Listeners/spotifyListener.py for the underlying issue).
"""
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
