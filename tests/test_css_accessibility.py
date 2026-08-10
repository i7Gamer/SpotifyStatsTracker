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
                         ".track-summary-grid-3", ".dashboard-summary"):
            with self.subTest(selector=selector):
                self.assertIn("gap: 18px", self._block(selector))

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

    def test_both_paths_that_change_the_menu_update_the_attribute(self):
        """Two of them: the button itself, and a link click that closes the
        menu without going through the button's handler. A fix landing in only
        the first leaves the attribute stuck at "true" after navigating."""
        chrome = _readFile(_LAYOUT_CHROME_PATH)
        navBlock = chrome[chrome.index("const navToggle"):]

        self.assertIn("aria-expanded", navBlock)
        #< "syncNavExpanded();" is a CALL - the declaration ends in " {"
        self.assertEqual(navBlock.count("syncNavExpanded();"), 2,
                         "both the toggle click and the link click must re-sync it")

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
