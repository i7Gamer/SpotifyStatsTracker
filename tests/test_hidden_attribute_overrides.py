"""`hidden` only hides an element whose own `display` doesn't outrank it.

The HTML `hidden` attribute works through a UA rule of `display: none`, which any
author rule setting `display` beats. Two classes in this stylesheet do exactly
that - `.button` is inline-flex and `.milestone-item` is flex - so JS setting
`el.hidden = true` on them changes nothing on screen without an explicit
`[hidden]` override.

This is here because the JS side is already well covered and the CSS side was
not: tests/test_back_button.js fully pins resolveBackTarget and
hasEarlierHistoryEntry across 8 cases, so deleting `.button[hidden]` from
style.css left every one of those green while the back button stayed visible on a
tab with nothing to go back to - the exact bug 002e539/23acdc5 fixed.

File-level assertions, no browser needed - the same approach as
test_playlist_tag_hover.py and test_detail_toolbar_layout.py.
"""
import os
import re
import unittest

_CSS_PATH = os.path.join(os.path.dirname(__file__), "..", "static", "css", "style.css")
_JS_DIR = os.path.join(os.path.dirname(__file__), "..", "static", "js")

# Every selector whose own display would otherwise win, and the JS that relies on
# the override actually taking effect.
HIDDEN_OVERRIDES = (
    (".button[hidden]", "back-button.js"),
    (".milestone-item[hidden]", "milestones.js"),
)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class HiddenAttributeOverrideTestCase(unittest.TestCase):
    def setUp(self):
        self.css = _read(_CSS_PATH)

    def test_each_override_exists_and_sets_display_none(self):
        for selector, _js in HIDDEN_OVERRIDES:
            with self.subTest(selector=selector):
                match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.css)

                self.assertIsNotNone(match, f"{selector} is missing - anything the JS marks "
                                            "hidden stays on screen")
                self.assertIn("display: none", match.group(1))

    def test_the_base_class_really_does_set_a_competing_display(self):
        """Negative control for the premise: if .button stopped setting display,
        the override would be redundant and this file would be pinning noise."""
        for selector in (".button", ".milestone-item"):
            with self.subTest(selector=selector):
                match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.css)

                self.assertIsNotNone(match)
                self.assertIn("display:", match.group(1))

    def test_something_actually_sets_hidden_from_js(self):
        """The override is only load-bearing while JS drives it. If nothing does,
        this whole file should go rather than quietly guard dead CSS."""
        sources = " ".join(_read(os.path.join(_JS_DIR, name))
                           for name in os.listdir(_JS_DIR) if name.endswith(".js"))

        self.assertRegex(sources, r"\.hidden\s*=|setAttribute\(\s*['\"]hidden['\"]")


if __name__ == "__main__":
    unittest.main()
