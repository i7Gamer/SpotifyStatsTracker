"""Template <script> blocks hold DATA, not logic.

Commit 3be61d7 moved the template scripts out to static/js so the ESLint gate
could read them - the gate exists because `resp` referenced inside a callback
whose parameter is named `response` threw a ReferenceError on every load and
shipped in a release. Logic left inline is invisible to it: ESLint only reads
.js files, and the Python tests assert on rendered markup, where a broken script
looks exactly like a working one.

import.html was missed by that sweep, and it was the worst page to miss - its 46
inline lines held the import-progress poller and the confirm() standing in front
of "deletes ALL recorded plays in the years these files cover".

So this pins the invariant rather than the one file: an inline block may compute
nothing, and the two remaining exceptions are named with what they are.
"""
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
STATIC_JS_DIR = REPO_ROOT / "static" / "js"

INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL)
#< the markers of behaviour, as opposed to a value handed over from the server
LOGIC_MARKERS = re.compile(r"\bfetch\s*\(|\baddEventListener\s*\(|\bfunction\s+\w+\s*\(")

# Inline blocks that legitimately still hold a statement, with the reason:
#   layout.html  - the theme bootstrap has to run before first paint, or the page
#                  flashes the wrong theme; a separate file is another round trip.
#   admin.html   - onSkipModeChange is called from an inline on*= attribute and
#                  toggles two fields; small enough that extracting it would move
#                  the wiring further from the markup it belongs to.
#   profile.html - the theme selector's change handler, same bootstrap concern.
ALLOWED_INLINE_LOGIC = {"layout.html", "admin.html", "profile.html"}


def _inlineLogic():
    """{template name: [offending snippets]} for every inline block with logic."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        for block in INLINE_SCRIPT.findall(path.read_text(encoding="utf-8", errors="replace")):
            found = LOGIC_MARKERS.findall(block)
            if found:
                offenders.setdefault(path.name, []).extend(found)
    return offenders


class InlineScriptExtractionTestCase(unittest.TestCase):
    def test_no_new_template_computes_anything_inline(self):
        offenders = {name: hits for name, hits in _inlineLogic().items()
                     if name not in ALLOWED_INLINE_LOGIC}

        self.assertEqual(offenders, {},
                         "logic in a template <script> is invisible to the ESLint gate - move it to "
                         "static/js and leave a data island, or add it to ALLOWED_INLINE_LOGIC "
                         f"with the reason. Offenders: {sorted(offenders)}")

    def test_the_marker_regex_actually_matches_logic(self):
        """Negative control: without this, the assertion above passes for any
        possible template."""
        self.assertRegex("await fetch('/api/x')", LOGIC_MARKERS)
        self.assertRegex("form.addEventListener('submit', f)", LOGIC_MARKERS)
        self.assertRegex("function fetchProgress() {", LOGIC_MARKERS)
        self.assertNotRegex('var HISTORY_DEFAULT_WINDOW = "day";', LOGIC_MARKERS)


class ImportPageScriptTestCase(unittest.TestCase):
    """The extracted file has to keep the two things that page's script does."""

    def setUp(self):
        self.template = (TEMPLATES_DIR / "import.html").read_text(encoding="utf-8")
        self.script = (STATIC_JS_DIR / "import-page.js").read_text(encoding="utf-8")

    def test_the_template_loads_the_extracted_script(self):
        self.assertIn("js/import-page.js", self.template)

    def test_the_data_island_supplies_both_values_the_script_reads(self):
        for name in ("IMPORT_PROGRESS_URL", "IMPORT_IS_RUNNING"):
            with self.subTest(value=name):
                self.assertIn(name, self.template)
                self.assertIn(name, self.script)

    def test_a_destructive_overwrite_is_still_confirmed_first(self):
        """The dialog is the only thing between a mis-click and deleting every
        play in the covered years, live-tracked ones included."""
        self.assertIn("confirm(", self.script)
        self.assertIn("This deletes ALL recorded plays", self.script)
        self.assertIn("event.preventDefault()", self.script)

    def test_the_confirmation_is_gated_on_the_overwrite_checkbox(self):
        self.assertIn("overwrite-range", self.script)

    def test_the_progress_poller_survived(self):
        self.assertIn("IMPORT_PROGRESS_URL", self.script)
        self.assertIn("setTimeout(fetchProgress", self.script)


class ImportPageRendersTestCase(AppTestCase):
    """GET /import was rendered by no test at all, so a data island whose Jinja
    does not resolve - a wrong endpoint name in url_for, a missing key on
    importProgress - would have raised on the real page and passed the suite."""

    def _get(self, status="idle"):
        db = MagicMock()
        db.readProgress.return_value = {"status": status, "percentage": 40, "message": "Working"}
        dash = self._makeApp()
        client = dash.app.test_client()
        with patch.object(dash, "is_user_logged_in", return_value=True), \
             patch.object(dash, "get_username_for_email", return_value="alice"), \
             patch.object(dash, "get_user_db", return_value=db):
            with client.session_transaction() as sess:
                sess["email"] = "alice@example.com"
            return client.get("/import")

    def test_the_page_renders_and_loads_the_extracted_script(self):
        resp = self._get()

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"js/import-page.js", resp.data)

    def test_the_progress_url_resolves_to_the_real_endpoint(self):
        resp = self._get()

        self.assertIn(b'window.IMPORT_PROGRESS_URL = "/import-progress"', resp.data)

    def test_a_running_import_tells_the_script_to_start_polling(self):
        self.assertIn(b"window.IMPORT_IS_RUNNING = true", self._get(status="running").data)

    def test_an_idle_import_does_not(self):
        self.assertIn(b"window.IMPORT_IS_RUNNING = false", self._get(status="idle").data)


if __name__ == "__main__":
    unittest.main()
