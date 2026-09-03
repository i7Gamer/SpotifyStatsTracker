"""The auth pages' chrome (2026-09-02 review, UT-6).

templates/login.html, register.html and reset_password.html extended
layout.html, whose full topbar - the Dashboard/Top Stats/Insights nav, the
account dropdown and its Log Out button - rendered regardless of login
state, since layout.html builds it unconditionally rather than gating it on
whether anyone is logged in. A keyboard user landed on one of these pages and
had to tab through all of it - links to pages they cannot reach yet, and a
logout control for a session that does not exist - before reaching the actual
login form.

None of the three pages use any layout.html block besides title/content, and
none of them queue a Flask flash() (their own `error` template variable does
that job), so the fix is the smaller of the two the review called out:
extend templates/layout_public.html instead - the same layout the public
Wrapped share page already uses for exactly this (no topbar, no nav, just the
skip link, page shell and shared chrome scripts) - rather than teaching
layout.html to gate its nav on login state.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from tests._app_factory import AppTestCase
from tests.test_history_htmx import HistoryHtmxTestCase

#< every page that extended layout.html directly and gained a topbar it
#  never needed
AUTH_PATHS = ("/login", "/register", "/reset-password")


class TestLoggedOutAuthPagesHaveNoNav(AppTestCase):
    def test_no_nav_menu_or_toggle(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        for path in AUTH_PATHS:
            with self.subTest(path=path):
                body = client.get(path).get_data(as_text=True)

                self.assertNotIn('id="nav-menu"', body)
                self.assertNotIn('id="nav-toggle"', body)

    def test_no_log_out_button(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        for path in AUTH_PATHS:
            with self.subTest(path=path):
                body = client.get(path).get_data(as_text=True)

                self.assertNotIn(">Log Out<", body)

    def test_the_skip_link_and_brand_survive(self):
        """layout_public.html still has to carry the two chrome pieces that
        matter with no nav present: the skip-to-content link (there is
        nothing else focusable before the form) and a way back to the app."""
        dash = self._makeApp()
        client = dash.app.test_client()

        body = client.get("/login").get_data(as_text=True)

        self.assertIn('class="skip-link"', body)


class TestLoggedInPagesStillShowNav(HistoryHtmxTestCase):
    """The control: a page reached while logged in must keep its full nav -
    this fix only removes it from the three pages that never had a session to
    show one for."""

    def test_a_logged_in_page_still_renders_the_nav_and_log_out_button(self):
        body = self._loggedInShell()

        self.assertIn('id="nav-menu"', body)
        self.assertIn('id="nav-toggle"', body)
        self.assertIn(">Log Out<", body)


if __name__ == "__main__":
    unittest.main()
