"""The scroll-to-top button stays out of the open mobile drawer
(2026-09-02 review, UI-14).

.scroll-to-top is position: fixed in the root stacking context at z-index
99; the drawer lives inside the sticky .topbar, whose z-index 10 makes it a
stacking context of its own - so 99 beats the whole topbar subtree, and on a
phone scrolled past the button's show line the round button floated over the
drawer's bottom-right corner, where its last row sits. The fix is a rule in
the drawer's own media block, evaluated here with soupsieve rather than
matched as a string.
"""
import os
import unittest

import bs4

from tests.test_css_class_references import _parseRules

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CSS_PATH = os.path.join(_ROOT, "static", "css", "style.css")

#< the breakpoint that turns .nav-links into the drawer (and shows the burger)
_DRAWER_MEDIA = "@media (max-width: 1024px)"


def _readFile(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestScrollToTopHidesUnderTheDrawer(unittest.TestCase):
    def setUp(self):
        self.drawerRules = [rule for rule in _parseRules(_readFile(_CSS_PATH))
                            if rule.enclosing == _DRAWER_MEDIA]

    def _button(self, bodyClass):
        soup = bs4.BeautifulSoup(
            f'<body class="{bodyClass}"><main></main><button class="scroll-to-top"></button></body>',
            "html.parser")
        return soup, soup.find("button")

    def _drawerRulesHitting(self, bodyClass):
        soup, button = self._button(bodyClass)
        return [rule for rule in self.drawerRules if rule.hits(soup, button)]

    def test_the_button_is_hidden_and_untouchable_while_the_drawer_is_open(self):
        hiding = [rule for rule in self._drawerRulesHitting("nav-open")
                  if rule.declaration("visibility") == "hidden"]

        self.assertEqual(len(hiding), 1)
        self.assertEqual(hiding[0].declaration("pointer-events"), "none")
        #< chrome-common.js writes display inline on every scroll tick, so a
        #  display rule here would lose without !important; visibility wins
        self.assertIsNone(hiding[0].declaration("display"))

    def test_the_button_is_left_alone_while_the_drawer_is_closed(self):
        hiding = [rule for rule in self._drawerRulesHitting("")
                  if rule.declaration("visibility") == "hidden"]

        self.assertEqual(hiding, [])


if __name__ == "__main__":
    unittest.main()
