"""The dashboard markup for the friends-listening strip.

The strip lives INSIDE the Now Playing panel - "what is playing right now" is
one question, and the answer for you and the answer for the people you share
with belong in one card rather than in a card and a separate row below it. It is
populated by the existing now-playing poll. What's pinned here is when the
markup exists at all, where it sits in the panel, and that it doesn't reuse
.summary-card - those are flex: 1 1 0, so a chip built from one would stretch to
the full card width while holding a fraction of its content.
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
    def test_it_sits_inside_the_now_playing_panel(self):
        """Not in a row of its own below the cards: the panel answers "what is
        playing right now", and the friends are the rest of that answer."""
        html = self._dashboardHtml()
        panel = html[html.index('id="nowPlayingPanel"'):html.index("onthisday-card")]

        self.assertIn('id="friendsListening"', panel)

    def test_it_sits_between_your_own_track_and_the_streak(self):
        """Both halves of "right now" together, with the streak - a statistic
        about the past - kept below them."""
        html = self._dashboardHtml()

        ownTrackIdx = html.index('id="nowPlayingCard"')
        stripIdx = html.index('id="friendsListening"')
        streakIdx = html.index('class="streak-block"')

        self.assertLess(ownTrackIdx, stripIdx)
        self.assertLess(stripIdx, streakIdx)

    def test_it_is_inside_the_filter_independent_live_panel(self):
        """Below the filter form it would read as filtered data, which it isn't."""
        html = self._dashboardHtml()

        self.assertLess(html.index('id="friendsListening"'), html.index('class="hero"'))

    def test_chips_do_not_reuse_the_stretching_summary_card_class(self):
        html = self._dashboardHtml()

        # End the slice at the streak block, the next thing in the panel: the
        # enclosing card IS a .summary-card, so the slice has to cover the
        # friends block alone.
        stripMarkup = html[
            html.index('<div class="friends-listening"'):
            html.index('<div class="streak-block">')
        ]
        self.assertNotIn("summary-card", stripMarkup)

    def test_it_is_titled_like_the_panel_s_other_blocks(self):
        """Inside the card it is a section of one, not a labelled strip - so it
        carries an h2 like "Now playing" and "Listening streak" above it."""
        html = self._dashboardHtml()
        stripMarkup = html[
            html.index('<div class="friends-listening"'):
            html.index('<div class="streak-block">')
        ]

        self.assertIn("<h2>Friends listening</h2>", stripMarkup)

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


class TestTheChipIsThreeLinks(unittest.TestCase):
    """Every href itself is the route's, and tested there
    (tests/test_friends_now_playing_api.py). What is asserted here is the half
    that lives in the browser script: that the chip names three things and
    links each one separately, and that every URL comes from the payload rather
    than being a path this file builds.

    The chip used to be ONE <a> to /compare wrapping everything, which made the
    track title a link to a page about the friend. A nested <a> is also invalid
    markup the parser would unnest, so this cannot be fixed by adding links
    inside the old chip - the container has to stop being one."""

    def setUp(self):
        scriptPath = os.path.join(os.path.dirname(__file__), "..", "static", "js", "dashboard-page.js")
        with open(scriptPath, encoding="utf-8") as handle:
            self.script = handle.read()

    def _renderFriends(self):
        body = self.script[self.script.index("function renderFriends("):]
        return body[:body.index("\n  var pollHandle")]

    def test_the_chip_container_is_not_itself_a_link(self):
        self.assertIn("var chip = document.createElement('div');", self.script)
        self.assertNotIn("chip.href", self.script)

    def test_the_friend_s_name_links_to_comparing_with_them(self):
        self.assertIn("friend.compareUrl", self._renderFriends())

    def test_the_track_title_links_to_the_track(self):
        self.assertIn("friendsChipLink(friend.name, friend.trackUrl", self._renderFriends())

    def test_each_artist_links_to_that_artist(self):
        self.assertIn("each.name, each.url", self._renderFriends())

    def test_the_cover_goes_where_the_track_title_does(self):
        """It is the track's own artwork, so clicking it should not do
        something different from clicking its name."""
        rendered = self._renderFriends()

        self.assertIn("friendsChipAnchor(friend.trackUrl, 'friends-listening-cover-link')",
                      rendered)
        self.assertIn("coverLink.appendChild(cover)", rendered)

    def test_the_cover_link_is_not_a_second_tab_stop_to_the_same_place(self):
        """The title beside it already links there, and four chips would
        otherwise cost eight tab stops to reach four destinations. Both halves
        are needed: aria-hidden alone would leave a focusable element hidden
        from the screen reader reading it."""
        rendered = self._renderFriends()

        self.assertIn("coverLink.setAttribute('aria-hidden', 'true')", rendered)
        self.assertIn("coverLink.tabIndex = -1", rendered)

    def test_no_url_is_built_in_the_browser(self):
        """Every internal path goes through url_for server-side; a literal
        '/song/' here would be unroutable and silently wrong after a rename."""
        rendered = self._renderFriends()

        for path in ("'/song/", "'/artist/", "'/compare"):
            with self.subTest(path=path):
                self.assertNotIn(path, rendered)

    def test_an_unchanged_poll_leaves_the_chips_alone(self):
        """The 15-second poll used to rebuild every chip unconditionally. That
        was invisible while they were inert divs; as links it swallows a click
        whose mousedown lands just before the rebuild, and drops keyboard focus
        to <body> mid-read.

        What the signature COVERS is asserted in plain node
        (tests/test_dashboard_page.js); this is the half node cannot see - that
        renderFriends actually returns early on it."""
        self.assertIn("var signature = friendsStripSignature(friends, moreCount);", self.script)
        self.assertIn("if (signature === renderedFriends) return;", self.script)


