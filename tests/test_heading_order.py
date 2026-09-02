"""Every page has one h1 and no heading level is skipped (2026-09-02 review,
UI-05).

The dashboard opened on twelve h2s with no h1, /history had no heading at
all, and the Top pages, /history's list, the Wrapped stat tiles and the
login pages' cookie instructions all jumped from the shell's h1 straight to
an h3 - so heading navigation landed on "Now playing" or nothing, and every
list read as a level deeper than it is. The fixes are visually-quiet
headings where the outline had a hole and one level up where a partial sat
under the wrong parent; these renders keep the outline whole.

Two-phase pages are checked as the browser ends up showing them: the shell
plus the fragment htmx swaps in (the _detail_client.py pattern - appended,
since the assertions are about order, not position).
"""
import unittest

from _app_factory import AppTestCase
from _headings import assertHeadingOrder
from test_css_class_references import _parseRules, _readFile, _CSS_PATH
from test_history_htmx import HistoryHtmxTestCase, HX_HEADERS
from test_top_list_default_window import TopListWindowTestCase, TOP_LIST_PATHS
from test_wrapped_htmx import WrappedHtmxTestCase

import bs4

#< pages a visitor sees before logging in; each renders the cookie
#  instructions partial under its h1
_LOGGED_OUT_PATHS = ("/login", "/register", "/reset-password")
#< the two pages that had no h1 of their own
_DASHBOARD_PATH = "/"
_HISTORY_PATH = "/history"


class TestLoggedOutPages(AppTestCase):
    def test_each_page_has_one_h1_and_no_skipped_level(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        for path in _LOGGED_OUT_PATHS:
            with self.subTest(path=path):
                assertHeadingOrder(self, client.get(path).get_data(as_text=True), path)


class TestDashboardAndHistory(HistoryHtmxTestCase):
    def test_the_dashboard_has_one_h1_and_no_skipped_level(self):
        self._login()

        assertHeadingOrder(self, self.client.get(_DASHBOARD_PATH).get_data(as_text=True), _DASHBOARD_PATH)

    def test_history_with_its_list_has_one_h1_and_no_skipped_level(self):
        self._login()

        shell = self.client.get(_HISTORY_PATH).get_data(as_text=True)
        fragment = self.client.get(_HISTORY_PATH, headers=HX_HEADERS).get_data(as_text=True)

        assertHeadingOrder(self, shell + fragment, _HISTORY_PATH)


class TestTopPages(TopListWindowTestCase):
    def test_each_top_page_with_its_list_has_one_h1_and_no_skipped_level(self):
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                assertHeadingOrder(self, self._shell(path) + self._list(path), path)


class TestWrapped(WrappedHtmxTestCase):
    """Checked separately rather than concatenated: the fragment carries the
    hero (with the h1) out of band, so shell + fragment would count it twice
    where the browser replaces it."""

    def test_the_page_has_one_h1_and_no_skipped_level(self):
        assertHeadingOrder(self, self._page(), "/wrapped")

    def test_the_fragment_has_one_h1_and_no_skipped_level(self):
        assertHeadingOrder(self, self._fragment(), "/wrapped fragment")


class TestCookieInstructionsHeading(unittest.TestCase):
    """The partial's heading moved from h3 to h2 to sit under the login h1;
    the rule that styled it has to move with it, at the size it had."""

    def test_the_instructions_heading_rule_reaches_an_h2(self):
        rules = _parseRules(_readFile(_CSS_PATH))
        soup = bs4.BeautifulSoup('<section class="instructions"><h2>How</h2></section>', "html.parser")
        heading = soup.find("h2")
        reaching = [rule for rule in rules if rule.depth == 0 and rule.hits(soup, heading)
                    and rule.declaration("margin-top") == "0"]

        self.assertEqual(len(reaching), 1)
        self.assertEqual(reaching[0].declaration("color"), "var(--text)")
        self.assertIsNotNone(reaching[0].declaration("font-size"), "the h3 size must come with the move")


if __name__ == "__main__":
    unittest.main()
