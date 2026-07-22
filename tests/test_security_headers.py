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

import app as appModule
from app import SpotifyDashboardApp, _hstsEnabled
from _app_factory import AppTestCase

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class TestSecurityHeaders(AppTestCase):
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


class TestHstsToggleParsing(unittest.TestCase):
    """_hstsEnabled reads ENABLE_HSTS with the same truthy/junk tolerance as
    the other env toggles (mirrors TRUST_PROXY_HEADERS parsing)."""

    def _withEnv(self, value):
        env = {} if value is None else {appModule.ENABLE_HSTS_ENV_VAR: value}
        with patch.dict(os.environ, env, clear=False):
            if value is None:
                os.environ.pop(appModule.ENABLE_HSTS_ENV_VAR, None)
            return _hstsEnabled()

    def test_unset_is_disabled(self):
        self.assertFalse(self._withEnv(None))

    def test_empty_is_disabled(self):
        self.assertFalse(self._withEnv(""))

    def test_truthy_values_enable(self):
        for value in ("1", "true", "yes", "on", "TRUE", "  on  "):
            self.assertTrue(self._withEnv(value), value)

    def test_junk_and_zero_are_disabled(self):
        self.assertFalse(self._withEnv("banana"))
        self.assertFalse(self._withEnv("0"))


class TestHstsHeader(AppTestCase):
    def test_hsts_absent_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(appModule.ENABLE_HSTS_ENV_VAR, None)
            dash = self._makeApp()
            client = dash.app.test_client()
            resp = client.get("/login")
        self.assertNotIn("Strict-Transport-Security", resp.headers)

    def test_hsts_present_and_valued_when_enabled(self):
        with patch.dict(os.environ, {appModule.ENABLE_HSTS_ENV_VAR: "1"}):
            dash = self._makeApp()
            client = dash.app.test_client()
            resp = client.get("/login")
            header = resp.headers.get("Strict-Transport-Security")
        self.assertEqual(header, appModule.HSTS_HEADER_VALUE)
        self.assertIn("max-age=", header)
        self.assertIn("includeSubDomains", header)

    def test_hsts_also_set_on_error_responses_when_enabled(self):
        """after_request fires for 404s too - HSTS should ride along there
        just like the baseline headers do."""
        with patch.dict(os.environ, {appModule.ENABLE_HSTS_ENV_VAR: "1"}):
            dash = self._makeApp()
            client = dash.app.test_client()
            resp = client.get("/this-route-does-not-exist")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.headers.get("Strict-Transport-Security"), appModule.HSTS_HEADER_VALUE)


if __name__ == "__main__":
    unittest.main()
