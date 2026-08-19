# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ending sessions on devices this browser cannot reach.

Sessions are signed COOKIES with no server-side store, so "log out" only ever
meant "clear the cookie in front of me" - every other device kept a valid
session for the rest of its 30 days, including after a password reset, which is
the one moment people expect the opposite.

users.session_version is the counter each cookie carries a copy of. Bump it and
every cookie for that account stops matching at once; the browser doing the
bumping re-stamps its own session so it stays where it is.
"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bs4 import BeautifulSoup
from werkzeug.security import generate_password_hash

from config import SESSION_VERSION_KEY
from _app_factory import AppTestCase

_PASSWORD = "Correct-Horse1"


class SessionVersionTestCase(AppTestCase):
    def _account(self, dash, username="alice", email="alice@example.com"):
        dash.repo.upsertUser(username, email)
        dash.repo.setUserCookies(username, {"sp_dc": "abc"})
        dash.repo.setUserPassword(username, generate_password_hash(_PASSWORD))
        dash.repo.commit()
        return username, email

    def _liveApp(self):
        """An app whose Spotify-side login check always says yes, so the only
        thing deciding these requests is the session version."""
        dash = self._makeApp()
        db = MagicMock()
        #< a real dict: /api/listener-status jsonifies this, and a MagicMock
        #  serializes to a 500 that would look like the guard rejecting
        db.getListenerHealth.return_value = {"status": "HEALTHY"}
        self.enterContext(patch.object(dash, "is_user_logged_in", return_value=True))
        self.enterContext(patch.object(dash, "get_user_db", return_value=db))
        return dash

    def _clientWithSession(self, dash, email="alice@example.com", username="alice", version=None):
        """A browser holding a session cookie. `version` None stands for a
        cookie minted before the column existed - it carries no version key at
        all, which is the shape every session out there has on upgrade day."""
        client = dash.app.test_client()
        with client.session_transaction() as sess:
            sess["email"] = email
            sess["username"] = username
            if version is not None:
                sess[SESSION_VERSION_KEY] = version
        return client


class TestTheGuard(SessionVersionTestCase):
    def test_a_session_stamped_with_the_current_version_is_accepted(self):
        dash = self._liveApp()
        self._account(dash)

        response = self._clientWithSession(dash, version=0).get("/api/listener-status")

        self.assertEqual(200, response.status_code)

    def test_a_session_from_before_a_bump_is_rejected(self):
        dash = self._liveApp()
        username, _ = self._account(dash)
        client = self._clientWithSession(dash, version=0)
        dash.repo.bumpUserSessionVersion(username)
        dash.repo.commit()

        response = client.get("/api/listener-status")

        self.assertEqual(401, response.status_code)

    def test_a_session_with_no_version_at_all_still_works_before_any_bump(self):
        """Upgrade day: every cookie already in the wild carries no version
        key, and the column starts at 0 for everyone. Reading a missing one as
        0 is what keeps the upgrade from logging the whole instance out."""
        dash = self._liveApp()
        self._account(dash)

        response = self._clientWithSession(dash, version=None).get("/api/listener-status")

        self.assertEqual(200, response.status_code)

    def test_a_session_with_no_version_is_rejected_once_the_account_bumps(self):
        """And the flip side: those same pre-upgrade cookies ARE what a first
        bump has to end, or the feature does nothing for anyone who has not
        logged in since."""
        dash = self._liveApp()
        username, _ = self._account(dash)
        client = self._clientWithSession(dash, version=None)
        dash.repo.bumpUserSessionVersion(username)
        dash.repo.commit()

        response = client.get("/api/listener-status")

        self.assertEqual(401, response.status_code)

    def test_a_rejected_session_is_cleared_rather_than_re_sent(self):
        """A cookie that can never be accepted again should stop riding along
        on every request - and the next page must not look half-logged-in."""
        dash = self._liveApp()
        username, _ = self._account(dash)
        client = self._clientWithSession(dash, version=0)
        dash.repo.bumpUserSessionVersion(username)
        dash.repo.commit()

        client.get("/api/listener-status")

        with client.session_transaction() as sess:
            self.assertNotIn("email", sess)

    def test_one_account_bumping_does_not_touch_another(self):
        dash = self._liveApp()
        self._account(dash)
        self._account(dash, username="bob", email="bob@example.com")
        bobClient = self._clientWithSession(dash, email="bob@example.com", username="bob", version=0)
        dash.repo.bumpUserSessionVersion("alice")
        dash.repo.commit()

        self.assertEqual(200, bobClient.get("/api/listener-status").status_code)

    def test_an_image_request_honours_the_bump_too(self):
        """The image routes authorize straight off the session rather than
        through the page decorator, so they need the same answer - a signed-out
        device must not keep pulling album art.

        The send is stubbed so a 404 can only come from the authorization: with
        no file on disk this route 404s anyway, and the test would pass with the
        guard deleted."""
        dash = self._liveApp()
        username, _ = self._account(dash)
        self.enterContext(patch.object(dash, "get_username_for_email", return_value="alice"))
        self.enterContext(patch("routes.media.sendCacheableImage", return_value="IMAGE"))
        client = self._clientWithSession(dash, version=0)

        #< the control: with the version still matching, this DOES serve
        self.assertEqual(200, client.get("/img/alice/tracks/abc.jpeg").status_code)

        dash.repo.bumpUserSessionVersion(username)
        dash.repo.commit()

        self.assertEqual(404, client.get("/img/alice/tracks/abc.jpeg").status_code)


