"""POST/Redirect/GET on /profile, and where the resulting message renders.

Every profile action used to render 200 straight from the POST, so a refresh
re-submitted it (and the confirmation for a form at the bottom of the page
appeared ~400 lines above it, off-screen). Each action now redirects back to
/profile carrying `flash_for`, which both anchors the scroll and tells the
template which section should show the message.

The rate-limited path is the deliberate exception: 429 must stay a rendered
response, since a redirect would drop the status code.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import RATE_LIMIT_MAX_ATTEMPTS
from _app_factory import AppTestCase

_CALLBACK_URL = "https://example.test/spotify-callback"


class ProfilePrgTestCase(AppTestCase):
    def setUp(self):
        self.dash = self._makeApp()

    def _loginAs(self, username, email):
        self.dash.repo.upsertUser(username, email)
        db = MagicMock()
        db.repo = self.dash.repo
        db.getUserSpotifyCredentials.return_value = {}
        db.getUserLastfmApiKey.side_effect = lambda: self.dash.repo.getUserLastfmApiKey(username)
        db.updateUserLastfmApiKey.side_effect = lambda key: self.dash.repo.updateUserLastfmApiKey(username, key)
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


class TestActionsRedirect(ProfilePrgTestCase):
    """Each action returns 302 back to /profile, tagged with its own section."""

    def _assertRedirectsTo(self, resp, flashFor):
        self.assertEqual(resp.status_code, 302)
        location = resp.headers["Location"]
        self.assertIn("/profile", location)
        self.assertIn(f"flash_for={flashFor}", location)
        #< the anchor is what puts the section (and its message) on screen
        self.assertTrue(location.endswith(f"#{flashFor}"), location)

    def test_save_preferences(self):
        client = self._loginAs("alice", "alice@example.com")
        resp = client.post("/profile", data={
            "action": "save_preferences", "default_dashboard_window": "week", "timezone": ""})
        self._assertRedirectsTo(resp, "preferences")

    def test_save_display_name(self):
        client = self._loginAs("alice", "alice@example.com")
        resp = client.post("/profile", data={
            "action": "save_display_name", "display_name": "Alice A"})
        self._assertRedirectsTo(resp, "display-name")

    def test_request_share(self):
        client = self._loginAs("alice", "alice@example.com")
        self.dash.repo.upsertUser("bob", "bob@example.com")
        resp = client.post("/profile", data={
            "action": "request_share", "target_username": "bob"})
        self._assertRedirectsTo(resp, "data-sharing")

    def test_save_lastfm_rejected_key_still_redirects(self):
        """Errors redirect too - otherwise a refresh re-runs the failed save."""
        client = self._loginAs("alice", "alice@example.com")
        with patch("routes.auth.LastfmClient") as mockClientClass:
            mockClientClass.return_value.validateApiKey.return_value = {
                "ok": False, "error": "invalid_key"}
            resp = client.post("/profile", data={
                "action": "save_lastfm", "lastfm_api_key": "badkey"})
        self._assertRedirectsTo(resp, "lastfm")
        self.assertIn("error=", resp.headers["Location"])

    def test_remove_lastfm(self):
        client = self._loginAs("alice", "alice@example.com")
        self.dash.repo.updateUserLastfmApiKey("alice", "key123")
        resp = client.post("/profile", data={"action": "remove_lastfm"})
        self._assertRedirectsTo(resp, "lastfm")

    def test_save_spotify_credentials(self):
        client = self._loginAs("alice", "alice@example.com")
        with patch.dict(os.environ, {"SPOTIFY_CALLBACK_URL": _CALLBACK_URL}):
            resp = client.post("/profile", data={
                "client_id": "abc", "client_secret": "def"})
        self._assertRedirectsTo(resp, "spotify")

    def test_missing_spotify_credentials_redirects_with_the_error(self):
        client = self._loginAs("alice", "alice@example.com")
        with patch.dict(os.environ, {"SPOTIFY_CALLBACK_URL": _CALLBACK_URL}):
            resp = client.post("/profile", data={"client_id": "abc", "client_secret": ""})
        self._assertRedirectsTo(resp, "spotify")
        self.assertIn("error=", resp.headers["Location"])

    def test_following_the_redirect_shows_the_message(self):
        client = self._loginAs("alice", "alice@example.com")
        resp = client.post("/profile", data={
            "action": "save_preferences", "default_dashboard_window": "week", "timezone": ""},
            follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Preferences saved successfully", resp.data)


class TestRateLimitedActionsDoNotRedirect(ProfilePrgTestCase):
    """429 has to be rendered: a 302 would throw the status code away, and the
    browser would follow it into a page that never mentions the throttling."""

    def test_request_share(self):
        client = self._loginAs("alice", "alice@example.com")
        self.dash.repo.upsertUser("bob", "bob@example.com")
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            client.post("/profile", data={"action": "request_share", "target_username": "bob"})

        resp = client.post("/profile", data={"action": "request_share", "target_username": "bob"})

        self.assertEqual(resp.status_code, 429)
        self.assertIn(b"Too many attempts", resp.data)

    def test_save_display_name(self):
        client = self._loginAs("alice", "alice@example.com")
        for i in range(RATE_LIMIT_MAX_ATTEMPTS):
            client.post("/profile", data={"action": "save_display_name",
                                          "display_name": f"Alice {i}"})

        resp = client.post("/profile", data={"action": "save_display_name",
                                             "display_name": "Alice Final"})

        self.assertEqual(resp.status_code, 429)
        self.assertIn(b"Too many attempts", resp.data)

    def test_save_lastfm(self):
        client = self._loginAs("alice", "alice@example.com")
        with patch("routes.auth.LastfmClient") as mockClientClass:
            mockClientClass.return_value.validateApiKey.return_value = {"ok": True}
            for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
                client.post("/profile", data={"action": "save_lastfm",
                                              "lastfm_api_key": "somekey"})

            resp = client.post("/profile", data={"action": "save_lastfm",
                                                 "lastfm_api_key": "somekey"})

        self.assertEqual(resp.status_code, 429)


class TestFlashPlacement(ProfilePrgTestCase):
    """`flash_for` decides which section renders the message."""

    def _indexOf(self, body, needle):
        index = body.find(needle)
        self.assertNotEqual(index, -1, f"{needle!r} not found in the page")
        return index

    def test_message_renders_inside_the_named_section(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/profile?success=Key+saved&flash_for=lastfm")

        body = resp.data
        self.assertIn(b"Key saved", body)
        #< after the Last.fm heading, not stranded at the top of the page
        self.assertGreater(self._indexOf(body, b"Key saved"),
                           self._indexOf(body, b'id="lastfm"'))

    def test_message_without_flash_for_falls_back_to_the_top(self):
        """Redirects from elsewhere (the Spotify OAuth callback, share actions)
        carry no section, so the message still has to render somewhere."""
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/profile?success=Something+happened")

        body = resp.data
        self.assertIn(b"Something happened", body)
        self.assertLess(self._indexOf(body, b"Something happened"),
                        self._indexOf(body, b'id="preferences"'))

    def test_unknown_flash_for_still_renders_the_message(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/profile?success=Still+shown&flash_for=nonsense")

        self.assertIn(b"Still shown", resp.data)

    def test_message_is_not_duplicated_across_sections(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/profile?success=Once+only&flash_for=preferences")

        self.assertEqual(resp.data.count(b"Once only"), 1)


if __name__ == "__main__":
    unittest.main()
