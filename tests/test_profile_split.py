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


class TestLinksFromOtherPagesPointAtTheRightTab(unittest.TestCase):
    """Prompts elsewhere in the app that send a user to add an API key must
    point at the page holding the form. The split moved the form to
    /profile/connections while four other templates still said "your profile",
    landing people on Account with no field in sight.

    A source scan rather than a render: these prompts only appear in states
    that are awkward to reach through the test client (no genre coverage yet,
    no plays at all), which is exactly why the stale links survived the split.
    """

    #< templates whose profilePage link is a *form* target that moved
    PROMPT_TEMPLATES = [
        "_genre_progress.html",
        "_history_results.html",
        "overview.html",
    ]
    #< the Account page's own forms and the two navs legitimately link there
    ALLOWED = {"profile.html", "_profile_nav.html", "layout.html"}

    def _templateDir(self):
        return os.path.join(os.path.dirname(__file__), "..", "templates")

    def test_the_api_key_prompts_link_to_connections(self):
        for name in self.PROMPT_TEMPLATES:
            with self.subTest(template=name):
                with open(os.path.join(self._templateDir(), name), encoding="utf-8") as handle:
                    source = handle.read()
                self.assertIn("url_for('profileConnectionsPage')", source)
                self.assertNotIn("url_for('profilePage')", source)

    def test_no_other_template_links_to_the_account_page(self):
        """Catches the next one: a new prompt added to some other page that
        copies the old wording."""
        offenders = []
        for name in sorted(os.listdir(self._templateDir())):
            if not name.endswith(".html") or name in self.ALLOWED:
                continue
            with open(os.path.join(self._templateDir(), name), encoding="utf-8") as handle:
                if "url_for('profilePage')" in handle.read():
                    offenders.append(name)
        self.assertEqual(offenders, [])


class TestFlashSurvivesAGatedSection(ProfileSplitTestCase):
    def test_a_lastfm_message_still_renders_with_the_section_switched_off(self):
        """remove_lastfm works whether or not the admin switch is on, so its
        confirmation has to land somewhere even when the Last.fm section it
        names was never rendered."""
        self.dash.repo.setLastfmGenreBackfillEnabled(False)
        client = self._loginAs()

        with patch.dict(os.environ, {"SPOTIFY_CALLBACK_URL": _CALLBACK_URL}):
            resp = client.get("/profile/connections?success=Key+removed&flash_for=lastfm")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'id="lastfm"', resp.data)   #< the section really is absent
        self.assertIn(b"Key removed", resp.data)

    def test_a_spotify_message_still_renders_without_the_callback_env(self):
        self.assertNotIn("SPOTIFY_CALLBACK_URL", os.environ)
        client = self._loginAs()

        resp = client.get("/profile/connections?success=Creds+saved&flash_for=spotify")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'id="spotify"', resp.data)
        self.assertIn(b"Creds saved", resp.data)


class TestCompareLinksToShareManagement(ProfileSplitTestCase):
    def test_the_empty_state_points_at_the_sharing_page(self):
        """A user with no accepted share cannot reach /compare at all, so this
        dead-end page is the only route to the request form for them."""
        client = self._loginAs()

        body = client.get("/compare").data

        self.assertIn(b'href="/profile/sharing"', body)


if __name__ == "__main__":
    unittest.main()
