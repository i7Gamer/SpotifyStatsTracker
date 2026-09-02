"""Static guards for the song/album/artist detail pages' toolbar row.

Two things that row has to keep doing, neither of which a route test can see:

* The controls on the right (admin "Refresh Last.fm Data", "Trend buckets")
  stay on the right when back-button.js hides the back button - which it does
  on any page opened in a new tab. `justify-content: space-between` alone puts
  the last remaining child on the *left*, so the group needs its own auto
  margin.
* The admin refresh result appears inline in that same row rather than as a
  block above it, where it pushed the whole page down by its own height plus a
  margin.

Cheap file-level assertions - no browser needed.
"""
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CSS_PATH = os.path.join(_ROOT, "static", "css", "style.css")
_REFRESH_JS_PATH = os.path.join(_ROOT, "static", "js", "admin-refresh.js")
#< the artist and album shells are thin wrappers around _entity_detail_shell.html
#  (2026-09-02, UI-11), which is where their toolbar row now lives
_DETAIL_TEMPLATES = ("song_detail.html", "_entity_detail_shell.html")

_TOOLBAR_MARKER = 'class="detail-toolbar"'
_ACTIONS_MARKER = 'class="detail-toolbar-actions"'
_FLASH_MARKER = 'id="detail-flash"'


def _readFile(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestDetailToolbarLayout(unittest.TestCase):
    def setUp(self):
        self.css = _readFile(_CSS_PATH)
        self.templates = {
            name: _readFile(os.path.join(_ROOT, "templates", name))
            for name in _DETAIL_TEMPLATES
        }

    def test_right_hand_controls_use_a_named_class(self):
        # An inline `display: flex` wrapper can't carry the auto margin rule.
        for name, template in self.templates.items():
            with self.subTest(template=name):
                self.assertIn(_ACTIONS_MARKER, template)

    def test_right_hand_controls_stay_right_without_the_back_button(self):
        rules = self._ruleFor(".detail-toolbar-actions")
        self.assertIn("margin-left: auto", rules)

    def test_toolbar_still_spreads_its_children(self):
        self.assertIn("justify-content: space-between", self._ruleFor(".detail-toolbar"))

    def test_flash_slot_sits_inside_the_toolbar(self):
        for name, template in self.templates.items():
            with self.subTest(template=name):
                toolbarAt = template.index(_TOOLBAR_MARKER)
                flashAt = template.index(_FLASH_MARKER)
                actionsAt = template.index(_ACTIONS_MARKER)
                self.assertGreater(flashAt, toolbarAt, "flash slot still sits above the toolbar")
                self.assertLess(flashAt, actionsAt, "flash slot must precede the right-hand controls")

    def test_flash_messages_add_no_vertical_space(self):
        # The reported bug: a `margin-bottom` on the message shoved everything
        # below it down. Inline styles beat the stylesheet, so neither the
        # server-rendered nor the fetch-rendered message may carry one.
        for name, template in self.templates.items():
            with self.subTest(template=name):
                flashBlock = template[template.index(_FLASH_MARKER):template.index(_ACTIONS_MARKER)]
                self.assertNotIn("margin-bottom", flashBlock)
        self.assertNotIn("marginBottom", _readFile(_REFRESH_JS_PATH))
        self.assertIn("margin: 0", self._ruleFor("#detail-flash p"))

    def _ruleFor(self, selector):
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(match, "missing CSS rule for " + selector)
        return match.group(1)


if __name__ == "__main__":
    unittest.main()
