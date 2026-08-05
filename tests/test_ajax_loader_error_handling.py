"""Every AJAX loader has to handle a failed response.

Two failure modes keep recurring, because both look like success to a naive
loader:

1. An expired session answers `401 {"error": "Not logged in", "loginUrl": ...}`.
   That body is VALID JSON, so `.json()` resolves and the payload key the loader
   wanted is simply absent - `innerHTML = data.summaryHtml` then writes the
   string "undefined" into the page. Only `redirectIfUnauthorized` can turn that
   into a trip to the login page.
2. A 5xx (or a proxy's HTML error page) answered by `resp.ok ? resp.json() : null`
   resolves to null, and the "if the payload is there, swap it in" branch below
   silently no-ops - leaving a "Loading..." placeholder up forever, or stale
   content under a URL that replaceState already changed to say otherwise.

Both are invisible to every other test: the server contract is pinned by
tests/test_ajax_unauthenticated.py, the templates render fine, and ESLint has no
opinion about a promise chain that drops a rejection. So this asserts the
CLIENT half, structurally, in the one place it can be seen at all.

It checks source shape rather than behaviour on purpose: the loaders live inside
IIFEs and are not require-able, so pinning "the guard is present" is what a
static check can honestly promise - and its absence is exactly the bug that
reached production twice.
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_JS_DIR = REPO_ROOT / "static" / "js"

# A "page loader" re-fetches the CURRENT page with ?ajax=..., i.e. it targets
# window.location.pathname, or one of the detail helpers that build such a URL.
# Those are the fetches whose response is a full page render behind @requiresUser,
# so those are the ones a 401 can reach.
PAGE_LOADER_FETCH = re.compile(
    r"fetch\(\s*[`(]?\s*(?:window\.location\.pathname|detail\w*Url\(|\$\{WRAPPED_FETCH_URL\})")

# The swallow: a non-2xx becomes null, and the caller's "did I get a payload"
# test then reads exactly like "nothing to do".
SWALLOWING_TERNARY = re.compile(r"resp(?:onse)?\.ok\s*\?\s*resp(?:onse)?\.json\(\)\s*:\s*null")

# Swallows that ARE correct, because a non-2xx has a real recovery path here:
#   dashboard-page.js - the 15s now-playing poll deliberately keeps the last
#     state on a transient error rather than yanking a background failure into
#     the user's face (its 401 is handled separately, by stopping the timer).
#   genres.js - the genre-detail load navigates to detailFallbackUrl() instead,
#     so the failure ends in a full page load rather than in silence.
# Counted per file so that a NEW swallow alongside one of these still fails.
DELIBERATE_SWALLOWS = {"dashboard-page.js": 1, "genres.js": 1}

# The files that must carry the guard, as of this test. Named rather than purely
# discovered so that a loader DISAPPEARING from the discovery (an extraction that
# accidentally drops the fetch, say) fails loudly instead of shrinking the set
# this test protects.
#
# history-page.js is deliberately absent, and it is the one entry worth
# explaining: /history moved to htmx, so it makes no fetch() call to guard. The
# invariant did not go away, it moved to the server - an htmx request from an
# expired session is answered with HX-Redirect rather than a 302 the swap would
# inline (see app.py's unauthenticatedResponse, pinned by
# tests/test_history_htmx.py's TestUnauthenticatedSwap). A page migrated to htmx
# leaves this set; a page still driving its own fetch() must not.
EXPECTED_PAGE_LOADERS = {
    "charts-page.js", "compare.js", "dashboard-page.js", "detail-chart.js",
    "detail-history.js", "detail-page.js", "genres.js",
    "top-list.js", "wrapped.js",
}

# JSON endpoints that render a dashboard card. Not page loaders (they have their
# own routes), but they sit behind @requiresUser(api=True), so the same 401 body
# reaches them - and a card that silently no-ops shows a permanent "Loading..."
# or, worse, states something untrue about the user's library.
CARD_ENDPOINT_FETCHES = (
    ("dashboard-page.js", "/api/dashboard-trends"),
    ("dashboard-page.js", "/api/dashboard-discover"),
)

#< how much of the promise chain after `fetch(` counts as "this fetch's response
#  handling" - generous enough to span a formatted .then(), tight enough not to
#  reach the next fetch in the file
HANDLER_WINDOW_LINES = 14


def _jsFiles():
    return {path.name: path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(STATIC_JS_DIR.glob("*.js"))}


def _handlerAfter(text: str, marker: str) -> str:
    """The response-handling lines that follow `marker`'s fetch call."""
    index = text.find(marker)
    if index == -1:
        return ""
    return "\n".join(text[index:].splitlines()[:HANDLER_WINDOW_LINES])


