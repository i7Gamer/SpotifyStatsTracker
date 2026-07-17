"""Public Wrapped share links: creating/revoking a link from the
authenticated /wrapped and /profile routes, and the public, unauthenticated
GET /shared/<token> page and its image routes.
"""
import datetime
import unittest
from unittest.mock import patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as appModule
from app import SpotifyDashboardApp, RATE_LIMIT_MAX_ATTEMPTS
import Database.utils as utilsModule

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class ShareLinkRoutesTestCase(unittest.TestCase):
    """Freezes now()/tz like test_wrapped_route.py, since sharedWrappedPage()
    renders wrapped.html through the same _buildWrappedContext() pipeline."""

    def setUp(self):
        tzPatcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        tzPatcher.start()
        self.addCleanup(tzPatcher.stop)

        nowPatcher = patch.object(appModule, "now",
                                   return_value=datetime.datetime(2026, 7, 11, tzinfo=datetime.timezone.utc))
        nowPatcher.start()
        self.addCleanup(nowPatcher.stop)

        self.dash = self._makeApp()

    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _makeDb(self):
        db = MagicMock()
        db.tz = datetime.timezone.utc   #< profilePage()'s dateToString() needs a real tzinfo, not a MagicMock
        db.getEntriesFromOld.return_value = []
        db.getTopSongs.return_value = []
        db.getTopArtists.return_value = []
        db.getTopAlbums.return_value = []
        db.getPlayTotals.return_value = (0, 0)
        db.getSongsStats.return_value = []
        db.getArtistsStats.return_value = []
        db.getAlbumsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getUserSpotifyCredentials.return_value = {}
        return db

    def _loginAs(self, username, email, db=None):
        self.dash.repo.upsertUser(username, email)
        db = db or self._makeDb()
        patcher_login = patch.object(self.dash, 'is_user_logged_in', return_value=True)
        patcher_email = patch.object(self.dash, 'get_username_for_email', return_value=username)
        patcher_db = patch.object(self.dash, 'get_user_db', return_value=db)
        patcher_login.start()
        patcher_email.start()
        patcher_db.start()
        self.addCleanup(patcher_login.stop)
        self.addCleanup(patcher_email.stop)
        self.addCleanup(patcher_db.stop)

        client = self.dash.app.test_client()
        with client.session_transaction() as sess:
            sess['email'] = email
            sess['username'] = username
        return client


class TestCreateShareLink(ShareLinkRoutesTestCase):
    def test_creates_a_link_and_redirects_with_success(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/wrapped/share-links/2026", data={"expiry": "never"})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/wrapped", resp.headers["Location"])
        links = self.dash.repo.getShareLinksForUser("alice")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["year"], 2026)
        self.assertIsNone(links[0]["expires_at"])

    def test_expiry_choice_is_stored(self):
        client = self._loginAs("alice", "alice@example.com")

        client.post("/wrapped/share-links/2026", data={"expiry": "7d"})

        link = self.dash.repo.getShareLinksForUser("alice")[0]
        self.assertIsNotNone(link["expires_at"])

    def test_disabled_feature_returns_404(self):
        self.dash.repo.setShareLinksEnabled(False)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.post("/wrapped/share-links/2026", data={"expiry": "never"})

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.dash.repo.getShareLinksForUser("alice"), [])

    def test_anonymous_redirects_to_login(self):
        client = self.dash.app.test_client()

        resp = client.post("/wrapped/share-links/2026", data={"expiry": "never"})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_rate_limited_after_max_attempts(self):
        client = self._loginAs("alice", "alice@example.com")
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            client.post("/wrapped/share-links/2026", data={"expiry": "never"})

        resp = client.post("/wrapped/share-links/2026", data={"expiry": "never"})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])
        self.assertEqual(len(self.dash.repo.getShareLinksForUser("alice")), RATE_LIMIT_MAX_ATTEMPTS)


class TestRevokeShareLink(ShareLinkRoutesTestCase):
    def test_owner_can_revoke_and_redirects_with_success(self):
        client = self._loginAs("alice", "alice@example.com")
        token = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        linkId = self.dash.repo.getShareLink(token)["id"]

        resp = client.post(f"/profile/share-links/{linkId}")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/profile", resp.headers["Location"])
        self.assertIsNone(self.dash.repo.getShareLink(token))

    def test_non_owner_cannot_revoke(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        token = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        linkId = self.dash.repo.getShareLink(token)["id"]
        client = self._loginAs("bob", "bob@example.com")

        resp = client.post(f"/profile/share-links/{linkId}")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])
        self.assertIsNotNone(self.dash.repo.getShareLink(token))

    def test_anonymous_redirects_to_login(self):
        client = self.dash.app.test_client()

        resp = client.post("/profile/share-links/1")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])


