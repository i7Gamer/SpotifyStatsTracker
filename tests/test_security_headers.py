"""Every response must carry a baseline set of security headers - none of
these were set previously, leaving the app without even basic defense-in-
depth against clickjacking, MIME-sniffing, or (partially) the DOM-based XSS
class of bug found in charts.js's chart tooltips.
"""
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as appModule
from app import SpotifyDashboardApp, _hstsEnabled
from _app_factory import AppTestCase

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'

_STATIC_JS_DIR = Path(__file__).resolve().parent.parent / "static" / "js"

# Constructs that compile a string into code, all of which CSP gates behind
# 'unsafe-eval'. setTimeout/setInterval only need it when handed a string, so
# they are matched with an opening quote rather than bare.
_EVAL_FAMILY_PATTERNS = {
    "new Function()": re.compile(r"\bnew\s+Function\s*\("),
    "eval()": re.compile(r"(?<![.\w])eval\s*\("),
    "setTimeout(string)": re.compile(r"\bsetTimeout\s*\(\s*['\"`]"),
    "setInterval(string)": re.compile(r"\bsetInterval\s*\(\s*['\"`]"),
}


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

    def test_pages_are_not_stored_by_the_browser(self):
        """Logging out has to actually take the pages away.

        Clearing htmx's sessionStorage snapshot cache only covers the entries
        htmx owns. The browser's own back/forward cache holds fully RENDERED
        pages and replays them with no request at all - so on a shared browser,
        A logs out and B presses Back onto A's dashboard, session check and all
        skipped. no-store is what makes a history navigation re-fetch (and, in
        Chrome, what disqualifies the page from the bfcache)."""
        dash = self._makeApp()
        client = dash.app.test_client()

        resp = client.get("/login")

        self.assertIn("no-store", resp.headers.get("Cache-Control", ""))

    def test_fragments_and_api_replies_are_not_stored_either(self):
        """The htmx migration made most user data arrive as fragments and JSON
        rather than whole pages; they carry the same account's content."""
        dash = self._makeApp()
        client = dash.app.test_client()

        resp = client.get("/history", headers={"HX-Request": "true"})

        self.assertIn("no-store", resp.headers.get("Cache-Control", ""))

    def test_static_assets_stay_cacheable(self):
        """The scope limit. CSS/JS/images carry no account data, and making the
        browser re-fetch them on every navigation would be a real regression -
        this app ships htmx and a chart bundle."""
        dash = self._makeApp()
        client = dash.app.test_client()

        resp = client.get("/static/js/session-reset.js")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("no-store", resp.headers.get("Cache-Control", ""))

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
        the inline-script/style exception this app's own templates need and the
        two named Spotify embed hosts) - nothing pointing at an arbitrary third
        party, and no wildcards."""
        dash = self._makeApp()
        client = dash.app.test_client()

        resp = client.get("/login")
        csp = resp.headers.get("Content-Security-Policy", "")

        self.assertIn("connect-src 'self'", csp)
        self.assertNotIn("*", csp)

    def test_csp_allows_the_spotify_embed_frame_and_api_script(self):
        """The detail pages' Play now embed loads Spotify's iFrame API loader
        (from open.spotify.com), whose payload script comes from
        embed-cdn.spotifycdn.com, and frames the player from open.spotify.com.
        All three hosts must be allowlisted; framing *this* app by others stays
        forbidden (frame-ancestors/X-Frame-Options untouched)."""
        dash = self._makeApp()
        client = dash.app.test_client()

        resp = client.get("/login")
        csp = resp.headers.get("Content-Security-Policy", "")

        scriptSrc = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
        self.assertIn("https://open.spotify.com", scriptSrc)
        self.assertIn("https://embed-cdn.spotifycdn.com", scriptSrc)
        self.assertIn("frame-src https://open.spotify.com", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")

    def test_non_detail_pages_do_not_get_unsafe_eval(self):
        """'unsafe-eval' is needed only by Spotify's iFrame API bundle (webpack
        eval devtool) and must be confined to the detail routes - every other
        page keeps an eval-free script-src. See DETAIL_CSP_ENDPOINTS in app.py."""
        dash = self._makeApp()
        client = dash.app.test_client()

        resp = client.get("/login")
        csp = resp.headers.get("Content-Security-Policy", "")

        self.assertNotIn("unsafe-eval", csp)


class TestBrowserScriptsRespectTheCsp(unittest.TestCase):
    """The companion to test_non_detail_pages_do_not_get_unsafe_eval above.

    That test pins the *header*; nothing pinned the *scripts*, and the two
    drifted apart: profile-page.js revived swapped-in inline <script> blocks
    with `new Function(text)()`, which /profile's CSP forbids. The EvalError
    landed in a `catch (_) {}` and was swallowed, so the Account tab's
    theme-selector initialiser silently stopped working after an AJAX tab
    switch - a dead feature no header test could see.

    Scanning source is crude but it is the only check that runs without a
    browser, and the failure it guards against is silent by construction.
    """

    def _sources(self):
        files = sorted(_STATIC_JS_DIR.glob("*.js"))
        self.assertTrue(files, "no browser scripts found to scan")
        return [(path, path.read_text(encoding="utf-8")) for path in files]

    def test_no_browser_script_uses_an_eval_family_construct(self):
        offenders = [
            f"{path.name}: {label}"
            for path, source in self._sources()
            for label, pattern in _EVAL_FAMILY_PATTERNS.items()
            if pattern.search(source)
        ]

        self.assertEqual(
            offenders, [],
            "these need 'unsafe-eval', which only the detail routes grant - and "
            "they fail silently, not loudly: " + ", ".join(offenders),
        )

    def test_the_scanner_would_actually_catch_an_offender(self):
        """A regex that matches nothing is indistinguishable from a clean tree,
        so prove each pattern still fires on the construct it names."""
        samples = {
            "new Function()": "var f = new Function('return 1');",
            "eval()": "eval('1 + 1');",
            "setTimeout(string)": "setTimeout('doThing()', 10);",
            "setInterval(string)": 'setInterval("poll()", 10);',
        }

        for label, pattern in _EVAL_FAMILY_PATTERNS.items():
            with self.subTest(construct=label):
                self.assertIsNotNone(pattern.search(samples[label]))

    def test_the_scanner_does_not_flag_the_ordinary_callback_forms(self):
        """setTimeout/setInterval with a function are fine under any CSP, and
        `.eval` as a property name is not the global eval."""
        benign = (
            "setTimeout(function () { doThing(); }, 10);\n"
            "setInterval(poll, 10);\n"
            "setTimeout(() => doThing(), 10);\n"
            "thing.eval(x);\n"
        )

        for label, pattern in _EVAL_FAMILY_PATTERNS.items():
            with self.subTest(construct=label):
                self.assertIsNone(pattern.search(benign))


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
