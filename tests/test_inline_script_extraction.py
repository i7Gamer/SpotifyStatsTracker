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
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
STATIC_JS_DIR = REPO_ROOT / "static" / "js"

#< the markers of behaviour, as opposed to a value handed over from the server.
#  Case-SENSITIVE, unlike the tag names below: these are JavaScript identifiers,
#  where `Fetch` is simply a different name and not the thing being looked for.
LOGIC_MARKERS = re.compile(r"\bfetch\s*\(|\baddEventListener\s*\(|\bfunction\s+\w+\s*\(")


class _InlineScriptCollector(HTMLParser):
    """The text of every <script> block that has no src=, tokenized rather than
    pattern-matched.

    This started as one regex, and each spelling the spec allows but the pattern
    missed was a way past the gate: `<SCRIPT>` read as plain text, and `</script >`
    - whitespace before the bracket is legal - did worse than mis-measure its own
    block, because the search then ran on to the next end tag it did recognise and
    merged two blocks into one. When there was no next one, the block vanished from
    the gate entirely. Comments cut the other way: `<!-- <script>...</script> -->`
    was reported as live code, failing the build over something that cannot run.

    An HTML tokenizer settles all of them at once by knowing the grammar instead of
    approximating it, and it cannot drift back - there is no pattern left to leave a
    case out of. (It also takes this file out of CodeQL's py/bad-tag-filter, which
    is what surfaced the end-tag gap: alerts #19 and #20.)
    """

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._inInlineScript = False

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            #< src= means the body is elsewhere and ESLint already reads it
            self._inInlineScript = not any(name.lower() == "src" for name, _ in attrs)

    def handle_data(self, data):
        if self._inInlineScript:
            self.blocks.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            self._inInlineScript = False


def inlineScriptBlocks(markup: str) -> list[str]:
    """The body of every inline (non-src) <script> in `markup`."""
    collector = _InlineScriptCollector()
    collector.feed(markup)
    collector.close()
    return collector.blocks

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
        for block in inlineScriptBlocks(path.read_text(encoding="utf-8", errors="replace")):
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

    def test_an_uppercase_script_tag_is_still_seen(self):
        """HTML tag names are case-insensitive, so `<SCRIPT>` is the same element
        - but a gate that only knows the lowercase spelling reads it as plain
        text and waves the block through. Nothing in templates/ is written that
        way today; the point is that nobody has to know that to stay covered."""
        self.assertEqual(inlineScriptBlocks("<SCRIPT>alert(1)</SCRIPT>"), ["alert(1)"])
        self.assertEqual(inlineScriptBlocks('<Script type="module">x</Script>'), ["x"])

    def test_an_uppercase_external_script_is_still_skipped(self):
        """The src= exclusion has to travel with it, or making the gate
        case-insensitive starts reporting every external <SCRIPT SRC=> as an
        empty inline block."""
        self.assertEqual(inlineScriptBlocks('<SCRIPT SRC="/js/a.js"></SCRIPT>'), [])

    def test_a_spaced_end_tag_still_closes_the_block(self):
        """`</script >` is a valid end tag - the spec allows whitespace before
        the bracket."""
        self.assertEqual(inlineScriptBlocks("<script>alert(1)</script >"), ["alert(1)"])
        self.assertEqual(inlineScriptBlocks("<SCRIPT>alert(1)</SCRIPT\t>"), ["alert(1)"])

    def test_a_spaced_end_tag_cannot_hide_the_last_block_in_a_file(self):
        """The sharp edge behind the case above. An end tag the scan does not
        recognise does not merely mis-measure that block: the search runs on to
        the next one it does recognise, so two blocks silently become one - and
        when there is no next one, the whole block leaves the gate's sight."""
        self.assertEqual(
            inlineScriptBlocks('<script src="/js/page.js"></script>\n'
                               "<script>\n  document.addEventListener('click', f)\n</script >"),
            ["\n  document.addEventListener('click', f)\n"])

    def test_a_commented_out_block_is_not_reported_as_live(self):
        """A commented-out block runs nothing, so holding it to the rule fails
        the gate over code that cannot execute - and the obvious repair is to
        delete evidence rather than move logic."""
        self.assertEqual(inlineScriptBlocks("<!-- <script>fetch('/dead')</script> -->"), [])

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
