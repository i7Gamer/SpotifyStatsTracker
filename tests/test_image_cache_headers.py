# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the four image routes tell the browser about caching.

These files are write-once: tryClaimImageDownload refuses to re-claim an image
already marked OK, and the artist route's lazy fetch only writes when the file
is ABSENT - so a given <imageId>.jpeg never changes content, and a different
image always arrives under a different id. That is what makes a long max-age
correct here rather than merely convenient.

Without one, every image on every page load is a conditional request. Not a
free one: /img/<username>/... is an authenticated route, so each 304 costs a
session read plus is_user_logged_in's queries, times ~30 images on a top-list
page.

Real requests through the app, with real files on disk - a mocked
send_from_directory would answer with whatever the mock returns and prove
nothing about the headers, which are entirely Flask's to set.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase
from Database.database import Database
from config import IMAGE_CACHE_MAX_AGE_SECONDS

_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class ImageCacheTestCase(AppTestCase):
    def setUp(self):
        patcher = patch("app.SpotifyDashboardApp._get_or_create_secret_key",
                        return_value="test-secret-key")
        patcher.start()
        self.addCleanup(patcher.stop)

        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        media = Path(self._tmpdir.name)
        (media / "tracks").mkdir()
        (media / "artists").mkdir()
        (media / "tracks" / "alb1.jpeg").write_bytes(_PNG)
        (media / "artists" / "art1.jpeg").write_bytes(_PNG)
        self.enterContext(patch.object(Database, "imgDir_tracks", media / "tracks"))
        self.enterContext(patch.object(Database, "imgDir_artists", media / "artists"))

    def fetch(self, client, url):
        """The response, with its file handle released. send_file streams the
        file, and on Windows the still-open handle blocks the temp directory's
        cleanup - the headers and status stay readable after close()."""
        response = client.get(url)
        response.close()
        return response

    def assertCachedPrivately(self, response):
        cacheControl = response.headers.get("Cache-Control", "")
        self.assertEqual(200, response.status_code)
        self.assertIn(f"max-age={IMAGE_CACHE_MAX_AGE_SECONDS}", cacheControl)
        # private, not public: these sit behind a session check, so a shared
        # proxy must not be allowed to hand one viewer's response to the next.
        # Flask's own max_age= emits "public", which is why this is asserted
        # rather than assumed.
        self.assertIn("private", cacheControl)
        self.assertNotIn("public", cacheControl)
        self.assertNotIn("no-store", cacheControl)


class TestAuthenticatedImageCaching(ImageCacheTestCase):
    def _authedClient(self, dash):
        self.enterContext(patch.object(dash, "is_user_logged_in", return_value=True))
        self.enterContext(patch.object(dash, "get_username_for_email", return_value="alice"))
        client = dash.app.test_client()
        with client.session_transaction() as sess:
            sess["email"] = "alice@example.com"
        return client

    def test_a_track_image_is_cacheable(self):
        dash = self._makeApp()

        self.assertCachedPrivately(self.fetch(self._authedClient(dash), "/img/alice/tracks/alb1.jpeg"))

    def test_an_artist_image_is_cacheable(self):
        dash = self._makeApp()
        dash.user_databases["alice"] = MagicMock()

        self.assertCachedPrivately(self.fetch(self._authedClient(dash), "/img/alice/artists/art1.jpeg"))

    def test_an_image_that_is_not_there_yet_is_never_cached(self):
        """The artist route fetches a missing image in the background, so the
        404 it answers with meanwhile is a temporary state. Caching THAT would
        pin the gap for the whole max-age window - the global no-store has to
        keep applying to it, which it does because a 404 carries no
        Cache-Control of its own for setdefault to defer to."""
        dash = self._makeApp()
        dash.user_databases["alice"] = MagicMock()

        response = self.fetch(self._authedClient(dash), "/img/alice/artists/never-fetched.jpeg")

        self.assertEqual(404, response.status_code)
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def test_a_refused_image_is_never_cached(self):
        """The authorization refusal is a bare ("", 404) from the view, and it
        must not be cacheable either - the session that owns the segment can
        change while the browser holds the answer."""
        dash = self._makeApp()

        response = self.fetch(self._authedClient(dash), "/img/bob/tracks/alb1.jpeg")

        self.assertEqual(404, response.status_code)
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def test_the_page_html_itself_stays_uncacheable(self):
        """The exemption is for images only. Everything this app renders is one
        account's data, and a cached page is what hands the next person on a
        shared browser the previous account's history."""
        dash = self._makeApp()

        response = self.fetch(self._authedClient(dash), "/login")

        self.assertIn("no-store", response.headers.get("Cache-Control", ""))


class TestSharedLinkImageCaching(ImageCacheTestCase):
    """The public /shared/<token>/img/... pair. Same files, same reasoning -
    and a public link is exactly where the round trips are most worth saving,
    since the viewer has no session and no reason to come back."""

    def _sharedToken(self, dash):
        dash.repo.upsertUser("alice", "alice@example.com")
        return dash.repo.createShareLink("alice", dash.repo.SHARE_LINK_KIND_WRAPPED, 2026, None)

    def test_a_shared_track_image_is_cacheable(self):
        dash = self._makeApp()
        token = self._sharedToken(dash)

        response = self.fetch(dash.app.test_client(), f"/shared/{token}/img/tracks/alb1.jpeg")

        self.assertCachedPrivately(response)

    def test_a_shared_artist_image_is_cacheable(self):
        dash = self._makeApp()
        token = self._sharedToken(dash)

        response = self.fetch(dash.app.test_client(), f"/shared/{token}/img/artists/art1.jpeg")

        self.assertCachedPrivately(response)

    def test_a_revoked_link_is_not_cacheable(self):
        dash = self._makeApp()
        token = self._sharedToken(dash)
        dash.repo.revokeShareLink(dash.repo.getShareLink(token)["id"], "alice")

        response = self.fetch(dash.app.test_client(), f"/shared/{token}/img/tracks/alb1.jpeg")

        self.assertEqual(404, response.status_code)
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))


if __name__ == "__main__":
    unittest.main()
