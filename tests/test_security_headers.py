"""Every response must carry a baseline set of security headers - none of
these were set previously, leaving the app without even basic defense-in-
depth against clickjacking, MIME-sniffing, or (partially) the DOM-based XSS
class of bug found in charts.js's chart tooltips.
"""
import unittest
from unittest.mock import patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class TestSecurityHeaders(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def test_headers_present_on_an_unauthenticated_page(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        resp = client.get("/login")

        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(resp.headers.get("Referrer-Policy"), "same-origin")
        self.assertIn("Content-Security-Policy", resp.headers)

    def test_headers_present_on_a_404(self):
        """after_request must fire for ordinary HTTP error responses too, not
        just 200s - a bug page is just as much a place clickjacking/MIME-
        sniffing protection matters."""
        dash = self._makeApp()
        client = dash.app.test_client()

        resp = client.get("/this-route-does-not-exist")

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("Content-Security-Policy", resp.headers)

    def test_csp_allows_google_fonts_but_restricts_object_and_framing(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        resp = client.get("/login")
        csp = resp.headers.get("Content-Security-Policy", "")

        self.assertIn("fonts.googleapis.com", csp)
        self.assertIn("fonts.gstatic.com", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("default-src 'self'", csp)

    def test_csp_does_not_allow_arbitrary_external_connect_or_script_hosts(self):
        """default-src/connect-src/script-src must only allowlist 'self' (plus
        the inline-script/style exception this app's own templates need) -
        nothing pointing at an arbitrary third party."""
        dash = self._makeApp()
        client = dash.app.test_client()

        resp = client.get("/login")
        csp = resp.headers.get("Content-Security-Policy", "")

        self.assertIn("connect-src 'self'", csp)
        self.assertNotIn("*", csp)


if __name__ == "__main__":
    unittest.main()
