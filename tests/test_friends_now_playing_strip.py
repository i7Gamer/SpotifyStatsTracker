"""The dashboard markup for the friends-listening strip.

The strip lives between the live cards row and the trends row, and is populated
by the existing now-playing poll. What's pinned here is when the markup exists
at all, and that it doesn't reuse .summary-card - those are flex: 1 1 0, so two
friends would each stretch wider than the viewer's own Now Playing card while
holding less content.
"""
import os
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase
from conftest import makeDashboardDbMock

#< the narrowest .friends-listening-chip cap that still leaves a usable track
#  title beside the 30px cover (see TestStripStyling)
CHIP_TITLE_ROOM_FLOOR_PX = 320


class FriendsStripTestCase(AppTestCase):
    USERNAME = "alice"
    EMAIL = "alice@example.com"

    def setUp(self):
        self.dash = self._makeApp()
        self.dash.repo.upsertUser(self.USERNAME, self.EMAIL)

    def _makeDb(self):
        #< the dashboard route's baseline lives in conftest: this test is about the
        #  friends strip, not about which six queries the page happens to read
        return makeDashboardDbMock()

    def _dashboardHtml(self, hasShares=True):
        client = self.dash.app.test_client()
        with patch.object(self.dash, "is_user_logged_in", return_value=True), \
             patch.object(self.dash, "get_username_for_email", return_value=self.USERNAME), \
             patch.object(self.dash, "get_user_db", return_value=self._makeDb()), \
             patch.object(self.dash.repo, "hasAnyAcceptedShare", return_value=hasShares):
            with client.session_transaction() as sess:
                sess["email"] = self.EMAIL
            return client.get("/").data.decode()


class TestWhenTheStripIsRendered(FriendsStripTestCase):
    def test_present_for_a_user_with_shares(self):
        self.assertIn('id="friendsListening"', self._dashboardHtml())

    def test_absent_for_a_user_with_no_shares(self):
        """Not merely hidden - a solo user gets no markup for it at all."""
        self.assertNotIn('id="friendsListening"', self._dashboardHtml(hasShares=False))

    def test_absent_when_the_admin_switch_is_off(self):
        self.dash.repo.setFriendsNowPlayingEnabled(False)

        self.assertNotIn('id="friendsListening"', self._dashboardHtml())

    def test_it_starts_hidden_so_an_empty_strip_costs_no_space(self):
        """Nobody playing is the common case; the poll reveals the row."""
        html = self._dashboardHtml()

        match = re.search(r'<div class="friends-listening" id="friendsListening"([^>]*)>', html)
        self.assertIsNotNone(match)
        self.assertIn("display: none", match.group(1))


class TestStripLayout(FriendsStripTestCase):
    def test_it_sits_between_the_live_cards_and_the_trends_row(self):
        html = self._dashboardHtml()

        nowPlayingIdx = html.index('id="nowPlayingPanel"')
        stripIdx = html.index('id="friendsListening"')
        trendsIdx = html.index('id="dashboardTrendsContainer"')

        self.assertLess(nowPlayingIdx, stripIdx)
        self.assertLess(stripIdx, trendsIdx)

    def test_it_is_inside_the_filter_independent_live_panel(self):
        """Below the filter form it would read as filtered data, which it isn't."""
        html = self._dashboardHtml()

        self.assertLess(html.index('id="friendsListening"'), html.index('class="hero"'))

    def test_chips_do_not_reuse_the_stretching_summary_card_class(self):
        html = self._dashboardHtml()

        # End the slice at the START of the trends div - its own
        # class="dashboard-summary-cards" contains "summary-card" as a substring.
        stripMarkup = html[
            html.index('<div class="friends-listening"'):
            html.index('<div class="dashboard-summary-cards" id="dashboardTrendsContainer"')
        ]
        self.assertNotIn("summary-card", stripMarkup)

    def test_the_chip_container_and_overflow_count_exist_for_the_poll(self):
        html = self._dashboardHtml()

        self.assertIn('id="friendsListeningChips"', html)
        self.assertIn('id="friendsListeningMore"', html)


