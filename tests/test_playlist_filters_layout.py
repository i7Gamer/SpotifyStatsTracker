"""Static guards for the playlist exporter's filter row layout.

The three controls (Match Mode / Sort Order / File Format) sit in a grid, but
`.filter-group` is a horizontal label+control row built for the wrapping filter
bars. Inside a fixed grid column the selects keep their intrinsic width (their
longest option text) and spill over the neighbouring group. These assert the
stacked, width-capped override stays in place - cheap, no browser needed.
"""
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CSS_PATH = os.path.join(_ROOT, "static", "css", "style.css")
_TEMPLATE_PATH = os.path.join(_ROOT, "templates", "playlists.html")


def _readFile(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestPlaylistFilterLayout(unittest.TestCase):
    def setUp(self):
        self.css = _readFile(_CSS_PATH)
        self.template = _readFile(_TEMPLATE_PATH)

    def test_template_uses_the_shared_filter_grid_class(self):
        self.assertIn('class="playlist-filters"', self.template)

    def test_template_does_not_reintroduce_an_inline_filter_grid(self):
        # An inline grid-template-columns would silently win over the class.
        inlineGrids = re.findall(r'style="[^"]*grid-template-columns[^"]*"', self.template)
        self.assertEqual([], inlineGrids)

    def test_filter_groups_stack_label_above_control(self):
        rules = self._ruleFor(".playlist-filters .filter-group")
        self.assertIn("flex-direction: column", rules)

    def test_selects_cannot_outgrow_their_grid_column(self):
        rules = self._ruleFor(".playlist-filters .filter-group select")
        self.assertIn("min-width: 0", rules)
        self.assertIn("max-width: 100%", rules)

    def _ruleFor(self, selector):
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(match, "missing CSS rule for " + selector)
        return match.group(1)


if __name__ == "__main__":
    unittest.main()
