"""Static guard for the timeline's decorative stem (static/css/style.css).

.timeline-stem is a 2px absolutely-positioned line drawn down the middle of
the detail-page timeline for decoration only. Sitting on top of the timeline
column, it intercepted clicks aimed at the centre of "Show More Plays"
(measured with document.elementsFromPoint in a real browser) - the button
looked dead even though it was rendered and enabled. `pointer-events: none`
lets clicks pass through it to whatever is actually underneath.

Cheap file-level assertions - no browser needed. Helpers copied from
tests/test_playlist_tag_hover.py (_ruleFor / _declaration).
"""
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CSS_PATH = os.path.join(_ROOT, "static", "css", "style.css")


def _readFile(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestTimelineStemIgnoresClicks(unittest.TestCase):
    def setUp(self):
        self.css = _readFile(_CSS_PATH)

    def test_the_stem_lets_clicks_pass_through(self):
        """The base rule (the first `.timeline-stem {` block) must declare
        pointer-events: none, or the stem keeps intercepting clicks aimed at
        whatever sits behind it - as it does at the centre of the "Show More
        Plays" button."""
        self.assertEqual(self._declaration(self._ruleFor(".timeline-stem"), "pointer-events"), "none")

    def test_the_mobile_override_does_not_turn_clicks_back_on(self):
        """The @media (max-width: 768px) override only repositions the stem
        (left/transform) - it must not re-enable pointer events there and
        silently undo the fix on mobile."""
        mobileRule = self._ruleFor(".timeline-stem", occurrence=2)
        self.assertNotRegex(mobileRule, r"pointer-events\s*:\s*(?!none\b)\S+")

    def _declaration(self, rules, property_):
        match = re.search(re.escape(property_) + r"\s*:\s*([^;]+)", rules)
        self.assertIsNotNone(match, "missing declaration " + property_)
        return match.group(1).strip()

    def _ruleFor(self, selector, occurrence=1):
        """The Nth (1-based) `{selector} { ... }` block's body."""
        pattern = re.compile(re.escape(selector) + r"\s*\{([^}]*)\}")
        matches = list(pattern.finditer(self.css))
        self.assertGreaterEqual(len(matches), occurrence, "missing CSS rule for " + selector)
        return matches[occurrence - 1].group(1)


if __name__ == "__main__":
    unittest.main()
