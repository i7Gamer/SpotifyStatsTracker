"""Static guards for the two background polls: when they run, and what the page
says when they stop working.

Neither poll used to stop. Browsers throttle background timers, they do not
stop them, so a tab left open kept asking - the dashboard's now-playing poll
every 15s (a handful of queries for the viewer, plus a fan-out over everyone
they share with) and the topbar's listener pill every 10s, on every page. The
lifecycle now lives in static/js/visibility-poll.js and its behaviour is tested
in node (tests/test_visibility_poll.js); what is pinned here is what node cannot
see - that both pollers actually go through it, that the template loads it
first, and that a poll which has stopped answering says so instead of leaving a
track from twenty minutes ago on screen looking current.
"""
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_JS_DIR = os.path.join(_ROOT, "static", "js")
_CSS_PATH = os.path.join(_ROOT, "static", "css", "style.css")
_LAYOUT_PATH = os.path.join(_ROOT, "templates", "layout.html")

_HELPER = "visibility-poll.js"
#< the two files that poll; both read window.VisibilityPoll at load
_POLLERS = ("dashboard-page.js", "layout-chrome.js")


def _readFile(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TestPollsStopWhenNobodyIsLooking(unittest.TestCase):
    def setUp(self):
        self.layout = _readFile(_LAYOUT_PATH)
        self.scripts = {name: _readFile(os.path.join(_JS_DIR, name)) for name in _POLLERS}

    def test_the_helper_is_loaded_before_the_scripts_that_poll(self):
        """layout-chrome.js reads it at load, not on first tick.

        Matched on the script tag rather than the bare filename: the comments
        around these tags name the files they explain."""
        def tagFor(name):
            return "filename='js/%s'" % name

        self.assertLess(self.layout.index(tagFor(_HELPER)),
                        self.layout.index(tagFor("layout-chrome.js")))

    def test_it_loads_in_the_head(self):
        """The page-level scripts that read it live inside {% block content %},
        which is inside <main> - so anything loaded at the end of the body is
        loaded too late for them."""
        head = self.layout[:self.layout.index("</head>")]

        self.assertIn(_HELPER, head)

    def test_both_polls_go_through_it(self):
        for name, script in self.scripts.items():
            with self.subTest(script=name):
                self.assertIn("VisibilityPoll.start(", script)

    def test_no_script_arms_a_timer_of_its_own(self):
        """A raw setInterval is a poll that runs in a background tab forever -
        which is the whole defect. Vendored libraries are not ours to rewrite."""
        offenders = []
        for name in sorted(os.listdir(_JS_DIR)):
            if not name.endswith(".js") or name == _HELPER:
                continue
            if "setInterval(" in _readFile(os.path.join(_JS_DIR, name)):
                offenders.append(name)

        self.assertEqual(offenders, [])

    def test_an_expired_session_stops_its_poll_for_good(self):
        """Both 401 branches stop through the handle, so the tab switch that
        follows cannot restart them (see tests/test_visibility_poll.js)."""
        for name, script in self.scripts.items():
            with self.subTest(script=name):
                self.assertIn(".stop()", script)


class TestStaleDataIsMarked(unittest.TestCase):
    """A poll that stops answering used to leave the last payload on screen
    indefinitely - a track that ended twenty minutes ago, rendered exactly like
    one playing now. The threshold itself is tested in node
    (tests/test_dashboard_page.js::pollIsStale); these are the parts of applying
    it that node cannot reach."""

    def setUp(self):
        self.script = _readFile(os.path.join(_JS_DIR, "dashboard-page.js"))
        self.css = _readFile(_CSS_PATH)

    def test_both_poll_fed_blocks_are_marked_not_just_your_own_track(self):
        """One request feeds the now-playing block and the friends block, so
        when it stops answering both are equally out of date."""
        self.assertIn("card.classList.toggle('is-stale'", self.script)
        self.assertIn("friendsRow.classList.toggle('is-stale'", self.script)

    def test_a_successful_poll_clears_the_mark(self):
        self.assertIn("consecutiveFailures = 0", self.script)

    def test_an_expired_session_marks_it_too(self):
        """The poll stops there, so that data is stale from then on - silently
        freezing is the same defect with a different cause."""
        session = self.script[self.script.index("resp.status === 401"):]
        session = session[:session.index("return null;")]

        self.assertIn("setStale(true)", session)

    def test_the_mark_is_visible(self):
        rule = re.search(r"\.is-stale\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(rule, "nothing renders the stale mark")
        self.assertIn("opacity", rule.group(1))

    def test_the_progress_tick_is_gated_on_the_same_mark(self):
        """The bar runs on its own 1s timer, off the last payload's anchor, so
        it is the one thing on the card that keeps LOOKING live after the poll
        feeding it has stopped. The rule itself is tested in node
        (progressShouldAdvance); this pins that the tick actually asks it."""
        tick = self.script[self.script.index("function tickProgress()"):]
        tick = tick[:tick.index("}")]

        self.assertIn("progressShouldAdvance(", tick)

    def test_an_expired_session_stops_the_tick_as_well_as_the_poll(self):
        """Two handles, one dead session. The tick's was discarded, so a 401
        left a 1s timer running for the life of the tab - the leak the whole
        VisibilityPoll change exists to close, reintroduced by the timer added
        beside it."""
        session = self.script[self.script.index("resp.status === 401"):]
        session = session[:session.index("return null;")]

        self.assertIn("pollHandle.stop()", session)
        self.assertIn("tickHandle.stop()", session)


if __name__ == "__main__":
    unittest.main()