class TestTheStripTellsThePanelItIsShowing(unittest.TestCase):
    """Now that the strip is a block inside the card, the card has to know
    whether it is showing - that's what draws the divider above the streak when
    the viewer isn't playing anything themselves. Asserted against the source
    for the same reason the poll's guards are: the class is set from a fetch
    callback no render test can reach."""

    def setUp(self):
        scriptPath = os.path.join(os.path.dirname(__file__), "..", "static", "js", "dashboard-page.js")
        with open(scriptPath, encoding="utf-8") as handle:
            self.script = handle.read()

    def test_the_panel_is_marked_while_friends_are_listening(self):
        self.assertIn("panel.classList.add('has-friends')", self.script)

    def test_the_mark_is_dropped_when_nobody_is(self):
        self.assertIn("panel.classList.remove('has-friends')", self.script)

    def test_the_mark_is_set_before_the_unchanged_poll_early_return(self):
        """Below it, the very first poll of a page load would set the class and
        every later one would skip it - fine - but a chips-unchanged poll
        arriving first after a re-render would leave the divider off."""
        renderFriends = self.script[self.script.index("function renderFriends("):]
        renderFriends = renderFriends[:renderFriends.index("\n  var pollHandle")]

        self.assertLess(renderFriends.index("panel.classList.add('has-friends')"),
                        renderFriends.index("if (signature === renderedFriends) return;"))


class TestStripStyling(unittest.TestCase):
    """The one rule the layout depends on, asserted against the stylesheet: a
    chip that can grow reintroduces exactly the stretched-slab problem."""

    def setUp(self):
        cssPath = os.path.join(os.path.dirname(__file__), "..", "static", "css", "style.css")
        with open(cssPath, encoding="utf-8") as handle:
            self.css = handle.read()

    def _block(self, selector):
        """The declarations of the rule whose selector LIST includes `selector`.

        Split on commas rather than matching "<selector> {" directly: the panel's
        dividers share one rule, so most of their selectors are followed by a
        comma and a direct lookup finds nothing. Comments come out first - this
        stylesheet's carry the reasoning, commas and all, and one sits directly
        above nearly every rule here.
        """
        rules = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)
        for match in re.finditer(r"([^{}]*)\{([^{}]*)\}", rules):
            if selector in [part.strip() for part in match.group(1).split(",")]:
                return match.group(2)
        self.fail(f"{selector} missing from style.css")

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

    def test_the_chip_s_links_do_not_read_as_bare_anchors(self):
        """Three links per chip without three blue underlined runs of text."""
        block = self._block(".friends-listening-link")

        self.assertIn("text-decoration: none", block)
        self.assertIn("color: inherit", block)

    def test_the_cover_link_does_not_squash_the_artwork(self):
        """Wrapping the img in an <a> made the ANCHOR the flex item, so the
        cover's own flex-shrink no longer reaches the thing that can shrink."""
        block = self._block(".friends-listening-cover-link")

        self.assertIn("flex-shrink: 0", block)

    def test_each_link_highlights_on_its_own_hover(self):
        """The whole-chip rule this replaced lit the track title while the
        cursor was over the friend's name - which goes somewhere else."""
        self.assertIn("color: var(--accent)", self._block(".friends-listening-link:hover"))

    def test_the_chips_wrap_rather_than_scrolling(self):
        block = self._block(".friends-listening-chips")
        self.assertIn("flex-wrap: wrap", block)
        self.assertNotIn("overflow-x", block)

    def test_the_block_stacks_its_heading_above_the_chips(self):
        """Inside the card the label is a heading with the chips under it, not a
        label sitting to their left - that's what the other blocks look like."""
        self.assertIn("display: grid", self._block(".friends-listening"))

    def test_long_names_ellipsise_instead_of_widening_the_chip(self):
        block = self._block(".friends-listening-track")
        self.assertIn("text-overflow: ellipsis", block)

    def test_a_divider_separates_it_from_your_own_track_above(self):
        """The blocks are hidden with display:none, which :first-child cannot
        see - so each divider is keyed off the class the poll sets."""
        self.assertIn("border-top", self._block(".now-playing-card.has-now-playing .friends-listening"))

    def test_the_streak_below_is_divided_off_when_only_friends_are_playing(self):
        """Its old rule fired on has-now-playing alone, so a viewer who wasn't
        playing got the friends block welded to the streak."""
        self.assertIn("border-top", self._block(".now-playing-card.has-friends .streak-block"))


if __name__ == "__main__":
    unittest.main()
