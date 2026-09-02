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
    r"fetch\(\s*[`(]?\s*(?:window\.location\.pathname|detail\w*Url\()")

# The swallow: a non-2xx becomes null, and the caller's "did I get a payload"
# test then reads exactly like "nothing to do".
SWALLOWING_TERNARY = re.compile(r"resp(?:onse)?\.ok\s*\?\s*resp(?:onse)?\.json\(\)\s*:\s*null")

# Comments, stripped before the scan below. Not a nicety: this gate spent its
# life counting PROSE. genres.js carried the line "readJsonOrThrow, not
# `resp.ok ? resp.json() : null`: ..." explaining why it did NOT swallow, and
# that comment is what put it in DELIBERATE_SWALLOWS - the entry documented a
# recovery path for a swallow that did not exist. ajax-status.js's own
# docstring says the same words and would newly trip the widened scan below.
#
# Naive enough to also eat the "//" in a URL, which is harmless here: the only
# thing searched for afterwards is the ternary above.
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _code(text):
    """`text` with comments removed, so a rule about code is about code."""
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


# Swallows that ARE correct, because a non-2xx has a real recovery path here.
# Counted per file so that a NEW swallow alongside an exempt one still fails.
#
# There is none left. dashboard-page.js's 15s now-playing poll was the entry:
# it kept the last state on a transient error rather than yanking a background
# failure into the user's face, which is still the right call - but "keep the
# last state" and "say nothing about it" turned out to be separable. The poll
# now throws on a non-2xx like everything else and COUNTS the failures, marking
# the panel stale after three in a row (see pollIsStale); the screen keeps its
# content, and stops claiming that content is current.
DELIBERATE_SWALLOWS = {}

# The files that must carry the guard, as of this test. Named rather than purely
# discovered so that a loader DISAPPEARING from the discovery (an extraction that
# accidentally drops the fetch, say) fails loudly instead of shrinking the set
# this test protects.
#
# It is down to ONE, and that is the point rather than a shrinking guard: every
# other page moved to htmx and stopped calling fetch() for its own body. The
# invariant did not go away, it moved to the server - an htmx request from an
# expired session is answered with HX-Redirect rather than a 302 the swap would
# inline (see app.py's unauthenticatedResponse, pinned by each page's
# test_*_htmx.py TestUnauthenticatedSwap). A page migrated to htmx leaves this
# set; a page still driving its own fetch() must not.
#
# detail-chart.js is the one that stays, and it stays for a reason worth keeping
# written down: the Trend-buckets select re-fetches a time SERIES, which is
# numbers rather than markup. htmx swaps response bodies, so there is nothing
# for it to do here - see routes/charts.py's `?ajax=true` branch.
EXPECTED_PAGE_LOADERS = {"detail-chart.js"}

# Deliberately empty. Both dashboard card endpoints (/api/dashboard-trends,
# /api/dashboard-discover) return HTML fragments now and are swapped by htmx, so
# there is no client-side payload read left to guard. The failure behaviour they
# had is still deliberate and still differs per card - Discover blanks, because
# its "locked"/"empty" messages are claims about the user's library that a 500 is
# no evidence for; trends shows the shared inline error and a Retry - but that is
# behaviour, pinned in tests/test_dashboard_htmx.py, not source shape.
#
# Kept as an empty tuple with this note rather than deleted, so that a future
# card added as a JSON fetch has an obvious place to be registered.
CARD_ENDPOINT_FETCHES = ()

#< how much of the promise chain after `fetch(` counts as "this fetch's response
#  handling" - generous enough to span a formatted .then(), tight enough not to
#  reach the next fetch in the file
HANDLER_WINDOW_LINES = 14

# The OTHER family of fetches a 401 reaches, and the one PAGE_LOADER_FETCH above
# was never looking at: a call to a `/api/` endpoint. Those routes are
# @requiresUser(api=True), so an expired session answers 401 with a JSON body -
# the same parses-fine trap, one endpoint family over. It is how
# static/js/playlists.js's preview came to print "0 tracks match selection" at a
# logged-out user (`data.track_count || 0` on the 401 body), and how its rename
# and delete came to alert() the route's "Not logged in" as if it were a
# rejected edit.
#
# Matched on the URL LITERAL rather than on `fetch(`, because the preview builds
# its URL in a variable first - which is exactly why a `fetch('/api/` pattern
# would have kept missing the one site that was actually broken.
API_URL_LITERAL = re.compile(r"""['"`]/api/""")

# What counts as handling it: the shared helper (which redirects), the redirect
# itself, or an inline status test. All three appear in the codebase today and
# all three are correct - what must not appear is none of them.
UNAUTHORIZED_GUARD = re.compile(r"readJsonOrThrow|redirectIfUnauthorized|status\s*[!=]==\s*401")