class TestEverySurfaceThatReadsTheSession(SessionVersionTestCase):
    """The guard cannot live only where @requiresUser looks.

    /overview is PUBLIC - it renders instance-wide stats for anybody - and
    decides on its own whether to add the viewer's account block, straight off
    session["email"]. So do the context processors behind the topbar. A device
    signed out everywhere must read as anonymous to all of them, not just to
    the pages that redirect."""

    def test_a_public_page_shows_a_signed_out_device_no_account_block(self):
        dash = self._liveApp()
        username, _ = self._account(dash)
        self.enterContext(patch.object(dash, "get_username_for_email", return_value="alice"))
        client = self._clientWithSession(dash, version=0)

        #< the control: while the version matches, the block IS rendered
        self.assertIn("Your Sync Status", client.get("/overview").data.decode())

        dash.repo.bumpUserSessionVersion(username)
        dash.repo.commit()

        self.assertNotIn("Your Sync Status", client.get("/overview").data.decode())

    def test_a_signed_out_device_is_not_still_an_admin_in_the_topbar(self):
        """The context processors read session["username"] directly, and they
        run on public pages too - where nothing else has had a chance to reject
        the cookie."""
        dash = self._liveApp()
        username, _ = self._account(dash)
        dash.repo.setUserAdmin(username, True)
        dash.repo.commit()
        self.enterContext(patch.object(dash, "get_username_for_email", return_value="alice"))
        client = self._clientWithSession(dash, version=0)

        #< the <a>, not the string: overview.html carries "/admin" inside an
        #  HTML COMMENT, which matches a substring assertion and proves nothing
        self.assertIsNotNone(self._adminLink(client), "control failed: no admin link to lose")

        dash.repo.bumpUserSessionVersion(username)
        dash.repo.commit()

        self.assertIsNone(self._adminLink(client))

    @staticmethod
    def _adminLink(client):
        soup = BeautifulSoup(client.get("/overview").data.decode(), "html.parser")
        return soup.find("a", href="/admin")

    def test_the_hook_runs_after_csrf_validation_not_before(self):
        """Order, pinned rather than assumed. CSRF validates a POST against the
        session that signed its token, so clearing an out-of-date session first
        would turn every such POST into a 400 CSRF error instead of the
        ordinary logged-out redirect. It holds today because CSRFProtect is
        constructed in __init__ and this hook is registered by registerRoutes -
        which is exactly the kind of thing a later reshuffle breaks silently."""
        dash = self._liveApp()

        registered = [f.__qualname__ for f in dash.app.before_request_funcs[None]]
        ours = registered.index("SpotifyDashboardApp.registerRoutes.<locals>."
                                "_endSessionsTheAccountHasInvalidated")
        csrf = next(i for i, name in enumerate(registered) if "csrf" in name.lower())

        self.assertLess(csrf, ours, f"CSRF must validate first: {registered}")

    def test_a_static_asset_is_not_gated_on_a_session_at_all(self):
        """Static files carry no account data and are requested dozens at a
        time - checking a session version for each would be pure cost, and a
        signed-out device still needs the stylesheet for the login page."""
        dash = self._liveApp()
        username, _ = self._account(dash)
        client = self._clientWithSession(dash, version=0)
        dash.repo.bumpUserSessionVersion(username)
        dash.repo.commit()

        with patch.object(dash.repo, "getUserSessionVersion") as version:
            response = client.get("/static/css/style.css")

        self.assertEqual(200, response.status_code)
        version.assert_not_called()

    def test_an_anonymous_request_asks_the_database_nothing(self):
        dash = self._liveApp()
        self._account(dash)

        with patch.object(dash.repo, "getUserSessionVersion") as version:
            dash.app.test_client().get("/login")

        version.assert_not_called()


