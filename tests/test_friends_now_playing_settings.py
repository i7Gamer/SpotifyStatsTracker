"""The two switches guarding the dashboard's friends-listening strip.

Live presence is a stronger disclosure than the aggregate comparison that
sharing already allows, so it has its own admin kill switch (separate from
data sharing) and its own per-user opt-out (separate from hiding the tag
panel). Both are plain feature-toggle/preference plumbing; the payload they
gate lives in test_friends_now_playing_api.py.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import Repository
from _app_factory import AppTestCase


class TestAdminKillSwitch(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(Path(":memory:"))
        self.addCleanup(self.repo.connectionManager.close)

    def test_enabled_by_default(self):
        """Absent row = enabled, the same contract as every other toggle."""
        self.assertTrue(self.repo.isFriendsNowPlayingEnabled())

    def test_can_be_turned_off_and_back_on(self):
        self.repo.setFriendsNowPlayingEnabled(False)
        self.assertFalse(self.repo.isFriendsNowPlayingEnabled())

        self.repo.setFriendsNowPlayingEnabled(True)
        self.assertTrue(self.repo.isFriendsNowPlayingEnabled())

    def test_it_is_independent_of_the_data_sharing_switch(self):
        """An admin can keep Compare while turning live presence off."""
        self.repo.setFriendsNowPlayingEnabled(False)

        self.assertTrue(self.repo.isDataSharingEnabled())

    def test_turning_off_data_sharing_does_not_flip_this_one(self):
        self.repo.setDataSharingEnabled(False)

        self.assertTrue(self.repo.isFriendsNowPlayingEnabled())


class TestPerUserOptOut(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(Path(":memory:"))
        self.addCleanup(self.repo.connectionManager.close)
        self.repo.upsertUser("alice", "alice@example.com")

    def test_broadcasting_is_on_by_default(self):
        self.assertFalse(self.repo.getUserSettings("alice")["hide_now_playing"])

    def test_the_preference_round_trips(self):
        self.repo.updateUserSettings("alice", "day", None, hide_tags_panel=False, hide_now_playing=True)

        self.assertTrue(self.repo.getUserSettings("alice")["hide_now_playing"])

    def test_it_is_independent_of_the_tag_panel_preference(self):
        self.repo.updateUserSettings("alice", "day", None, hide_tags_panel=True, hide_now_playing=False)

        settings = self.repo.getUserSettings("alice")
        self.assertTrue(settings["hide_tags_panel"])
        self.assertFalse(settings["hide_now_playing"])

    def test_an_unknown_user_reads_as_broadcasting(self):
        """getUserSettings' no-row fallback must carry the new key too, or
        callers KeyError on a username that vanished mid-request."""
        self.assertFalse(self.repo.getUserSettings("nobody")["hide_now_playing"])

    def test_the_column_add_is_idempotent(self):
        """The migrator re-runs on any boot that retries a failed migration."""
        self.repo.addHideNowPlayingColumnIfMissing()
        self.repo.addHideNowPlayingColumnIfMissing()

        self.assertFalse(self.repo.getUserSettings("alice")["hide_now_playing"])


class FriendsNowPlayingRouteTestCase(AppTestCase):
    EMAIL = "alice@example.com"
    USERNAME = "alice"

    def setUp(self):
        self.dash = self._makeApp()
        self.dash.repo.upsertUser(self.USERNAME, self.EMAIL)
        self.dash.repo.setUserCookies(self.USERNAME, {"sp_dc": "cookie"})

    def _client(self, isAdmin=False):
        userDb = MagicMock()
        userDb.repo = self.dash.repo
        for patcher in (
            patch.object(self.dash, "is_user_logged_in", return_value=True),
            patch.object(self.dash, "get_username_for_email", return_value=self.USERNAME),
            patch.object(self.dash, "get_user_db", return_value=userDb),
            patch.object(self.dash.repo, "isAdmin", return_value=isAdmin),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        client = self.dash.app.test_client()
        with client.session_transaction() as sess:
            sess["email"] = self.EMAIL
            sess["username"] = self.USERNAME
        return client


class TestProfileCheckbox(FriendsNowPlayingRouteTestCase):
    def test_saving_preferences_stores_the_opt_out(self):
        client = self._client()

        client.post("/profile", data={"action": "save_preferences",
                                      "default_dashboard_window": "day",
                                      "timezone": "",
                                      "hide_now_playing": "1"})

        self.assertTrue(self.dash.repo.getUserSettings(self.USERNAME)["hide_now_playing"])

    def test_an_absent_checkbox_clears_the_opt_out(self):
        """Unchecked boxes aren't submitted at all - absence must mean
        "keep broadcasting", not "leave whatever was there"."""
        self.dash.repo.updateUserSettings(self.USERNAME, "day", None, hide_now_playing=True)
        client = self._client()

        client.post("/profile", data={"action": "save_preferences",
                                      "default_dashboard_window": "day", "timezone": ""})

        self.assertFalse(self.dash.repo.getUserSettings(self.USERNAME)["hide_now_playing"])

    def test_saving_does_not_disturb_the_tag_panel_preference(self):
        self.dash.repo.updateUserSettings(self.USERNAME, "day", None, hide_tags_panel=True)
        client = self._client()

        client.post("/profile", data={"action": "save_preferences",
                                      "default_dashboard_window": "day", "timezone": "",
                                      "hide_tags_panel": "1", "hide_now_playing": "1"})

        settings = self.dash.repo.getUserSettings(self.USERNAME)
        self.assertTrue(settings["hide_tags_panel"])
        self.assertTrue(settings["hide_now_playing"])

    def test_the_checkbox_is_hidden_when_the_admin_switch_is_off(self):
        """Nothing to opt out of - the strip can't appear for anyone."""
        self.dash.repo.setFriendsNowPlayingEnabled(False)
        client = self._client()

        body = client.get("/profile").data.decode()

        self.assertNotIn('name="hide_now_playing"', body)

    def test_the_checkbox_is_hidden_when_sharing_is_off(self):
        self.dash.repo.setDataSharingEnabled(False)
        client = self._client()

        body = client.get("/profile").data.decode()

        self.assertNotIn('name="hide_now_playing"', body)

    def test_the_checkbox_shows_when_both_are_on(self):
        client = self._client()

        body = client.get("/profile").data.decode()

        self.assertIn('name="hide_now_playing"', body)


class TestAdminToggleRoute(FriendsNowPlayingRouteTestCase):
    def test_an_admin_can_turn_it_off(self):
        client = self._client(isAdmin=True)

        client.post("/admin/user_settings", data={"data_sharing": "1", "tags": "1"})

        self.assertFalse(self.dash.repo.isFriendsNowPlayingEnabled())

    def test_an_admin_can_turn_it_on(self):
        self.dash.repo.setFriendsNowPlayingEnabled(False)
        client = self._client(isAdmin=True)

        client.post("/admin/user_settings", data={"friends_now_playing": "1"})

        self.assertTrue(self.dash.repo.isFriendsNowPlayingEnabled())

    def test_a_non_admin_cannot_change_it(self):
        client = self._client(isAdmin=False)

        resp = client.post("/admin/user_settings", data={"friends_now_playing": "1"})

        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
