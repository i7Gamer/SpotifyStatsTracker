"""The custom-date-range control set has exactly one implementation.

Six pages render the same four controls - #interval, #startDate, #endDate and
#dateError - and each needs the same three behaviours: decide whether the range
is worth a request, paint the error state, and enable/disable the date inputs so
a stale range is not serialized. static/js/htmx-filters.js owns all three
(rangeProblemFromDom, showRangeError, syncCustomRange) precisely so the six
cannot disagree.

The extraction commit converted five of them and missed the dashboard, which
kept private copies that were identical line for line. Nothing caught it: the
copies behaved the same, so no behavioural test could fail, ESLint has no
opinion about duplication, and the Python suite never executes either file.

So this pins the invariant structurally, which is what a static check can
honestly promise here: outside htmx-filters.js, no page module touches the date
inputs' `disabled` or `borderColor`, and none spells the error message itself.
Those three are the tells of a private copy - they are what the shared functions
exist to do, and a page doing them itself is a page that has stopped sharing.

The failure this prevents is a silent divergence: one page querying on a
half-typed date while another does not, or two spellings of "start after end"
drifting apart the way /charts and /genres' single-day rule already did once.
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_JS_DIR = REPO_ROOT / "static" / "js"
TEMPLATES_DIR = REPO_ROOT / "templates"

#< the one module allowed to implement the control set
SHARED_MODULE = "htmx-filters.js"
#< stored byte-for-byte as upstream published it (see .gitattributes)
VENDOR_DIR = "vendor"

# Comments are stripped before the scan. Not a nicety - the sibling gate in
# tests/test_ajax_loader_error_handling.py spent its life counting PROSE, and
# every module here carries a comment explaining what the shared helper does
# with `disabled` and why. Those must not read as an implementation.
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

# The three tells, each naming a thing showRangeError/syncCustomRange do.
# Anchored on the date inputs by name so unrelated borderColor work elsewhere
# (playlists.js tints tag chips) is not swept up.
_DATE_INPUT = r"(?:'|\")(?:start|end)Date(?:'|\")\s*\)"
TELLS = {
    "toggles the date inputs' `disabled` (syncCustomRange's job)":
        re.compile(_DATE_INPUT + r"\s*\.\s*disabled\s*="),
    "paints the date inputs' border (showRangeError's job)":
        re.compile(_DATE_INPUT + r"\s*\.\s*style\s*\.\s*borderColor"),
    "spells the inverted-range message itself (RANGE_INVERTED_MESSAGE's job)":
        re.compile(r"RANGE_INVERTED_MESSAGE\s*=|Start date cannot be after end date"),
}


def _codeOf(path: Path) -> str:
    """The file with its comments removed, so prose about the shared helper is
    not mistaken for a reimplementation of it."""
    src = path.read_text(encoding="utf-8")
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", src))


def pageModules():
    """Every browser script that is not the shared module or vendored."""
    return sorted(
        p for p in STATIC_JS_DIR.rglob("*.js")
        if p.name != SHARED_MODULE and VENDOR_DIR not in p.parts
    )


# The four ids the shared functions reach for by name. They are one control set:
# rangeProblemFromDom reads the first three, showRangeError paints the last three,
# syncCustomRange enables/disables two of them. None of the three is meaningful
# without all four present.
CONTROL_SET_IDS = ('id="interval"', 'id="startDate"', 'id="endDate"', 'id="dateError"')


def _templatesRenderingAnyControl():
    """{template: ids present} for every template that renders part of the set."""
    rendering = {}
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        markup = path.read_text(encoding="utf-8")
        present = tuple(i for i in CONTROL_SET_IDS if i in markup)
        if present:
            rendering[path.name] = present
    return rendering


def _dateErrorSpanIn(markup: str) -> str:
    """The #dateError element's opening tag through its close, or ''."""
    start = markup.find('id="dateError"')
    if start == -1:
        return ''
    start = markup.rfind('<', 0, start)
    return markup[start:markup.find('</span>', start) + len('</span>')]


def _dateErrorSpan(templateName: str) -> str:
    return _dateErrorSpanIn((TEMPLATES_DIR / templateName).read_text(encoding='utf-8'))

class TestTheControlSetIsRenderedWhole(unittest.TestCase):
    """What licenses the shared functions to reach for these by name without
    null-guarding each one.

    showRangeError used to guard #dateError and not #startDate/#endDate beside
    it, which reads as "the error span is optional" - it is not, and the guard
    only ever hid the fact that a page rendering three of the four would fail
    silently instead of loudly. The invariant belongs here, where a template
    breaking it is caught, rather than as a runtime branch that turns a broken
    page into a quietly inert one.
    """

    def test_a_template_rendering_any_of_the_set_renders_all_of_it(self):
        partial = {name: sorted(set(CONTROL_SET_IDS) - set(present))
                   for name, present in _templatesRenderingAnyControl().items()
                   if len(present) != len(CONTROL_SET_IDS)}
        self.assertEqual(
            partial, {},
            "These templates render part of the custom-date control set. The shared "
            "helpers in static/js/htmx-filters.js address all four by id and assume "
            "they arrive together.\n  " + "\n  ".join(f"{k} missing {v}" for k, v in partial.items()))

    def test_the_scan_sees_the_pages_that_carry_the_set(self):
        """Guards the gate: an id renamed in every template at once would empty
        the map, and an empty map satisfies the assertion above."""
        rendering = _templatesRenderingAnyControl()
        for template in ("history.html", "charts.html", "genres.html",
                         "compare.html", "tracks.html", "_page_card.html"):
            with self.subTest(template=template):
                self.assertIn(template, rendering)

    def test_a_partial_control_set_would_be_reported(self):
        present = tuple(i for i in CONTROL_SET_IDS if i != 'id="dateError"')
        self.assertNotEqual(len(present), len(CONTROL_SET_IDS))


