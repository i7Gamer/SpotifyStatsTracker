"""The Preferences section on /profile: the two default time windows, timezone,
and the per-user "hide the tag panel" checkbox (independent of the admin's
instance-wide tags kill switch - see Database/queries/settings.py's
isTagsEnabled and Database/queries/users.py's hide_tags_panel column)."""
import sys
import os
from unittest.mock import patch, MagicMock
from urllib.parse import unquote_plus

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
        self.db = db
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


class TestIntervalWindowValidation(ProfilePreferencesTestCase):
    """save_preferences validates both window selects against
    dashboard.date_ranges.SETTABLE_INTERVALS before writing anything - a
    bogus value can only reach this form via a hand-crafted POST (the
    template only ever offers the seven real options), but honouring it
    would store an interval _resolveIntervalParam's absentDefault path
    would then have to reason about forever (X3, 2026-09-02 review)."""

    def test_a_bogus_dashboard_window_is_rejected(self):
        client = self._loginAs("alice", "alice@example.com")
        before = self.dash.repo.getUserSettings("alice")["default_dashboard_window"]

        resp = client.post("/profile", data={
            "action": "save_preferences",
            "default_dashboard_window": "bogus",
            "default_top_list_window": "year",
            "timezone": "",
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Invalid time window selected.", resp.data)
        self.assertEqual(self.dash.repo.getUserSettings("alice")["default_dashboard_window"], before)

    def test_a_bogus_top_list_window_is_rejected(self):
        client = self._loginAs("alice", "alice@example.com")
        before = self.dash.repo.getUserSettings("alice")["default_top_list_window"]

        resp = client.post("/profile", data={
            "action": "save_preferences",
            "default_dashboard_window": "week",
            "default_top_list_window": "bogus",
            "timezone": "",
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Invalid time window selected.", resp.data)
        self.assertEqual(self.dash.repo.getUserSettings("alice")["default_top_list_window"], before)

    def test_neither_bogus_value_touches_the_other_setting(self):
        """A rejected save must not write EITHER column - not the bogus one,
        and not the valid one submitted alongside it."""
        client = self._loginAs("alice", "alice@example.com")
        beforeTopList = self.dash.repo.getUserSettings("alice")["default_top_list_window"]

        client.post("/profile", data={
            "action": "save_preferences",
            "default_dashboard_window": "bogus",
            "default_top_list_window": "year",
            "timezone": "",
        })

        self.assertEqual(self.dash.repo.getUserSettings("alice")["default_top_list_window"], beforeTopList)

    def test_two_valid_values_still_save(self):
        """The validation must not reject what the form actually offers."""
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile", data={
            "action": "save_preferences",
            "default_dashboard_window": "week",
            "default_top_list_window": "year",
            "timezone": "",
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        settings = self.dash.repo.getUserSettings("alice")
        self.assertEqual(settings["default_dashboard_window"], "week")
        self.assertEqual(settings["default_top_list_window"], "year")


class TestTimezoneValidation(ProfilePreferencesTestCase):
    """save_preferences stored `timezone` as received - a bad value (only
    reachable via a hand-crafted POST; profile.html's <select> offers 14 fixed
    names) was "saved successfully" while Database.refreshSettings's
    `except Exception` swallowed the ZoneInfo construction failure and fell
    back to the instance zone, silently. Same standard as the neighbouring
    SETTABLE_INTERVALS guard (X3, 2026-09-02 review): reject before writing
    anything, next to that check (2026-09-04 review, F-A-3)."""

    def test_an_unconstructable_timezone_is_rejected(self):
        client = self._loginAs("alice", "alice@example.com")
        before = self.dash.repo.getUserSettings("alice")["timezone"]

        resp = client.post("/profile", data={
            "action": "save_preferences",
            "default_dashboard_window": "week",
            "default_top_list_window": "year",
            "timezone": "Mars/Olympus",
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Invalid timezone.", resp.data)
        self.assertEqual(self.dash.repo.getUserSettings("alice")["timezone"], before)

    def test_an_unconstructable_timezone_drops_no_wrapped_cache(self):
        """updateUserSettings drops every cached Wrapped year when timezone
        actually changes (see its comment) - a rejected save must never reach
        that write, or a crafted POST could invalidate every user's Wrapped
        cache for nothing."""
        client = self._loginAs("alice", "alice@example.com")

        with patch.object(self.dash.repo, "deleteAllUserWrapped") as mocked:
            client.post("/profile", data={
                "action": "save_preferences",
                "default_dashboard_window": "week",
                "default_top_list_window": "year",
                "timezone": "Mars/Olympus",
            }, follow_redirects=True)

        mocked.assert_not_called()

    def test_a_valid_timezone_still_saves(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile", data={
            "action": "save_preferences",
            "default_dashboard_window": "week",
            "default_top_list_window": "year",
            "timezone": "Europe/Berlin",
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"Invalid timezone.", resp.data)
        self.assertEqual(self.dash.repo.getUserSettings("alice")["timezone"], "Europe/Berlin")

    def test_an_empty_timezone_still_means_leave_it_unset(self):
        """"" -> None is the existing, deliberate meaning (see the route) -
        the new guard must not treat an empty string as unconstructable."""
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/profile", data={
            "action": "save_preferences",
            "default_dashboard_window": "week",
            "default_top_list_window": "year",
            "timezone": "",
        }, follow_redirects=True)

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"Invalid timezone.", resp.data)
        self.assertIsNone(self.dash.repo.getUserSettings("alice")["timezone"])


class TestThemeInitialiserPlacement(ProfilePreferencesTestCase):
    """ProfilePage.navigate() swaps only the SIBLINGS between .profile-subnav
    and .profile-logout-row, and _runInlineScripts re-runs only the inline
    scripts INSIDE those siblings (cur.querySelectorAll('script') finds
    descendants, never a bare sibling script and never anything outside the
    region). The theme-selector initialiser used to render in a profileScripts
    block OUTSIDE .profile-card, so AJAX-navigating to Account inserted a
    fresh #theme-selector with no change listener and no synced value - theme
    switching silently did nothing until a full reload. The revival machinery
    in profile-page.js even names this initialiser as its purpose; its own
    tests exercise a synthetic DOM, so only this template contract pins the
    real placement."""

    def test_the_theme_initialiser_is_a_descendant_of_a_swapped_sibling(self):
        from bs4 import BeautifulSoup

        client = self._loginAs("alice", "alice@example.com")
        html = client.get("/profile").get_data(as_text=True)
        soup = BeautifulSoup(html, "html.parser")

        nav = soup.select_one(".profile-subnav")
        logout = soup.select_one(".profile-logout-row")
        self.assertIsNotNone(nav)
        self.assertIsNotNone(logout)

        themeScripts = [s for s in soup.find_all("script")
                        if "theme-selector" in s.get_text()]
        self.assertEqual(len(themeScripts), 1,
                         "expected exactly one theme-selector initialiser script")
        script = themeScripts[0]

        #< identity (`is`), never equality: bs4 tags compare equal by CONTENT,
        #  so `in`/== can match a lookalike elsewhere in the page
        swapSiblings = []
        for sibling in nav.next_siblings:
            if sibling is logout:
                break
            swapSiblings.append(sibling)

        descendantOfSwappedSibling = any(
            container is not script and any(found is script for found in container.find_all("script"))
            for container in swapSiblings if getattr(container, "find_all", None)
        )
        self.assertTrue(
            descendantOfSwappedSibling,
            "the theme initialiser must be a DESCENDANT of a sibling between "
            ".profile-subnav and .profile-logout-row - outside that region the "
            "AJAX swap drops it; as a BARE sibling it is swapped in inert and "
            "never revived")


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


class TestSaveFailureIsGeneric(ProfilePreferencesTestCase):
    """A failed save used to flash `str(e)` - and the flash rides in the
    redirect's query string, so the exception text (a table name, a path, a
    lock message) landed in browser history and the access log. The same file
    already states the rule for the token exchange: full detail server-side
    only. The user gets a message that says what to do; app.log gets the why."""

    def test_the_exception_text_stays_out_of_the_redirect(self):
        client = self._loginAs("alice", "alice@example.com")
        self.db.refreshSettings.side_effect = RuntimeError("no such table: users_v9")

        with self.assertLogs("routes.auth", level="WARNING") as logs:
            resp = client.post("/profile", data={
                "action": "save_preferences",
                "default_dashboard_window": "week",
                "default_top_list_window": "year",
                "timezone": "",
            })

        location = unquote_plus(resp.headers["Location"])
        self.assertIn("Failed to save preferences", location)
        self.assertNotIn("users_v9", location)
        self.assertTrue(any("users_v9" in line for line in logs.output), logs.output)
