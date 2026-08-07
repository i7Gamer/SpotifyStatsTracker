"""Static guards for the Now Playing panel's own layout.

Two things the panel has to keep doing, neither of which a route test can see:

* From full-nav widths up, the "Now playing" heading sits BESIDE the cover art
  instead of above the whole block. With a 112px cover, a full-width heading row
  left an empty strip to the left of the cover's top half and pushed the card
  taller for nothing. Below that breakpoint the cover is 56px and the heading
  keeps the full width, so the two are one grid-template-areas line apart - a
  line that is a single word away from putting the cover under the heading.
* The blocks the panel stacks - your own now-playing, the friends who are
  listening, the listening streak - are independently hidden, so the heading,
  the track text and the cover have to be siblings in ONE grid. A wrapper around
  the text+cover row (what .now-playing-body used to be) puts the heading in a
  different formatting context, where no media query can move it beside a cover
  it isn't a grid sibling of.

Cheap file-level assertions - no browser needed.
"""
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CSS_PATH = os.path.join(_ROOT, "static", "css", "style.css")
_TEMPLATE_PATH = os.path.join(_ROOT, "templates", "tracks.html")

#< heading beside the cover, track text under the heading
_DESKTOP_AREAS = ('"head cover"', '"meta cover"')
#< heading across the top, track text beside the (smaller) cover
_NARROW_AREAS = ('"head head"', '"meta cover"')


def _readFile(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _mediaBlocks(css):
    """(query, body) for every @media block in `css`.

    Brace-matched rather than regexed: a media block holds whole rules, so the
    `[^}]*` shortcut the single-rule helpers use would stop at the first rule's
    closing brace and report an empty block.
    """
    blocks = []
    for match in re.finditer(r"@media([^{]*)\{", css):
        depth, index = 1, match.end()
        while depth and index < len(css):
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
            index += 1
        blocks.append((match.group(1).strip(), css[match.end():index - 1]))
    return blocks


class NowPlayingLayoutTestCase(unittest.TestCase):
    def setUp(self):
        self.css = _readFile(_CSS_PATH)
        self.template = _readFile(_TEMPLATE_PATH)

    def _ruleFor(self, selector, css=None):
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css if css is not None else self.css)
        self.assertIsNotNone(match, "missing CSS rule for " + selector)
        return match.group(1)

    def _mediaBlockDefining(self, selector):
        """The @media query whose body carries a rule for `selector`."""
        for query, body in _mediaBlocks(self.css):
            if re.search(re.escape(selector) + r"\s*[,{]", body):
                return query, body
        self.fail(selector + " is not defined inside any @media block")


class TestTheBlockIsOneGrid(NowPlayingLayoutTestCase):
    def test_the_heading_track_text_and_cover_are_grid_siblings(self):
        rule = self._ruleFor(".now-playing-inner")

        self.assertIn("display: grid", rule)
        for area in _NARROW_AREAS:
            self.assertIn(area, rule)

    def test_the_row_wrapper_is_gone_from_the_markup_and_the_stylesheet(self):
        """A wrapper around the text+cover row re-nests the cover, and the
        heading can only move beside a cover it shares a grid with."""
        self.assertNotIn("now-playing-body", self.template)
        self.assertNotIn(".now-playing-body", self.css)

    def test_each_part_names_the_area_it_occupies(self):
        self.assertIn("grid-area: head", self._ruleFor(".now-playing-heading"))
        self.assertIn("grid-area: meta", self._ruleFor(".now-playing-meta"))
        self.assertIn("grid-area: cover", self._ruleFor(".now-playing-cover-link"))

    def test_the_heading_carries_the_class_the_grid_places(self):
        self.assertIn('class="now-playing-heading"', self.template)


class TestTheHeadingMovesBesideTheCoverOnDesktop(NowPlayingLayoutTestCase):
    def test_the_desktop_area_map_puts_the_heading_next_to_the_cover(self):
        _, body = self._mediaBlockDefining(".now-playing-inner")
        rule = self._ruleFor(".now-playing-inner", css=body)

        for area in _DESKTOP_AREAS:
            self.assertIn(area, rule)

    def test_it_moves_at_the_same_width_the_cover_grows_to_112px(self):
        """Two different breakpoints would give a band of widths where the
        heading sits beside a cover that is still a 56px thumbnail."""
        headingQuery, _ = self._mediaBlockDefining(".now-playing-inner")
        coverQuery, _ = self._mediaBlockDefining(".now-playing-cover")

        self.assertIn("min-width", headingQuery)
        self.assertEqual(headingQuery, coverQuery)

    def test_the_heading_hugs_the_top_of_the_cover(self):
        """Beside a 112px cover the heading's grid row is taller than the
        heading; without this it floats in the middle of the cover's top half."""
        self.assertIn("align-self: start", self._ruleFor(".now-playing-heading"))

    def test_the_track_text_still_sits_on_the_cover_s_base(self):
        self.assertIn("align-items: end", self._ruleFor(".now-playing-inner"))


if __name__ == "__main__":
    unittest.main()