class TestCustomDateControlsHaveOneImplementation(unittest.TestCase):
    def test_no_page_module_reimplements_the_control_set(self):
        offenders = []
        for module in pageModules():
            code = _codeOf(module)
            for description, pattern in TELLS.items():
                if pattern.search(code):
                    offenders.append(f"{module.name} {description}")
        self.assertEqual(
            offenders, [],
            "These modules carry their own copy of the custom-date control set. "
            "Call HtmxFilters.syncCustomRange(containerId) / showRangeError(problem) / "
            "rangeProblemFromDom() instead - see static/js/htmx-filters.js.\n  "
            + "\n  ".join(offenders))

    def test_the_shared_module_really_does_implement_all_three(self):
        """Guards the gate itself: if htmx-filters.js stopped owning these, the
        scan above would pass by finding nothing anywhere, which reads like
        success and is the opposite."""
        code = _codeOf(STATIC_JS_DIR / SHARED_MODULE)
        for description, pattern in TELLS.items():
            with self.subTest(tell=description):
                self.assertRegex(
                    code, pattern,
                    f"{SHARED_MODULE} no longer {description} - the scan above is "
                    "now asserting that nobody does it at all.")

    def test_the_scan_would_notice_a_private_copy(self):
        """A page module that reimplements the set must trip every tell - the
        gate is worthless if it only ever matches the text it was written
        against."""
        privateCopy = (
            "var invalid = problem === 'inverted';\n"
            "byId('startDate').disabled = !custom;\n"
            "byId('endDate').style.borderColor = invalid ? 'var(--accent)' : '';\n"
            "var MESSAGE = 'Start date cannot be after end date.';\n"
        )
        for description, pattern in TELLS.items():
            with self.subTest(tell=description):
                self.assertRegex(privateCopy, pattern)


class TestPageCardDateErrorIsAnnouncedAndTied(unittest.TestCase):
    """FOLLOW-UP (2026-09-02 review): templates/history.html's #dateError got
    role="alert" + aria-describedby, and history-page.js got aria-invalid, in
    c40dbc6 (UT-4). The three Top pages (/top-songs, /top-artists,
    /top-albums) share _page_card.html and htmx-filters.js's showRangeError/
    syncCustomRange for the exact same control set, so this pins that they got
    the same treatment - folded into the SHARED code (see
    TestShowRangeErrorSetsAriaInvalid below), not copied a third time."""

    def setUp(self):
        self.markup = (TEMPLATES_DIR / "_page_card.html").read_text(encoding="utf-8")

    def test_the_error_span_is_an_alert(self):
        self.assertIn('id="dateError" class="date-error" role="alert"', self.markup)

    def test_both_date_inputs_point_at_it(self):
        self.assertEqual(self.markup.count('aria-describedby="dateError"'), 2)


class TestEveryDateErrorIsAnnouncedAndTied(unittest.TestCase):
    """The same treatment, swept across every template that renders the
    control set rather than pinned per page. history.html got it in c40dbc6
    (UT-4) and _page_card.html in the follow-up above; tracks.html,
    charts.html, genres.html and compare.html were still bare spans, so on
    four pages an inverted custom range was announced to nobody and the
    message was tied to neither input (2026-09-03 review, C-4 / RR2-1).

    Swept, because showRangeError paints whichever #dateError the page
    rendered - the announcement is a property of the control set, not of the
    page that happens to carry it."""

    def test_every_error_span_is_an_alert(self):
        missing = sorted(
            name for name in _templatesRenderingAnyControl()
            if 'role="alert"' not in _dateErrorSpan(name))
        self.assertEqual(
            missing, [],
            'These templates render #dateError without role="alert", so'
            ' showRangeError writes a message no screen reader announces.')

    def test_both_date_inputs_point_at_it_on_every_page(self):
        wrong = {name: (TEMPLATES_DIR / name).read_text(encoding='utf-8')
                 .count('aria-describedby="dateError"')
                 for name in _templatesRenderingAnyControl()}
        self.assertEqual(
            {k: v for k, v in wrong.items() if v != 2}, {},
            'Both #startDate and #endDate must name #dateError, or the message is'
            ' tied to neither input.')

    def test_the_sweep_would_notice_a_bare_span(self):
        """Guards the gate: the assertions above pass trivially if the
        extractor stops finding the span at all."""
        self.assertEqual(_dateErrorSpanIn('<span id="dateError" class="date-error"></span>'),
                         '<span id="dateError" class="date-error"></span>')
        self.assertNotIn('role="alert"',
                         _dateErrorSpanIn('<span id="dateError" class="date-error"></span>'))

class TestShowRangeErrorSetsAriaInvalid(unittest.TestCase):
    """The JS half of the same follow-up, pinned structurally rather than by
    running the DOM (tests/test_htmx_filters.js does that): showRangeError
    itself must call the shared syncDateAriaInvalid, since that is what makes
    every page routing through it - not just /history - benefit."""

    def test_show_range_error_calls_the_shared_sync(self):
        code = _codeOf(STATIC_JS_DIR / SHARED_MODULE)
        showRangeError = re.search(r"function showRangeError\([^)]*\)\s*\{(.*?)\n\}", code, re.DOTALL)
        self.assertIsNotNone(showRangeError, "showRangeError not found in " + SHARED_MODULE)
        self.assertIn("syncDateAriaInvalid(", showRangeError.group(1))


if __name__ == "__main__":
    unittest.main()
