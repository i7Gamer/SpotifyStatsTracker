# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Auth pages (login/register/reset-password) and profile's display-name form
carried no autocomplete hints, and their password-rule/error text sat only
visually beside the field it described - never linked via aria-describedby,
so a screen reader focusing the input never announced it.

2026-09-02 review, UT-20."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from tests._app_factory import AppTestCase


def _describedByTarget(body, inputId):
    """The element id an input[id=inputId]'s aria-describedby points at, or
    None if the input has no such attribute. Verifies the referenced element
    actually exists in the same render, not just that the attribute is
    present."""
    inputMatch = re.search(r'<input[^>]*\bid="%s"[^>]*>' % re.escape(inputId), body)
    assert inputMatch, f"no input#{inputId} in the rendered page"
    describedByMatch = re.search(r'aria-describedby="([^"]+)"', inputMatch.group(0))
    if not describedByMatch:
        return None
    targetId = describedByMatch.group(1)
    assert re.search(r'\bid="%s"' % re.escape(targetId), body), \
        f"aria-describedby target #{targetId} does not exist in the render"
    return targetId


class LoginPageAutocompleteTestCase(AppTestCase):
    def test_email_and_password_carry_autocomplete_hints(self):
        dash = self._makeApp()
        body = dash.app.test_client().get("/login").get_data(as_text=True)

        self.assertRegex(body, r'<input[^>]*id="loginEmail"[^>]*autocomplete="username"')
        self.assertRegex(body, r'<input[^>]*id="loginPassword"[^>]*autocomplete="current-password"')


class RegisterPageAccessibilityTestCase(AppTestCase):
    def test_email_and_password_fields_carry_autocomplete_hints(self):
        dash = self._makeApp()
        body = dash.app.test_client().get("/register").get_data(as_text=True)

        self.assertRegex(body, r'<input[^>]*id="registerEmail"[^>]*autocomplete="username"')
        self.assertRegex(body, r'<input[^>]*id="registerPassword"[^>]*autocomplete="new-password"')
        self.assertRegex(body, r'<input[^>]*id="registerConfirmPassword"[^>]*autocomplete="new-password"')

    def test_password_rule_hint_is_linked_via_aria_describedby(self):
        dash = self._makeApp()
        body = dash.app.test_client().get("/register").get_data(as_text=True)

        target = _describedByTarget(body, "registerPassword")
        self.assertIsNotNone(target)
        hintMatch = re.search(r'<small id="%s"[^>]*>([^<]*)</small>' % re.escape(target), body)
        self.assertIsNotNone(hintMatch)
        self.assertIn("characters", hintMatch.group(1))


class ResetPasswordPageAccessibilityTestCase(AppTestCase):
    def test_email_and_password_fields_carry_autocomplete_hints(self):
        dash = self._makeApp()
        body = dash.app.test_client().get("/reset-password").get_data(as_text=True)

        self.assertRegex(body, r'<input[^>]*id="resetEmail"[^>]*autocomplete="username"')
        self.assertRegex(body, r'<input[^>]*id="resetPassword"[^>]*autocomplete="new-password"')
        self.assertRegex(body, r'<input[^>]*id="resetConfirmPassword"[^>]*autocomplete="new-password"')

    def test_password_rule_hint_is_linked_via_aria_describedby(self):
        dash = self._makeApp()
        body = dash.app.test_client().get("/reset-password").get_data(as_text=True)

        target = _describedByTarget(body, "resetPassword")
        self.assertIsNotNone(target)
        hintMatch = re.search(r'<small id="%s"[^>]*>([^<]*)</small>' % re.escape(target), body)
        self.assertIsNotNone(hintMatch)
        self.assertIn("characters", hintMatch.group(1))


class ProfileDisplayNameAccessibilityTestCase(AppTestCase):
    def _loginAs(self, dash, username="alice", email="alice@example.com"):
        from unittest.mock import patch, MagicMock
        dash.repo.upsertUser(username, email)
        db = MagicMock()
        db.repo = dash.repo
        db.getUserSpotifyCredentials.return_value = {}
        for patcher in (
            patch.object(dash, 'is_user_logged_in', return_value=True),
            patch.object(dash, 'get_username_for_email', return_value=username),
            patch.object(dash, 'get_user_db', return_value=db),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        client = dash.app.test_client()
        with client.session_transaction() as sess:
            sess['email'] = email
            sess['username'] = username
        return client

    def test_no_error_state_has_no_describedby_or_invalid(self):
        """Negative control: a clean load must not reference a flash element
        that was never rendered."""
        dash = self._makeApp()
        client = self._loginAs(dash)

        body = client.get("/profile").get_data(as_text=True)

        self.assertIsNone(_describedByTarget(body, "display_name"))
        self.assertNotRegex(body, r'<input[^>]*id="display_name"[^>]*aria-invalid')

    def test_a_rejected_name_links_the_field_to_its_error(self):
        dash = self._makeApp()
        client = self._loginAs(dash)

        body = client.post("/profile", data={"action": "save_display_name",
                                              "display_name": "A"},
                           follow_redirects=True).get_data(as_text=True)

        target = _describedByTarget(body, "display_name")
        self.assertEqual(target, "display-name-flash")
        errorMatch = re.search(r'<p class="profile-flash profile-flash-error" id="display-name-flash">([^<]*)</p>', body)
        self.assertIsNotNone(errorMatch)
        self.assertIn("at least", errorMatch.group(1))
        self.assertRegex(body, r'<input[^>]*id="display_name"[^>]*aria-invalid="true"')


if __name__ == "__main__":
    unittest.main()
