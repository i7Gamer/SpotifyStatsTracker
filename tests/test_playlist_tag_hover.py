"""Static guards for the playlist page's two interactive tag controls.

Both looked dead under the cursor. The Delete button carried its danger tint as
an *inline* style, which beats `.button:hover` in the cascade, so hovering it
changed nothing at all. The tag chips had no glow of their own, and their own
inline background beats `.button:hover`'s fill the same way - so they need the
box-shadow treatment the "Play now" / Spotify-link pills already use, which
nothing inline can override.

Cheap file-level assertions - no browser needed.
"""
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CSS_PATH = os.path.join(_ROOT, "static", "css", "style.css")
_TEMPLATE_PATH = os.path.join(_ROOT, "templates", "playlists.html")

_GLOW_PROPERTY = "box-shadow"


def _readFile(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestPlaylistTagHover(unittest.TestCase):
    def setUp(self):
        self.css = _readFile(_CSS_PATH)
        self.template = _readFile(_TEMPLATE_PATH)

    def test_delete_button_has_a_hover_state(self):
        rules = self._ruleFor(".btn-delete-tag:hover")
        self.assertIn("background", rules)

    def test_delete_button_styling_is_not_inline(self):
        # An inline background/color wins over :hover, so the button has to take
        # its resting look from the stylesheet too.
        markup = self._elementFor("btn-delete-tag")
        for property_ in ("background", "color"):
            with self.subTest(property=property_):
                self.assertNotIn(property_, self._inlineStyleOf(markup))
        self.assertIn("background", self._ruleFor(".btn-delete-tag"))

    def test_delete_button_styling_outranks_the_plain_button_rule(self):
        # `.btn-delete-tag` and `.button` are the same specificity, so source
        # order alone decides - placed above `.button` the danger tint loses to
        # its `background: transparent` and the button renders like any other.
        self.assertGreater(self.css.index(".btn-delete-tag {"), self.css.index("\n.button {"))

    def test_tag_chips_glow_on_hover(self):
        self.assertIn(_GLOW_PROPERTY, self._ruleFor(".playlist-tag-chip:hover"))

    def test_the_chip_glow_matches_the_play_now_and_spotify_pills(self):
        chipGlow = self._declaration(self._ruleFor(".playlist-tag-chip:hover"), _GLOW_PROPERTY)
        playNowGlow = self._declaration(self._ruleFor(".play-now-button:hover"), _GLOW_PROPERTY)
        spotifyGlow = self._declaration(self._ruleFor(".track-spotify-link:hover"), _GLOW_PROPERTY)
        self.assertEqual(playNowGlow, chipGlow)
        self.assertEqual(spotifyGlow, chipGlow)

    def _elementFor(self, className):
        """The opening tag of the element carrying this class."""
        classAt = self.template.index(className)
        startAt = self.template.rindex("<", 0, classAt)
        return self.template[startAt:self.template.index(">", classAt) + 1]

    def _inlineStyleOf(self, markup):
        match = re.search(r'style="([^"]*)"', markup)
        return match.group(1) if match else ""

    def _declaration(self, rules, property_):
        match = re.search(re.escape(property_) + r"\s*:\s*([^;]+)", rules)
        self.assertIsNotNone(match, "missing declaration " + property_)
        return match.group(1).strip()

    def _ruleFor(self, selector):
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(match, "missing CSS rule for " + selector)
        return match.group(1)


if __name__ == "__main__":
    unittest.main()