class AjaxLoaderErrorHandlingTestCase(unittest.TestCase):
    def setUp(self):
        self.files = _jsFiles()

    def test_the_discovered_page_loaders_are_the_ones_we_expect(self):
        """Negative control: if this drifts, the assertions below are silently
        protecting a different (possibly empty) set of files."""
        discovered = {name for name, text in self.files.items()
                      if PAGE_LOADER_FETCH.search(text)}

        self.assertEqual(discovered, EXPECTED_PAGE_LOADERS)

    def test_every_page_loader_handles_an_expired_session(self):
        """Either through the shared helper (the normal way) or, for a loader
        whose response handling genuinely differs, by calling the check itself.

        detail-page.js is the one exception: it has to read a 404's body to find
        the redirectUrl the route sends when an entity no longer resolves, so it
        cannot delegate to a helper that throws on every non-2xx."""
        missing = sorted(name for name in EXPECTED_PAGE_LOADERS
                         if "readJsonOrThrow" not in self.files[name]
                         and "redirectIfUnauthorized" not in self.files[name])

        self.assertEqual(missing, [], f"page AJAX loaders with no 401 handling: {missing}. "
                                      "A 401's JSON body resolves, so the payload key is undefined "
                                      "and the page renders the word 'undefined'.")

    def test_the_shared_helper_is_the_normal_way_to_read_a_payload(self):
        """Pins the convention rather than just the outcome: a new loader that
        hand-rolls the pair is how one of them lost the 401 check in the first
        place. Only the documented exception may opt out."""
        HAND_ROLLED_EXCEPTIONS = {"detail-page.js"}   #< needs a 404's body, see above

        handRolled = sorted(name for name in EXPECTED_PAGE_LOADERS - HAND_ROLLED_EXCEPTIONS
                            if "readJsonOrThrow" not in self.files[name])

        self.assertEqual(handRolled, [], "these loaders read a payload without "
                                         "AjaxStatus.readJsonOrThrow; use it, or add the file to "
                                         "HAND_ROLLED_EXCEPTIONS with the reason")

    def test_every_layout_hosting_a_loader_also_loads_ajax_status(self):
        """AjaxStatus is a hard dependency of readJsonOrThrow, not an optional
        nicety. layout_public.html hosted wrapped.js WITHOUT it, so every
        renderInto/showBanner call on the public shared-Wrapped page was silently
        skipped and a failed filter change said nothing at all."""
        templatesDir = REPO_ROOT / "templates"
        for layout in ("layout.html", "layout_public.html"):
            with self.subTest(layout=layout):
                self.assertIn("js/ajax-status.js",
                              (templatesDir / layout).read_text(encoding="utf-8"))

    def test_no_page_loader_swallows_a_non_2xx_into_null(self):
        """Counted, not merely absent, so the two deliberate swallows stay
        exempt while a NEW one in the same file still fails."""
        for name in sorted(EXPECTED_PAGE_LOADERS):
            with self.subTest(js=name):
                found = len(SWALLOWING_TERNARY.findall(self.files[name]))

                self.assertEqual(found, DELIBERATE_SWALLOWS.get(name, 0),
                                 f"{name} turns a non-2xx into null, which its caller cannot tell "
                                 "apart from an empty-but-valid answer. Throw instead, so the "
                                 "AjaxStatus banner and its Retry appear - or add it to "
                                 "DELIBERATE_SWALLOWS with the recovery path that justifies it.")

    def test_every_dashboard_card_endpoint_checks_the_status(self):
        for fileName, endpoint in CARD_ENDPOINT_FETCHES:
            with self.subTest(endpoint=endpoint):
                handler = _handlerAfter(self.files[fileName], f"fetch('{endpoint}')")

                self.assertNotEqual(handler, "", f"{endpoint} is no longer fetched from {fileName}")
                #< either through the shared helper (which checks the status and
                #  throws) or by checking it inline
                self.assertTrue("readJsonOrThrow" in handler or ".ok" in handler,
                                f"{endpoint}'s handler never checks the status, so a 500 or a 401 "
                                "resolves and the card silently keeps its placeholder")
                #< .ok alone is not enough: the swallowing ternary contains it and
                #  still ends in the same silent no-op
                self.assertNotRegex(handler, SWALLOWING_TERNARY,
                                    f"{endpoint} turns a non-2xx into null, which its caller cannot "
                                    "tell apart from an empty-but-valid answer")

    def test_the_swallow_pattern_is_detected_at_all(self):
        """Negative control for the regex: without it, the assertion above would
        pass for every possible file."""
        self.assertRegex("return resp.ok ? resp.json() : null;", SWALLOWING_TERNARY)
        self.assertRegex("return response.ok ? response.json() : null;", SWALLOWING_TERNARY)
        self.assertNotRegex("if (!resp.ok) throw new Error('x'); return resp.json();",
                            SWALLOWING_TERNARY)


if __name__ == "__main__":
    unittest.main()