# htmx issues its own requests and an expired session is answered with
# HX-Redirect rather than a body (see app.py's unauthenticatedResponse), so
# there is no client-side payload read to guard - dashboard-page.js's
# `htmx.ajax('GET', '/api/dashboard-trends', ...)` is not this rule's business.
# Whole-line, so a URL wrapped onto the NEXT line is still counted: this rule
# should fail loudly rather than exempt something quietly.
_HTMX_AJAX_LINE = re.compile(r"^.*htmx\.ajax\(.*$", re.MULTILINE)


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

        detail-page.js used to be the exception (it read a 404's body for the
        redirectUrl the route sends when an entity no longer resolves); it moved
        to htmx and that answer became a header. detail-chart.js has the same
        need for its ?ajax=true mode and meets it by peeling the 404 off AHEAD
        of the helper, so the 401 still goes through readJsonOrThrow."""
        missing = sorted(name for name in EXPECTED_PAGE_LOADERS
                         if "readJsonOrThrow" not in self.files[name]
                         and "redirectIfUnauthorized" not in self.files[name])

        self.assertEqual(missing, [], f"page AJAX loaders with no 401 handling: {missing}. "
                                      "A 401's JSON body resolves, so the payload key is undefined "
                                      "and the page renders the word 'undefined'.")

    def test_the_shared_helper_is_the_normal_way_to_read_a_payload(self):
        """Pins the convention rather than just the outcome: a new loader that
        hand-rolls the pair is how one of them lost the 401 check in the first
        place. Only a documented exception may opt out.

        There is none left. detail-page.js was the entry, and it was there
        because it had to read a 404's BODY to find where an unresolvable entity
        should send the visitor - which no helper that throws on every non-2xx
        can do. The detail pages moved to htmx and that answer became a header
        (HX-Redirect, see _missingEntityResponse), so the branch, the fetch and
        the exception all went at once.

        The same need is back in detail-chart.js - _missingEntityResponse still
        answers its ?ajax=true mode with 404 {redirectUrl}, and for a while
        nothing read it - but it is NOT an exception: the loader peels the 404
        off first and hands every other status to readJsonOrThrow, so the
        convention this pins still holds (behaviour in tests/test_detail_chart.js)."""
        HAND_ROLLED_EXCEPTIONS = set()

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

    def test_no_script_swallows_a_non_2xx_into_null(self):
        """Counted, not merely absent, so an exempt swallow stays exempt while a
        NEW one in the same file still fails.

        Scans EVERY script, not just EXPECTED_PAGE_LOADERS, and that widening is
        load-bearing: the last deliberate swallow lived in dashboard-page.js's
        now-playing poll, and the dashboard left that set when it moved to htmx.
        Scoped to the loaders, this rule would have quietly stopped covering the
        only file it still had anything to say about - and the poll is exactly
        the code most likely to grow a second, accidental swallow, since it is
        the last background fetch in the app."""
        for name in sorted(self.files):
            with self.subTest(js=name):
                found = len(SWALLOWING_TERNARY.findall(_code(self.files[name])))

                self.assertEqual(found, DELIBERATE_SWALLOWS.get(name, 0),
                                 f"{name} turns a non-2xx into null, which its caller cannot tell "
                                 "apart from an empty-but-valid answer. Throw instead, so the "
                                 "AjaxStatus banner and its Retry appear - or add it to "
                                 "DELIBERATE_SWALLOWS with the recovery path that justifies it.")

    def test_every_api_fetch_handles_an_expired_session(self):
        """Counted, not merely present, so a SECOND api call added to a file
        that already guards its first still fails.

        That counting is the whole point here. playlists.js held three `/api/`
        calls and zero guards, and no rule in this file had anything to say
        about it: PAGE_LOADER_FETCH only ever looked at fetches of the current
        page. "The file mentions redirectIfUnauthorized somewhere" would have
        passed the day one of the three was fixed and left the other two.

        Scope worth naming rather than implying: this sees `/api/` URLs written
        as literals. An endpoint reached through a variable set elsewhere
        (import-page.js's window.IMPORT_PROGRESS_URL) is outside it - that one
        checks its own 401, and a template-supplied URL cannot be read from
        here at all."""
        for name in sorted(self.files):
            code = _HTMX_AJAX_LINE.sub("", _code(self.files[name]))
            apiCalls = len(API_URL_LITERAL.findall(code))
            if not apiCalls:
                continue
            with self.subTest(js=name):
                guards = len(UNAUTHORIZED_GUARD.findall(code))

                self.assertGreaterEqual(
                    guards, apiCalls,
                    f"{name} makes {apiCalls} /api/ call(s) but carries only {guards} 401 "
                    "guard(s). A 401's JSON body PARSES, so the payload key is simply absent "
                    "and the page states something false about the user's data instead of "
                    "sending them to log in. Use AjaxStatus.redirectIfUnauthorized.")

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

    def test_a_swallow_named_only_in_prose_is_not_counted(self):
        """The bug this gate had for its whole life. genres.js explained in a
        comment why it did NOT swallow - quoting the pattern to say "not this" -
        and the scan counted the quote, which is how it earned a
        DELIBERATE_SWALLOWS entry documenting a recovery path for code that was
        never there. ajax-status.js's docstring says the same words today."""
        prose = "// non-2xx handed to `resp.ok ? resp.json() : null` reads as nothing to do\n"
        block = "/* readJsonOrThrow, not resp.ok ? resp.json() : null: it throws */\n"

        self.assertEqual(SWALLOWING_TERNARY.findall(_code(prose)), [])
        self.assertEqual(SWALLOWING_TERNARY.findall(_code(block)), [])
        #< and the real thing still counts once the comments around it are gone
        self.assertEqual(len(SWALLOWING_TERNARY.findall(
            _code("// why:\nreturn resp.ok ? resp.json() : null;\n"))), 1)


if __name__ == "__main__":
    unittest.main()
