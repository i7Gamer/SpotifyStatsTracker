"""The Preferences section on /profile: the two default time windows, timezone,
and the per-user "hide the tag panel" checkbox (independent of the admin's
instance-wide tags kill switch - see Database/queries/settings.py's
isTagsEnabled and Database/queries/users.py's hide_tags_panel column)."""
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase
from config import TOP_LIST_DEFAULT_WINDOW


class ProfilePreferencesTestCase(AppTestCase):
    def setUp(self):
        self.dash = self._makeApp()

    def _loginAs(self, username, email):
        self.dash.repo.upsertUser(username, email)
        db = MagicMock()
        db.repo = self.dash.repo
        db.getUserSpotifyCredentials.return_value = {}
        for patcher in (
            patch.object(self.dash, 'is_user_logged_in', return_value=True),
            patch.object(self.dash, 'get_username_for_email', return_value=username),
            patch.object(self.dash, 'get_user_db', return_value=db),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        client = self.dash.app.test_client()
        with client.session_transaction() as sess:
            sess['email'] = email
            sess['username'] = username
        return client


class TestDefaultTimeWindows(ProfilePreferencesTestCase):
    """Two separate windows, because the pages they drive disagree about what a
    useful default is: the Dashboard/Insights/Compare views answer "what have I
    been listening to lately", while the Top pages are a career ranking and have
    always opened on All Time."""

    def test_each_select_names_the_pages_it_drives(self):
        client = self._loginAs("alice", "alice@example.com")

        body = client.get("/profile").get_data(as_text=True)

        self.assertIn("Default Time Window (Dashboard, Insights, Compare)", body)
        self.assertIn("Default Time Window (Top Lists)", body)

    def test_top_list_window_defaults_to_all_time(self):
        client = self._loginAs("alice", "alice@example.com")

        body = client.get("/profile").get_data(as_text=True)

        self.assertIn(f'<option value="{TOP_LIST_DEFAULT_WINDOW}" selected>All Time</option>', body)

    def test_save_preferences_persists_the_top_list_window(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile", data={
            "action": "save_preferences",
            "default_dashboard_window": "week",
            "default_top_list_window": "year",
            "timezone": "",
        }, follow_redirects=True)   #< the action redirects; see test_profile_prg.py

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.dash.repo.getUserSettings("alice")["default_top_list_window"], "year")

    def test_saved_top_list_window_renders_selected(self):
        client = self._loginAs("alice", "alice@example.com")
        client.post("/profile", data={
            "action": "save_preferences",
            "default_dashboard_window": "week",
            "default_top_list_window": "month",
            "timezone": "",
        })

        body = client.get("/profile").get_data(as_text=True)

        self.assertIn('<option value="month" selected>Last Month</option>', body)

    def test_the_two_windows_are_stored_independently(self):
        """One control must not write the other's column - they are separate
        settings sharing a vocabulary, not two views of one value."""
        client = self._loginAs("alice", "alice@example.com")

        client.post("/profile", data={
            "action": "save_preferences",
            "default_dashboard_window": "week",
            "default_top_list_window": "5years",
            "timezone": "",
        })

        settings = self.dash.repo.getUserSettings("alice")
        self.assertEqual(settings["default_dashboard_window"], "week")
        self.assertEqual(settings["default_top_list_window"], "5years")


class TestHideTagsPanelCheckbox(ProfilePreferencesTestCase):
    def test_unchecked_by_default(self):
        client = self._loginAs("alice", "alice@example.com")
        resp = client.get("/profile")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'name="hide_tags_panel"', resp.data)
        self.assertNotIn(b'name="hide_tags_panel" value="1" checked', resp.data)

    def test_save_preferences_persists_the_checkbox(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile", data={
            "action": "save_preferences",
            "default_dashboard_window": "week",
            "timezone": "",
            "hide_tags_panel": "1",
        }, follow_redirects=True)   #< the action redirects; see test_profile_prg.py

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self.dash.repo.getHideTagsPanel("alice"))

    def test_checkbox_renders_checked_after_being_saved(self):
        client = self._loginAs("alice", "alice@example.com")
        client.post("/profile", data={
            "action": "save_preferences",
            "default_dashboard_window": "week",
            "timezone": "",
            "hide_tags_panel": "1",
        })

        resp = client.get("/profile")

        self.assertIn(b'name="hide_tags_panel" value="1" checked', resp.data)

    def test_unchecking_clears_the_preference(self):
        """An unchecked checkbox isn't submitted at all - absence must clear
        a previously-saved True, not leave it untouched.

        The rendered form carries hide_tags_panel_present alongside the box,
        which is how the handler tells "unchecked" from "the control was never
        offered" (it is gated on the admin tagging switch)."""
        client = self._loginAs("alice", "alice@example.com")
        client.post("/profile", data={
            "action": "save_preferences",
            "default_dashboard_window": "week",
            "timezone": "",
            "hide_tags_panel": "1",
        })
        self.assertTrue(self.dash.repo.getHideTagsPanel("alice"))

        client.post("/profile", data={
            "action": "save_preferences",
            "default_dashboard_window": "week",
            "timezone": "",
            "hide_tags_panel_present": "1",
        })

        self.assertFalse(self.dash.repo.getHideTagsPanel("alice"))

    def test_hidden_when_admin_disables_tags_instance_wide(self):
        self.dash.repo.setTagsEnabled(False)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/profile")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'name="hide_tags_panel"', resp.data)


if __name__ == "__main__":
    import unittest
    unittest.main()
