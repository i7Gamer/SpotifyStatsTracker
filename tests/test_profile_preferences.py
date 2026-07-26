"""The Preferences section on /profile: default time window, timezone, and
the per-user "hide the tag panel" checkbox (independent of the admin's
instance-wide tags kill switch - see Database/queries/settings.py's
isTagsEnabled and Database/queries/users.py's hide_tags_panel column)."""
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase


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
        })

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
