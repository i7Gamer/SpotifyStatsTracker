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
