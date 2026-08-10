"""Static guards for a couple of CSS-level accessibility fixes (2026-07-24 review).

These assert against the stylesheet text directly - cheap regression guards that
don't need a browser: the nav dropdowns must open on keyboard focus, not only on
hover, and the logout control (now a POST form button) must be styled like its
sibling links.

Since extended to the markup/script pair behind the mobile nav toggle, which is
the same kind of fix one level up from the stylesheet.
"""
import os
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CSS_PATH = os.path.join(_ROOT, "static", "css", "style.css")
_LAYOUT_PATH = os.path.join(_ROOT, "templates", "layout.html")
_LAYOUT_CHROME_PATH = os.path.join(_ROOT, "static", "js", "layout-chrome.js")


def _readFile(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestNavKeyboardCss(unittest.TestCase):
    def setUp(self):
        with open(_CSS_PATH, encoding="utf-8") as fh:
            self.css = fh.read()

    def test_dropdown_opens_on_focus_within(self):
        # Without this, submenu links are display:none and unreachable by Tab.
        self.assertIn(".nav-item-dropdown:focus-within .dropdown-content", self.css)

    def test_logout_form_button_is_styled_like_dropdown_links(self):
        self.assertIn(".dropdown-content .dropdown-logout-form button", self.css)

    def test_setting_hint_summary_centered(self):
        # Verify .setting-hint summary includes padding-right to shift the italic 'i' symbol left into optical center
        self.assertIn(".setting-hint summary", self.css)
        self.assertIn("padding-right: 1px;", self.css)


class TestOverlaySurfaceLegibility(unittest.TestCase):
    """The 2026-08-10 overlay pass: surfaces that float over CONTENT are not
    allowed to be glass.

    --glass-bg (0.65 alpha) reads fine over the fixed body gradient, which is
    what the topbar and the desktop dropdowns sit on. The mobile nav drawer and
    the admin setting-hint popovers sit over a scrolling page instead - over a
    light album cover a 0.65 panel composites to roughly #545454, which drops
    --muted text to ~2.9:1, under the 4.5:1 floor. The setting-hint was worse
    still: 0.45 alpha and no backdrop-filter at all, so the checkbox labels
    behind it showed through the tooltip as text-over-text.

    --overlay-bg is the near-opaque counterpart. The blur stays (it is the
    design language) but it is no longer load-bearing for legibility, which
    matters because some Android WebViews drop backdrop-filter entirely and
    would otherwise be left with the raw alpha."""

    #< the point of the token: opaque enough that what is behind it cannot be
    #  read through it. 0.97 over white composites to ~#181818 (16.4:1 against
    #  --text), which is the same as opaque for every practical purpose.
    _MIN_OVERLAY_ALPHA = 0.95
    #< WCAG 2.5.5 (AAA), the floor already used by .scroll-to-top and the
    #  pager. Right for a control with room around it, which a full-width
    #  drawer row has.
    _ROOMY_TOUCH_TARGET = "44px"
    #< WCAG 2.5.8 (AA). Right for a control that does NOT have room: the
    #  setting-hint sits 8px from its label (.admin-check-label) and 6.4px in
    #  the settings loop, so a 44px hit area overhangs the neighbour by 13px
    #  and eats clicks meant for the checkbox. 24px reaches 3px and clears
    #  both gaps - measured, not assumed.
    _TIGHT_TOUCH_TARGET = "24px"

    def setUp(self):
        self.css = _readFile(_CSS_PATH)

    def _block(self, selector, source=None):
        """The declarations of the FIRST rule for `selector`, comments stripped.

        Brace-matched, so a rule nested inside a media query slices out too,
        and the selector may head a comma-separated list (`.dropdown-content a,
        .dropdown-content ... button {`). Comments go because these tests
        assert on what a declaration says, and the comments beside them
        legitimately name the values they replaced."""
        source = self.css if source is None else source
        import re
        match = re.search(re.escape(selector) + r"\s*(?:,[^{}]*)?\{", source)
        self.assertIsNotNone(match, f"{selector} missing from style.css")
        depth, start = 1, match.end()
        for index in range(start, len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    return re.sub(r"/\*.*?\*/", "", source[start:index], flags=re.DOTALL)
        self.fail(f"unbalanced braces after {selector}")

    def _mobileNavBlock(self):
        """Everything at the <=1024px breakpoint, joined. There are two such
        media queries and the drawer spans both: .nav-links lives in the first,
        the .dropdown-content flattening in the second."""
        import re
        blocks = []
        for match in re.finditer(r"@media \(max-width: 1024px\)\s*\{", self.css):
            depth, start = 1, match.end()
            for index in range(start, len(self.css)):
                if self.css[index] == "{":
                    depth += 1
                elif self.css[index] == "}":
                    depth -= 1
                    if depth == 0:
                        blocks.append(self.css[start:index])
                        break
        self.assertTrue(blocks, "no <=1024px media query in style.css")
        return "\n".join(blocks)

    def test_the_overlay_token_is_effectively_opaque(self):
        import re
        match = re.search(r"--overlay-bg:\s*rgba\([^)]*,\s*([\d.]+)\s*\)", self.css)
        self.assertIsNotNone(match, "--overlay-bg missing or not an rgba()")
        self.assertGreaterEqual(float(match.group(1)), self._MIN_OVERLAY_ALPHA)

    def test_the_mobile_drawer_is_an_overlay_surface_not_a_glass_one(self):
        block = self._block(".nav-links", self._mobileNavBlock())
        self.assertIn("background: var(--overlay-bg)", block)
        self.assertNotIn("var(--glass-bg)", block)

    def test_the_drawer_keeps_the_blur(self):
        """Both prefixes: -webkit- is still what iOS Safari reads."""
        block = self._block(".nav-links", self._mobileNavBlock())
        self.assertIn("backdrop-filter: blur", block)
        self.assertIn("-webkit-backdrop-filter: blur", block)

    def test_the_setting_hint_popover_is_an_overlay_surface(self):
        block = self._block(".setting-hint p")
        self.assertIn("background: var(--overlay-bg)", block)
        self.assertNotIn("var(--surface)", block)
        self.assertIn("backdrop-filter: blur", block)

    def test_the_setting_hint_popover_uses_full_strength_text(self):
        """0.8rem is small text, which is exactly where the 4.5:1 minimum
        stops being generous."""
        self.assertIn("color: var(--text)", self._block(".setting-hint p"))

    def test_the_drawer_runs_to_the_bottom_of_the_viewport(self):
        block = self._block(".nav-links", self._mobileNavBlock())
        self.assertIn("top: 100%", block)
        self.assertIn("--topbar-current-height", block)
        #< dvh, not vh: vh on a phone measures the viewport WITHOUT the
        #  browser's collapsing chrome, so the last item lands under it
        self.assertIn("100dvh", block)
        #< the height is no longer something the .active state grows into
        self.assertNotIn("max-height", block)
        self.assertNotIn("max-height", self._block(".nav-links.active", self._mobileNavBlock()))

    def test_no_rule_hardcodes_the_topbar_height_as_an_offset(self):
        """The literal this replaces. .topbar is flex-wrap and grows a row for
        any of the four badges, so 62px was wrong whenever one was showing -
        and it never agreed with --topbar-height (52px) either."""
        self.assertNotIn("top: 62px", self.css)

    def test_the_drawer_contains_its_own_scrolling(self):
        block = self._block(".nav-links", self._mobileNavBlock())
        self.assertIn("overflow-y: auto", block)
        self.assertIn("overscroll-behavior: contain", block)

    def test_an_open_drawer_locks_the_page_behind_it(self):
        """Scoped INSIDE the media query: opening at 900px and resizing past
        the breakpoint would otherwise leave the page unscrollable with no
        drawer left to close."""
        self.assertIn("overflow: hidden", self._block("body.nav-open", self._mobileNavBlock()))

    def test_every_menu_item_meets_the_touch_target_floor(self):
        """Both selectors: most of this menu (Top Songs/Artists/Albums,
        Charts, Genres, Wrapped, Profile, Import, Admin) is dropdown-content,
        not a top-level .nav-links link."""
        mobile = self._mobileNavBlock()
        for selector in (".nav-links a", ".dropdown-content a"):
            with self.subTest(selector=selector):
                self.assertIn(f"min-height: {self._ROOMY_TOUCH_TARGET}",
                              self._block(selector, mobile))

    def test_menu_items_are_primary_text_not_muted(self):
        """They are the only controls on the screen while the drawer is open;
        --muted reads as "secondary" and is the weaker contrast of the two."""
        mobile = self._mobileNavBlock()
        for selector in (".nav-links a", ".dropdown-content a"):
            with self.subTest(selector=selector):
                self.assertIn("color: var(--text)", self._block(selector, mobile))

    def test_the_hint_toggle_has_a_touchable_hit_area(self):
        """The circle stays 16px so the settings rows keep their alignment; a
        transparent pseudo-element does the touching.

        Sized to the AA target, not the AAA one the drawer rows use. Measured
        in Chrome 148 against both arrangements in admin.html: a 44px hit area
        reaches 13px past the circle, which is more than either gap to the
        neighbouring label (8px / 6.4px), so it silently swallows clicks meant
        for the checkbox. 24px reaches 3px and overlaps neither."""
        block = self._block(".setting-hint summary::before")
        self.assertIn(f"width: {self._TIGHT_TOUCH_TARGET}", block)
        self.assertIn(f"height: {self._TIGHT_TOUCH_TARGET}", block)
        self.assertNotIn(self._ROOMY_TOUCH_TARGET, block)


class TestPageRhythmAndInlineHero(unittest.TestCase):
    """The 2026-08-10 spacing polish, pinned.

    One vertical rhythm: the page used to stack its blocks (hero, filter card,
    lists, summary grids) 24px apart while the cards INSIDE a list sat 18px
    apart - close enough to look like a mistake rather than a hierarchy. The
    lower value is the standard now, everywhere a block or card ends.

    And the Top Songs/Artists/Albums hero holds its one-sentence description
    beside the heading (desktop/tablet) instead of under it, in a shorter
    pane; phones stack it back underneath by media query, not by luck of
    where the sentence happens to wrap."""

    _RHYTHM_SELECTORS = (
        ".hero", ".filter-section", ".import-card", ".biography-card",
        ".track-summary-grid", ".track-summary-grid-3",
        ".track-list + .track-list",
        #< the 2026-08-10 sweep's leftovers: page-level panels that stack
        #  exactly like .hero does but were not in the first pass.
        #  .dashboard-live IS the dashboard's top panel (its .hero stand-in),
        #  and .chart-card is the panel above the list on /charts and on both
        #  detail pages.
        ".dashboard-live", ".chart-card",
    )

    def setUp(self):
        self.css = _readFile(_CSS_PATH)

    def _block(self, selector):
        import re
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(match, f"{selector} missing from style.css")
        return match.group(1)

    def test_every_stacked_block_shares_the_18px_rhythm(self):
        for selector in self._RHYTHM_SELECTORS:
            with self.subTest(selector=selector):
                block = self._block(selector)
                margin = "margin-top" if "+" in selector else "margin-bottom"
                self.assertIn(f"{margin}: 18px", block)
                #< the margin specifically - horizontal PADDING may still say
                #  24px (.hero pads 28px 24px), and that is not the rhythm
                self.assertNotIn(f"{margin}: 24px", block)

    def test_the_between_cards_gap_is_the_same_18px(self):
        for selector in (".track-list", ".track-summary-grid",
                         ".track-summary-grid-3", ".dashboard-summary",
                         #< the row that actually holds the dashboard's live
                         #  cards. Nested inside .dashboard-summary, which the
                         #  first pass moved to 18px while this one - the gap
                         #  you can see - stayed at 20px.
                         ".dashboard-summary-cards"):
            with self.subTest(selector=selector):
                self.assertIn("gap: 18px", self._block(selector))

    def test_the_gap_under_the_nav_bar_is_the_same_rhythm(self):
        """.page's top padding IS the topbar -> hero distance: .topbar is
        sticky with a border, <main class="page"> follows it, and .hero has no
        margin-top. It sat at 24px while every block below it stacked at 18px,
        so the first gap on every page was the odd one out.

        The bottom (40px) is not the rhythm - it is the run-out under the last
        block, and the scroll-to-top button parks in it."""
        self.assertIn("padding: 18px 16px 40px", self._block(".page"))

    def test_the_top_pages_hero_is_the_inline_variant(self):
        for template in ("top_songs.html", "top_artists.html", "top_albums.html"):
            with self.subTest(template=template):
                markup = _readFile(os.path.join(_ROOT, "templates", template))
                self.assertIn('class="hero hero-inline"', markup)

    def test_the_inline_hero_rides_beside_the_heading_and_shrinks_the_pane(self):
        self.assertIn("display: flex", self._block(".hero-inline .hero-content"))
        self.assertIn("align-items: baseline", self._block(".hero-inline .hero-content"))
        self.assertIn("padding: 18px 24px", self._block(".hero-inline"))
        self.assertIn("margin-top: 0", self._block(".hero-inline .hero-content p"))

    def test_phones_stack_the_description_back_underneath(self):
        """By media query, not by wrap: a layout that depends on how long the
        sentence happens to be isn't a layout."""
        mobile = self.css[self.css.index(".hero-inline"):]
        mobile = mobile[mobile.index("@media (max-width: 768px)"):]
        blockEnd = mobile.index(".hero-inline .hero-content p")
        self.assertIn("display: block", mobile[:blockEnd])
        self.assertIn("margin-top: 12px", mobile[blockEnd:blockEnd + 200])


class TestButtonNormalization(unittest.TestCase):
    """The 2026-08-10 button sweep: two bases, one small size, no inline
    paddings. Nine per-instance paddings, two phantom classes (.button-small
    had no rule; .form-control never had one anywhere) and a third,
    inline-built "primary" look on Wrapped's Export/Share all collapsed into
    (primary-button | button) x (button-small?) x (is-danger | is-neutral |
    button-danger), plus .button-icon carrying the icon buttons' flex+gap."""

    def setUp(self):
        self.css = _readFile(_CSS_PATH)

    def _block(self, selector):
        import re
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(match, f"{selector} missing from style.css")
        return match.group(1)

    def test_the_small_modifier_is_real_and_single_sized(self):
        block = self._block(".button-small")
        self.assertIn("padding: 4px 12px", block)
        self.assertIn("font-size: 0.85rem", block)

    def test_the_modifiers_come_after_both_bases(self):
        """Equal specificity means source order decides: a .button-small
        declared before .primary-button would silently lose the padding fight
        and size nothing at all."""
        smallAt = self.css.index(".button-small {")
        iconAt = self.css.index(".button-icon {")
        for base in (".primary-button {", ".button {"):
            self.assertLess(self.css.index(base), smallAt, base)
            self.assertLess(self.css.index(base), iconAt, base)

    def test_the_folded_classes_are_gone(self):
        """Each was a one-off spelling of button-small or is-neutral."""
        for selector in (".admin-save-btn", ".share-action-button",
                         ".profile-logout-button"):
            self.assertNotIn(selector, self.css)

    def test_no_button_carries_an_inline_padding(self):
        """The drift guard: a button's size is its class now. Any new
        style="...padding..." on a button-classed element restarts the
        nine-sizes problem this sweep ended. Lookaheads, because class= and
        style= appear in either order."""
        import glob
        import re
        buttonWithInlinePadding = re.compile(
            r'<(?:button|a)\b(?=[^>]*class="[^"]*button)(?=[^>]*style="[^"]*padding)')
        offenders = []
        for path in glob.glob(os.path.join(_ROOT, "templates", "*.html")):
            if buttonWithInlinePadding.search(_readFile(path)):
                offenders.append(os.path.basename(path))
        self.assertEqual(offenders, [])

    def test_the_phantom_form_control_class_is_gone(self):
        import glob
        for path in glob.glob(os.path.join(_ROOT, "templates", "*.html")):
            self.assertNotIn("form-control", _readFile(path),
                             f"{os.path.basename(path)} references a class no stylesheet defines")


class TestMobileNavToggleAnnouncesItsState(unittest.TestCase):
    """Opening the mobile menu only flips CSS classes, which a screen reader
    cannot see. `.artist-toggle` and the play-embed button both carry
    aria-expanded already; this one was the remaining disclosure control that
    did not, so the menu opened and closed with nothing announced."""

    def test_the_button_starts_collapsed_and_names_what_it_controls(self):
        layout = _readFile(_LAYOUT_PATH)
        navToggle = layout[layout.index('id="nav-toggle"'):]
        navToggle = navToggle[:navToggle.index(">")]

        self.assertIn('aria-expanded="false"', navToggle,
                      "the menu starts closed, so the initial state must say so")
        self.assertIn('aria-controls="nav-menu"', navToggle)

    def test_one_writer_owns_the_menu_state_so_the_attribute_cannot_drift(self):
        """Was: count syncNavExpanded() and require one call per path. The
        paths are four now (button, link, Escape, tap-outside) and counting
        them is the weaker guard - what matters is that no path flips the
        class on its own. setNavOpen is the single writer and it always
        re-syncs, so a fifth path cannot forget to."""
        chrome = _readFile(_LAYOUT_CHROME_PATH)
        navBlock = chrome[chrome.index("const navToggle"):]

        self.assertIn("aria-expanded", navBlock)
        #< "syncNavExpanded();" is a CALL - the declaration ends in " {"
        self.assertEqual(navBlock.count("syncNavExpanded();"), 1,
                         "more than one call means a path is flipping the class itself")
        for stray in ("navMenu.classList.add", "navMenu.classList.remove",
                      "navMenu.classList.toggle"):
            with self.subTest(stray=stray):
                self.assertNotIn(stray, navBlock, f"{stray} bypasses setNavOpen")
        #< the declaration plus the four paths
        self.assertGreaterEqual(navBlock.count("setNavOpen("), 5)

class TestAdminUtilityClasses(unittest.TestCase):
    def setUp(self):
        with open(_CSS_PATH, encoding="utf-8") as fh:
            self.css = fh.read()

    def test_admin_worker_label_class_exists(self):
        self.assertIn('.admin-worker-label', self.css)

    def test_admin_worker_row_class_exists(self):
        self.assertIn('.admin-worker-row', self.css)

    def test_admin_number_input_class_exists(self):
        self.assertIn('.admin-number-input', self.css)

    def test_admin_check_label_class_exists(self):
        self.assertIn('.admin-check-label', self.css)

    def test_admin_form_row_class_exists(self):
        self.assertIn('.admin-form-row', self.css)

    def test_admin_card_grid_class_exists(self):
        self.assertIn('.admin-card-grid', self.css)

    def test_admin_text_input_class_exists(self):
        self.assertIn('.admin-text-input', self.css)

    def test_admin_status_table_class_exists(self):
        self.assertIn('.admin-status-table', self.css)

    def test_admin_select_input_width(self):
        self.assertIn('.admin-select-input', self.css)
        select_block = self.css.split('.admin-select-input {')[1].split('}')[0]
        self.assertIn('width: 9rem;', select_block)
        self.assertIn('max-width: 100%;', select_block)



if __name__ == "__main__":
    unittest.main()