class TestStripPollIsNotCoupledToNowPlaying(unittest.TestCase):
    """The strip and the Now Playing card share one poll (one request per 15s),
    but they are independently absent: the strip isn't rendered without shares
    or with the admin switch off, and the card could become conditional later.
    Neither may gate the other's polling.

    Asserted against the script source because the coupling is invisible from
    the outside - the poll simply never starts, with no error and no missing
    markup for a render test to catch. (The script moved out of tracks.html into
    static/js so the lint gate could see it; these assertions follow it.)"""

    def setUp(self):
        scriptPath = os.path.join(os.path.dirname(__file__), "..", "static", "js", "dashboard-page.js")
        with open(scriptPath, encoding="utf-8") as handle:
            self.template = handle.read()

    def test_the_poll_starts_when_either_half_is_present(self):
        self.assertIn("if (!card && !friendsRow) return;", self.template)

    def test_the_poll_does_not_bail_on_a_missing_now_playing_card(self):
        """`if (!card) return;` at IIFE scope would kill the strip too."""
        pollScope = self.template[self.template.index("var NOW_PLAYING_POLL_MS"):]
        guard = pollScope[:pollScope.index("function render(")]

        self.assertNotIn("if (!card) return;", guard)

    def test_each_renderer_guards_its_own_element(self):
        #< one indent level shallower than when this lived inside the template
        self.assertIn("function render(np) {\n    if (!card) return;", self.template)
        self.assertIn("function renderFriends(friends, moreCount) {\n    if (!friendsRow) return;",
                      self.template)


class TestTheChipIsALink(unittest.TestCase):
    """The href itself is the route's, and tested there
    (tests/test_friends_now_playing_api.py). What is asserted here is the half
    that lives in the browser script: that the chip is an anchor at all, and
    that it uses the URL the payload carries rather than a path of its own."""

    def setUp(self):
        scriptPath = os.path.join(os.path.dirname(__file__), "..", "static", "js", "dashboard-page.js")
        with open(scriptPath, encoding="utf-8") as handle:
            self.script = handle.read()

    def test_the_chip_element_is_an_anchor(self):
        self.assertIn("var chip = document.createElement('a');", self.script)

    def test_the_href_comes_from_the_payload(self):
        self.assertIn("chip.href = friend.compareUrl;", self.script)


class TestStripStyling(unittest.TestCase):
    """The one rule the layout depends on, asserted against the stylesheet: a
    chip that can grow reintroduces exactly the stretched-slab problem."""

    def setUp(self):
        cssPath = os.path.join(os.path.dirname(__file__), "..", "static", "css", "style.css")
        with open(cssPath, encoding="utf-8") as handle:
            self.css = handle.read()

    def _block(self, selector):
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(match, f"{selector} missing from style.css")
        return match.group(1)

    def test_a_chip_never_grows(self):
        self.assertIn("flex: 0 1 auto", self._block(".friends-listening-chip"))

    def test_a_chip_is_width_capped(self):
        self.assertIn("max-width", self._block(".friends-listening-chip"))

    def test_the_cap_still_leaves_room_for_the_track_title(self):
        """The cap is a design choice; a cap that eats the title is not.

        At 230px - minus the 30px cover and the 8px gap - one ~190px line had to
        hold the friend's name, a separator AND the track name, so most titles
        arrived as an ellipsis. Pinned as a floor rather than an exact value so
        the number stays tunable, but not back down into that."""
        block = self._block(".friends-listening-chip")

        match = re.search(r"max-width:\s*(\d+)px", block)
        self.assertIsNotNone(match, "the chip cap is not a plain px value")
        self.assertGreaterEqual(int(match.group(1)), CHIP_TITLE_ROOM_FLOOR_PX)

    def test_the_chip_link_does_not_read_as_a_bare_anchor(self):
        """It became an <a> without becoming blue and underlined."""
        block = self._block(".friends-listening-chip")

        self.assertIn("text-decoration: none", block)
        self.assertIn("color: inherit", block)

    def test_the_row_wraps_rather_than_scrolling(self):
        block = self._block(".friends-listening")
        self.assertIn("flex-wrap: wrap", block)
        self.assertNotIn("overflow-x", block)

    def test_long_names_ellipsise_instead_of_widening_the_chip(self):
        block = self._block(".friends-listening-track,\n.friends-listening-artist")
        self.assertIn("text-overflow: ellipsis", block)


if __name__ == "__main__":
    unittest.main()
