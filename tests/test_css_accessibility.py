"""Static guards for a couple of CSS-level accessibility fixes (2026-07-24 review).

These assert against the stylesheet text directly - cheap regression guards that
don't need a browser: the nav dropdowns must open on keyboard focus, not only on
hover, and the logout control (now a POST form button) must be styled like its
sibling links.
"""
import os
import unittest

_CSS_PATH = os.path.join(os.path.dirname(__file__), "..", "static", "css", "style.css")


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


if __name__ == "__main__":
    unittest.main()