class TestStamping(SessionVersionTestCase):
    """Every door into a session has to stamp the current version, or the user
    is logged out again by the very next request."""

    def _assertStamped(self, client, expected):
        with client.session_transaction() as sess:
            self.assertEqual(expected, sess.get(SESSION_VERSION_KEY))

    def test_a_password_login_stamps_the_current_version(self):
        dash = self._makeApp()
        username, email = self._account(dash)
        dash.repo.bumpUserSessionVersion(username)   #< anything but the 0 default
        dash.repo.commit()
        client = dash.app.test_client()

        with patch.object(dash, "get_user_db"):
            client.post("/login", data={"email": email, "password": _PASSWORD})

        self._assertStamped(client, 1)

    def test_a_cookie_login_stamps_the_current_version(self):
        dash = self._makeApp()
        username, email = self._account(dash)
        dash.repo.bumpUserSessionVersion(username)
        dash.repo.commit()
        client = dash.app.test_client()

        with patch.object(dash, "get_user_db"), \
             patch.object(dash, "_refresh_user_session"), \
             patch.object(dash, "_verifyCookiesMatchEmail", return_value=True):
            client.post("/login", data={"email": email, "cookies": "sp_dc=abc"})

        self._assertStamped(client, 1)

    def test_registering_stamps_a_version(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        with patch.object(dash, "get_user_db"), \
             patch.object(dash, "get_or_create_user", return_value="alice"), \
             patch.object(dash, "_verifyCookiesMatchEmail", return_value=True):
            client.post("/register", data={"email": "alice@example.com", "cookies": "sp_dc=abc",
                                           "password": "Hunter22!", "confirm_password": "Hunter22!"})

        self._assertStamped(client, 0)


class TestPasswordResetEndsOtherSessions(SessionVersionTestCase):
    def _resetPassword(self, dash, client, email="alice@example.com"):
        with patch.object(dash, "get_user_db"), \
             patch.object(dash, "_refresh_user_session"), \
             patch.object(dash, "_verifyCookiesMatchEmail", return_value=True):
            return client.post("/reset-password", data={
                "email": email, "cookies": "sp_dc=abc",
                "password": "Hunter22!", "confirm_password": "Hunter22!"})

    def test_a_reset_bumps_the_version(self):
        dash = self._makeApp()
        username, _ = self._account(dash)

        self._resetPassword(dash, dash.app.test_client())

        self.assertEqual(1, dash.repo.getUserSessionVersion(username))

    def test_the_browser_doing_the_reset_stays_logged_in(self):
        """The bump ends every session including this one, so the reset has to
        re-stamp its own or it signs the user out of the device they are
        holding - one line away from a feature that looks broken."""
        dash = self._liveApp()
        self._account(dash)
        client = dash.app.test_client()

        self._resetPassword(dash, client)

        self.assertEqual(200, client.get("/api/listener-status").status_code)

    def test_another_device_is_signed_out_by_the_reset(self):
        dash = self._liveApp()
        self._account(dash)
        otherDevice = self._clientWithSession(dash, version=0)

        self._resetPassword(dash, dash.app.test_client())

        self.assertEqual(401, otherDevice.get("/api/listener-status").status_code)


class TestSignOutEverywhere(SessionVersionTestCase):
    def test_it_signs_out_another_device(self):
        dash = self._liveApp()
        self._account(dash)
        otherDevice = self._clientWithSession(dash, version=0)
        client = self._clientWithSession(dash, version=0)

        response = client.post("/profile/sign-out-everywhere")

        self.assertEqual(302, response.status_code)
        self.assertEqual(401, otherDevice.get("/api/listener-status").status_code)

    def test_the_device_that_asked_stays_signed_in(self):
        dash = self._liveApp()
        self._account(dash)
        client = self._clientWithSession(dash, version=0)

        client.post("/profile/sign-out-everywhere")

        self.assertEqual(200, client.get("/api/listener-status").status_code)

    def test_it_says_it_worked(self):
        dash = self._liveApp()
        self._account(dash)
        client = self._clientWithSession(dash, version=0)

        response = client.post("/profile/sign-out-everywhere")

        self.assertIn("success=", response.headers["Location"])

    def test_the_confirmation_is_actually_rendered_on_the_page(self):
        """Following the redirect, not just reading it: the flash is attached
        to a named SECTION, and one naming a section the landing page has no
        slot for lands in the fallback or nowhere. A confirmation the user
        never sees is the same as none."""
        dash = self._liveApp()
        self._account(dash)
        self.enterContext(patch.object(dash, "get_username_for_email", return_value="alice"))
        client = self._clientWithSession(dash, version=0)

        response = client.post("/profile/sign-out-everywhere", follow_redirects=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("Signed out on every other device.", response.data.decode())

    def test_it_is_post_only(self):
        """State-changing, so it must not be reachable by a cross-site GET
        link - the same rule /logout follows."""
        dash = self._liveApp()
        self._account(dash)
        client = self._clientWithSession(dash, version=0)

        self.assertEqual(405, client.get("/profile/sign-out-everywhere").status_code)

    def test_the_account_page_offers_it_as_a_post_form(self):
        """Parsed, not string-matched: a button that posts nowhere, or one
        without a CSRF token, would satisfy any grep for the endpoint name and
        fail for a real person."""
        dash = self._liveApp()
        self._account(dash)
        db = MagicMock()
        db.repo = dash.repo
        self.enterContext(patch.object(dash, "get_username_for_email", return_value="alice"))

        html = self._clientWithSession(dash, version=0).get("/profile").data.decode()

        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form", attrs={"action": "/profile/sign-out-everywhere"})
        self.assertIsNotNone(form, "the Account page offers no sign-out-everywhere form")
        self.assertEqual("post", (form.get("method") or "").lower())
        self.assertIsNotNone(form.find("input", attrs={"name": "csrf_token"}),
                             "the form would be rejected by CSRF protection")
        self.assertIsNotNone(form.find("button"), "the form has no way to submit it")

    def test_an_anonymous_caller_gets_nowhere(self):
        dash = self._liveApp()
        self._account(dash)

        response = dash.app.test_client().post("/profile/sign-out-everywhere")

        self.assertEqual(302, response.status_code)
        self.assertIn("/login", response.headers["Location"])
        self.assertEqual(0, dash.repo.getUserSessionVersion("alice"))


if __name__ == "__main__":
    unittest.main()
