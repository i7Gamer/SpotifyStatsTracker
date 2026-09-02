"""The admin settings cards' header styles live in the stylesheet, not in
seven copies of the same style attribute (2026-09-02 review, UI-13).

admin.html repeated `style="padding: 0;"` on every settings card and the same
h2 / p typography inside each `.admin-card-header`, and overview.html did the
same for its six stats-card values - a typographic change was a seven-place
edit. The rules now sit on the classes that already existed, and the counts
below are ratchets so the attributes cannot creep back.

This is not the declined "extract inline styles for a strict CSP" item (that
one is unreachable by its own means); it is a verbatim repeat.
"""
import os
import re
import unittest

import bs4

from tests.test_css_class_references import _parseRules

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CSS_PATH = os.path.join(_ROOT, "static", "css", "style.css")
_ADMIN_PATH = os.path.join(_ROOT, "templates", "admin.html")
_OVERVIEW_PATH = os.path.join(_ROOT, "templates", "overview.html")

#< the inline-style counts the two templates were left at once the repeated
#  card-header and stats-value attributes moved into the stylesheet (admin
#  119 -> 96, overview 55 -> 49); lower them as more move out
ADMIN_INLINE_STYLE_CEILING = 96
OVERVIEW_INLINE_STYLE_CEILING = 49
#< how many settings cards the admin tabs render between them
ADMIN_SETTINGS_CARD_COUNT = 7
_INLINE_STYLE = re.compile(r'style="')


def _readFile(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestInlineStyleRatchet(unittest.TestCase):
    def test_admin_inline_style_count_does_not_climb_back(self):
        self.assertLessEqual(len(_INLINE_STYLE.findall(_readFile(_ADMIN_PATH))), ADMIN_INLINE_STYLE_CEILING)

    def test_overview_inline_style_count_does_not_climb_back(self):
        self.assertLessEqual(len(_INLINE_STYLE.findall(_readFile(_OVERVIEW_PATH))), OVERVIEW_INLINE_STYLE_CEILING)

    def test_the_settings_cards_carry_the_class_and_no_padding_attribute(self):
        admin = _readFile(_ADMIN_PATH)

        #< counts rather than assertNotIn, which would print the template
        self.assertEqual(admin.count('<article class="card admin-settings-card">'), ADMIN_SETTINGS_CARD_COUNT)
        self.assertEqual(admin.count('style="padding: 0;"'), 0)
        self.assertEqual(admin.count('<h2 style="margin: 0; font-family: var(--font-headings); font-size: 1.15rem;">'), 0)

    def test_the_stats_values_carry_no_size_attribute(self):
        for path in (_ADMIN_PATH, _OVERVIEW_PATH):
            with self.subTest(template=os.path.basename(path)):
                self.assertEqual(_readFile(path).count('class="summary-value" style='), 0)


class TestTheRulesReachTheMarkup(unittest.TestCase):
    """Evaluated with soupsieve against the markup shape the templates emit,
    so a rule that exists but misses (a typo'd class, a lost descendant
    combinator) fails here rather than on /admin."""

    def setUp(self):
        self.rules = _parseRules(_readFile(_CSS_PATH))

    def _declarationsFor(self, soup, element, props):
        """The last top-level declaration of each prop that reaches `element`,
        in source order - the cascade at equal specificity."""
        declared = {}
        for rule in self.rules:
            if rule.depth == 0 and rule.hits(soup, element):
                for prop in props:
                    value = rule.declaration(prop)
                    if value is not None:
                        declared[prop] = value
        return declared

    def test_a_settings_card_is_flush_with_a_titled_header(self):
        soup = bs4.BeautifulSoup(
            '<article class="card admin-settings-card">'
            '<div class="card-header admin-card-header"><h2>Backups</h2><p>Snapshots.</p></div>'
            '</article>', "html.parser")

        article = self._declarationsFor(soup, soup.find("article"), ("padding",))
        heading = self._declarationsFor(soup, soup.find("h2"), ("margin", "font-size"))
        blurb = self._declarationsFor(soup, soup.find("p"), ("color", "font-size", "margin-top"))

        self.assertEqual(article, {"padding": "0"})
        self.assertEqual(heading, {"margin": "0", "font-size": "1.15rem"})
        self.assertEqual(blurb, {"color": "var(--muted)", "font-size": "0.85rem", "margin-top": "0.25rem"})

    def test_the_users_table_header_keeps_its_larger_heading(self):
        """The pinned Registered Users card shares .admin-card-header but is
        not a settings card: its h2 stays at the default heading size."""
        soup = bs4.BeautifulSoup(
            '<section class="card">'
            '<div class="card-header admin-card-header"><h2>Registered Users</h2></div>'
            '</section>', "html.parser")

        heading = self._declarationsFor(soup, soup.find("h2"), ("margin", "font-size"))

        self.assertEqual(heading.get("margin"), "0")
        self.assertNotEqual(heading.get("font-size"), "1.15rem")

    def test_a_stats_card_value_is_sized_by_the_card(self):
        soup = bs4.BeautifulSoup(
            '<article class="card stats-card"><div class="stats-card-info">'
            '<h2 class="summary-value">1,234</h2></div></article>', "html.parser")

        value = self._declarationsFor(soup, soup.find("h2"), ("font-size", "margin"))

        self.assertEqual(value, {"font-size": "2.2rem", "margin": "0.5rem 0 0 0"})


if __name__ == "__main__":
    unittest.main()
