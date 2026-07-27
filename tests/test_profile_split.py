"""The three profile pages and the sub-nav that ties them together.

/profile used to be one 443-line page whose handler ran every query and two
badge-clearing WRITES on every visit - changing your theme acknowledged share
notifications you never saw. Each page now answers only for its own section.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase

_CALLBACK_URL = "https://example.test/spotify-callback"


class ProfileSplitTestCase(AppTestCase):
    def setUp(self):
        self.dash = self._makeApp()

    def _loginAs(self, username="alice", email="alice@example.com"):
        self.dash.repo.upsertUser(username, email)
        db = MagicMock()
        db.repo = self.dash.repo
        db.getUserSpotifyCredentials.return_value = {}
        db.getUserLastfmApiKey.return_value = None
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


class TestEachPageOnlyDoesItsOwnWork(ProfileSplitTestCase):
    def test_account_page_does_not_acknowledge_share_notifications(self):
        """The whole point of the split: visiting Account to change a theme
        used to run markAcceptedSharesSeenByRequester and clear a badge for a
        list that was 200 lines further down the same page."""
        client = self._loginAs()
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.createShareRequest("alice", "bob")
        shareId = self.dash.repo.getPendingIncomingShares("bob")[0]["id"]
        self.dash.repo.respondToShareRequest(shareId, "bob", accept=True)
        self.assertEqual(self.dash.repo.getUnseenAcceptedShareCount("alice"), 1)

        client.get("/profile")

        self.assertEqual(self.dash.repo.getUnseenAcceptedShareCount("alice"), 1)

    def test_sharing_page_still_acknowledges_them(self):
        client = self._loginAs()
        self.dash.repo.upsertUser("bob", "bob@example.com")
        self.dash.repo.createShareRequest("alice", "bob")
        shareId = self.dash.repo.getPendingIncomingShares("bob")[0]["id"]
        self.dash.repo.respondToShareRequest(shareId, "bob", accept=True)

        client.get("/profile/sharing")

        self.assertEqual(self.dash.repo.getUnseenAcceptedShareCount("alice"), 0)

    def test_account_page_carries_no_other_sections(self):
        client = self._loginAs()

        body = client.get("/profile").data

        self.assertIn(b'id="display-name"', body)
        self.assertIn(b'id="preferences"', body)
        self.assertNotIn(b'id="data-sharing"', body)
        self.assertNotIn(b'id="lastfm"', body)

    def test_sharing_page_carries_no_other_sections(self):
        client = self._loginAs()

        body = client.get("/profile/sharing").data

        self.assertIn(b'id="data-sharing"', body)
        self.assertNotIn(b'id="preferences"', body)
        self.assertNotIn(b'id="lastfm"', body)

    def test_connections_page_carries_no_other_sections(self):
        client = self._loginAs()

        with patch.dict(os.environ, {"SPOTIFY_CALLBACK_URL": _CALLBACK_URL}):
            body = client.get("/profile/connections").data

        self.assertIn(b'id="spotify"', body)
        self.assertIn(b'id="lastfm"', body)
        self.assertNotIn(b'id="preferences"', body)
        self.assertNotIn(b'id="data-sharing"', body)


class TestTheSubNav(ProfileSplitTestCase):
    def test_every_page_offers_all_three_tabs(self):
        client = self._loginAs()

        for path in ("/profile", "/profile/sharing"):
            with self.subTest(path=path):
                body = client.get(path).data
                self.assertIn(b'class="profile-subnav"', body)
                self.assertIn(b'href="/profile"', body)
                self.assertIn(b'href="/profile/sharing"', body)
                self.assertIn(b'href="/profile/connections"', body)

    def test_the_current_tab_is_marked(self):
        client = self._loginAs()

        cases = [("/profile", b'aria-current="page"\n     href="/profile"'),
                 ("/profile/sharing", b'aria-current="page"\n     href="/profile/sharing"')]
        for path, marker in cases:
            with self.subTest(path=path):
                self.assertIn(marker, client.get(path).data)

    def test_the_topbar_account_dropdown_stays_highlighted_on_every_page(self):
        """All three keep section="profile", so the dropdown needs no case of
        its own - a regression here is invisible until you look at the nav."""
        client = self._loginAs()

        for path in ("/profile", "/profile/sharing"):
            with self.subTest(path=path):
                self.assertIn(b"nav-account-dropdown active-parent", client.get(path).data)


class TestCompareLinksToShareManagement(ProfileSplitTestCase):
    def test_the_empty_state_points_at_the_sharing_page(self):
        """A user with no accepted share cannot reach /compare at all, so this
        dead-end page is the only route to the request form for them."""
        client = self._loginAs()

        body = client.get("/compare").data

        self.assertIn(b'href="/profile/sharing"', body)


if __name__ == "__main__":
    unittest.main()