class TestShareLinkListOnProfilePage(ShareLinkRoutesTestCase):
    def test_lists_year_created_and_expiry(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, expiresInSeconds=None)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/profile")
        body = resp.data.decode()

        self.assertIn("Wrapped Share Links", body)
        self.assertIn("2026", body)
        self.assertIn("Never", body)   #< expiresInSeconds=None -> "Never" expires

    def test_revoke_button_targets_the_right_link(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        token = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        linkId = self.dash.repo.getShareLink(token)["id"]
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/profile")

        self.assertIn(f'action="/profile/share-links/{linkId}"'.encode(), resp.data)

    def test_section_hidden_when_user_has_no_links(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/profile")

        self.assertNotIn(b"Wrapped Share Links", resp.data)

    def test_section_hidden_when_feature_disabled_even_with_links(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        self.dash.repo.setShareLinksEnabled(False)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/profile")

        self.assertNotIn(b"Wrapped Share Links", resp.data)

    def test_expired_link_does_not_appear(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, expiresInSeconds=-10)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/profile")

        self.assertNotIn(b"Wrapped Share Links", resp.data)


class PublicSharedWrappedTestCase(ShareLinkRoutesTestCase):
    def _createLink(self, username="alice", email="alice@example.com", year=2026, expiresInSeconds=None):
        self.dash.repo.upsertUser(username, email)
        return self.dash.repo.createShareLink(
            username, self.dash.repo.SHARE_LINK_KIND_WRAPPED, year, expiresInSeconds)

    def _getShared(self, token, db=None):
        client = self.dash.app.test_client()
        with patch.object(self.dash, '_getReadOnlyUserDb', return_value=db or self._makeDb()):
            return client.get(f"/shared/{token}")


class TestPublicSharedWrappedPage(PublicSharedWrappedTestCase):
    def test_valid_token_renders_200(self):
        token = self._createLink()

        resp = self._getShared(token)

        self.assertEqual(resp.status_code, 200)

    def test_unknown_token_404s(self):
        resp = self._getShared("does-not-exist")

        self.assertEqual(resp.status_code, 404)

    def test_disabled_feature_404s_even_for_a_valid_token(self):
        token = self._createLink()
        self.dash.repo.setShareLinksEnabled(False)

        resp = self._getShared(token)

        self.assertEqual(resp.status_code, 404)

    def test_expired_token_404s(self):
        # Negative expiresInSeconds -> already in the past at creation, no
        # time mocking needed (see test_repository_share_links.py for the
        # dedicated lazy-deletion coverage of getShareLink itself).
        token = self._createLink(expiresInSeconds=-10)

        resp = self._getShared(token)

        self.assertEqual(resp.status_code, 404)

    def test_revoked_token_404s(self):
        token = self._createLink()
        linkId = self.dash.repo.getShareLink(token)["id"]
        self.dash.repo.revokeShareLink(linkId, "alice")

        resp = self._getShared(token)

        self.assertEqual(resp.status_code, 404)

    def test_no_pii_in_public_response(self):
        token = self._createLink()

        resp = self._getShared(token)

        self.assertNotIn(b"alice@example.com", resp.data)

    def test_track_card_images_use_the_token_keyed_image_route(self):
        """_track_card.html's imageBase override must actually take effect on
        the public page - otherwise cards would request /img/alice/... ,
        which 404s for an anonymous viewer (see serveTrackImage's own
        session-authorization check)."""
        token = self._createLink()
        db = self._makeDb()
        db.getTopSongs.return_value = [{
            "id": "song1", "name": "Song", "url": "u", "imageId": "img1", "duration": 0,
            "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album", "url": "u", "imageId": "img1", "imageUrl": "",
                       "totalTracks": 1, "releaseDate": 0},
            "artists": [], "plays": 5, "totalTimeListened": 5000, "firstListenedAt": 0,
        }]

        resp = self._getShared(token, db=db)
        body = resp.data.decode()

        self.assertIn(f'src="/shared/{token}/img/tracks/img1.jpeg"', body)
        self.assertNotIn('src="/img/alice/', body)

    def test_noindex_header_present(self):
        token = self._createLink()

        resp = self._getShared(token)

        self.assertEqual(resp.headers.get("X-Robots-Tag"), "noindex")

    def test_no_authenticated_nav_or_filter_controls(self):
        """The public page must use layout_public.html (no topbar/nav) and
        must not show the AJAX filter form, year badges, or export button -
        none of that machinery applies to an anonymous, single-year view."""
        token = self._createLink()

        resp = self._getShared(token)
        body = resp.data.decode()

        self.assertNotIn('id="nav-toggle"', body)
        self.assertNotIn('id="groupBy"', body)
        self.assertNotIn('id="exportWrappedBtn"', body)
        self.assertNotIn('class="wrapped-year-badges"', body)

    def test_no_share_panel_on_the_public_page_itself(self):
        token = self._createLink()

        resp = self._getShared(token)

        self.assertNotIn(b"Share this Wrapped", resp.data)

    def test_repeated_valid_visits_are_not_rate_limited(self):
        token = self._createLink()

        for _ in range(RATE_LIMIT_MAX_ATTEMPTS + 5):
            resp = self._getShared(token)
            self.assertEqual(resp.status_code, 200)

    def test_unknown_token_misses_are_rate_limited(self):
        for i in range(RATE_LIMIT_MAX_ATTEMPTS):
            self._getShared(f"unknown-{i}")

        resp = self._getShared("one-more-unknown")

        self.assertEqual(resp.status_code, 429)


class TestShareLinkPanelOnWrappedPage(ShareLinkRoutesTestCase):
    """The owner-only 'Share this Wrapped' panel on the authenticated
    /wrapped page - not to be confused with the public page itself."""

    def test_shows_create_form_when_no_link_exists_for_the_year(self):
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/wrapped?year=2026")
        body = resp.data.decode()

        self.assertIn("Share this Wrapped", body)
        self.assertIn('action="/wrapped/share-links/2026"', body)
        self.assertNotIn("Revoke", body)

    def test_shows_existing_link_and_revoke_when_one_exists_for_the_year(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        token = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/wrapped?year=2026")
        body = resp.data.decode()

        self.assertIn(f"/shared/{token}", body)
        self.assertIn("Revoke", body)
        self.assertNotIn("Create Share Link", body)

    def test_a_different_years_link_does_not_show_as_the_current_one(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2025, None)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/wrapped?year=2026")
        body = resp.data.decode()

        self.assertIn("Create Share Link", body)
        self.assertNotIn("Revoke", body)

    def test_panel_hidden_when_feature_disabled(self):
        self.dash.repo.setShareLinksEnabled(False)
        client = self._loginAs("alice", "alice@example.com")

        resp = client.get("/wrapped?year=2026")

        self.assertNotIn(b"Share this Wrapped", resp.data)


class TestSharedImageRoutes(ShareLinkRoutesTestCase):
    def _createLink(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")
        return self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)

    @patch('app.send_from_directory')
    def test_valid_token_serves_track_image(self, mock_send):
        mock_send.return_value = "OK"
        token = self._createLink()
        client = self.dash.app.test_client()

        resp = client.get(f"/shared/{token}/img/tracks/abc.jpeg")

        self.assertEqual(resp.status_code, 200)
        mock_send.assert_called_once()
        self.assertEqual(resp.headers.get("X-Robots-Tag"), "noindex")

    def test_unknown_token_404s_for_track_image(self):
        client = self.dash.app.test_client()

        resp = client.get("/shared/does-not-exist/img/tracks/abc.jpeg")

        self.assertEqual(resp.status_code, 404)

    def test_path_traversal_filename_404s(self):
        token = self._createLink()
        client = self.dash.app.test_client()

        resp = client.get(f"/shared/{token}/img/tracks/..%5C..%5Csecret.txt")

        self.assertEqual(resp.status_code, 404)

    @patch('app.send_from_directory')
    @patch('app.os.path.exists', return_value=False)
    def test_valid_token_lazily_fetches_missing_artist_image(self, mock_exists, mock_send):
        mock_send.return_value = "OK"
        token = self._createLink()
        readOnlyDb = self._makeDb()
        client = self.dash.app.test_client()

        with patch.object(self.dash, '_getReadOnlyUserDb', return_value=readOnlyDb):
            resp = client.get(f"/shared/{token}/img/artists/art1.jpeg")

        self.assertEqual(resp.status_code, 200)
        readOnlyDb.lazyFetchArtistImage.assert_called_once()
        self.assertEqual(readOnlyDb.lazyFetchArtistImage.call_args.args[0], "art1")


class TestActivationGuardViaPublicRoute(ShareLinkRoutesTestCase):
    """The end-to-end version of the activation-guard tests in
    test_read_only_user_db.py, driven through the actual HTTP routes: a
    public share-link view for a "cold" username must never activate the
    listener, but the owner's next real login must activate that exact same
    cached instance rather than being silently skipped."""

    def _makeMockDb(self, *a, **k):
        return self._makeDb()

    def test_cold_username_view_then_real_login_activates_once(self):
        self.dash.repo.upsertUser("alice", "alice@example.com")

        with patch('app.Database', side_effect=self._makeMockDb):
            token = self.dash.repo.createShareLink("alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)
            client = self.dash.app.test_client()

            sharedResp = client.get(f"/shared/{token}")
            self.assertEqual(sharedResp.status_code, 200)

            coldDb = self.dash.user_databases["alice"]
            coldDb.startAutoImporter.assert_not_called()
            coldDb.resetProgress.assert_not_called()
            coldDb.startListener.assert_not_called()

            with patch.object(self.dash, 'is_user_logged_in', return_value=True), \
                 patch.object(self.dash, 'get_username_for_email', return_value='alice'):
                with client.session_transaction() as sess:
                    sess['email'] = 'alice@example.com'
                loginResp = client.get("/wrapped")

        self.assertEqual(loginResp.status_code, 200)
        activatedDb = self.dash.user_databases["alice"]
        self.assertIs(activatedDb, coldDb)   #< same object, never reconstructed
        coldDb.startAutoImporter.assert_called_once()
        coldDb.resetProgress.assert_called_once()
        coldDb.startListener.assert_called_once_with(email="alice@example.com")
        self.assertIn("alice", self.dash._activatedUsers)


if __name__ == "__main__":
    unittest.main()
