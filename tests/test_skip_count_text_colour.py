"""Static guard: skip counts read as part of the `.track-meta` line they sit on.

Two places render a skip count onto a track-meta line, and both once tinted the
number apart from the muted text around it:

* the Top-list pill (`.track-label.skip-label`), fixed earlier, and
* the detail hero card's skip summary (templates/_skip_stat.html), which kept a
  `color: var(--text)` wrapper from when the summary was a standalone block
  below the card rather than part of the card's own meta line.

Neither may declare a `color` of its own: `.track-meta` sets `var(--muted)` for
the whole line and everything on it inherits that. A pill may still set itself
apart through background/border, which don't affect the text colour.

Cheap file-level assertions - no browser needed.
"""
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CSS_PATH = os.path.join(_ROOT, "static", "css", "style.css")
_SKIP_STAT_TEMPLATE = os.path.join(_ROOT, "templates", "_skip_stat.html")

_LINE_SELECTOR = ".track-meta"
_LINE_COLOUR = "var(--muted)"
#< Selectors for skip text living on a .track-meta line. A rule may be absent
#  entirely - that just means the text inherits, which is the point.
_SKIP_TEXT_SELECTORS = (".detail-skip-stat", ".detail-skip-count", ".track-label.skip-label")


def _readFile(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestSkipCountTextColour(unittest.TestCase):
    def setUp(self):
        self.css = _readFile(_CSS_PATH)
        self.template = _readFile(_SKIP_STAT_TEMPLATE)

    def test_the_meta_line_still_sets_the_colour_everything_on_it_inherits(self):
        # If this ever moves, the selectors below stop being guards.
        self.assertEqual(self._declaration(_LINE_SELECTOR, "color"), _LINE_COLOUR)

    def test_no_skip_text_overrides_the_meta_lines_colour(self):
        for selector in _SKIP_TEXT_SELECTORS:
            with self.subTest(selector=selector):
                self.assertIsNone(self._declaration(selector, "color"))

    def test_the_detail_summary_wraps_the_count_in_nothing_that_could_tint_it(self):
        """The count carried a <span> whose only purpose was that colour, so
        with the rule gone the span goes too rather than lingering as dead
        markup for the next reader to wonder about."""
        self.assertNotIn("detail-skip-count", self.template)

    def _declaration(self, selector, property_):
        """The value of `property_` in `selector`'s rule, or None if either the
        rule or the declaration is absent."""
        rule = re.search(re.escape(selector) + r"\s*(?:,[^{}]*)?\{([^}]*)\}", self.css)
        if rule is None:
            return None
        match = re.search(r"(?:^|;)\s*" + re.escape(property_) + r"\s*:\s*([^;]+)", rule.group(1))
        return match.group(1).strip() if match else None


if __name__ == "__main__":
    unittest.main()
