import unittest
from unittest.mock import patch
from tests._app_factory import AppTestCase, makeApp


def makeTrack(trackId="t1", name="Song One", albumId="alb1", artistId="art1"):
    return {
        "id": trackId,
        "name": name,
        "url": f"http://example.com/track/{trackId}",
        "artists": [
            {"id": artistId, "name": f"Artist {artistId}", "url": f"http://example.com/artist/{artistId}",
             "imageUrl": "", "imageId": artistId},
        ],
        "album": {
            "id": albumId, "name": f"Album {albumId}", "url": f"http://example.com/album/{albumId}",
            "imageId": albumId, "imageUrl": "http://img.example.com/a.jpg",
            "totalTracks": 10, "releaseDate": 12345.0,
        },
        "imageUrl": "http://img.example.com/a.jpg",
        "imageId": albumId,
        "duration": 200000,
        "explicit": False,
        "isrc": "US1234567890",
        "discNumber": 1,
        "trackNumber": 3,
        "releaseDate": 12345.0,
    }


class TestTagsRoutes(AppTestCase):
    def setUp(self):
        self.dash = makeApp()
        self.client = self.dash.app.test_client()
        self.username = "testuser"
        self.email = "testuser@example.com"

        self.listener_patcher = patch("Database.database.Database.startListener")
        self.listener_patcher.start()

        # Create user & set cookies so get_user_db works
        self.dash.repo.upsertUser(self.username, self.email)
        self.dash.repo.setUserCookies(self.username, {"sp_dc": "fake_cookie"})
        self.user_db = self.dash.get_user_db(self.username, self.email)

        # Seed sample catalog & plays
        self.dash.repo.upsertTrack(makeTrack(trackId="t1", name="Rock Song", albumId="alb1", artistId="art1"))
        self.dash.repo.insertPlay(self.username, "t1", 1000.0, 200000)
        self.dash.repo.commit()

        self.logged_in_patcher = patch.object(self.dash, "is_user_logged_in", return_value=True)
        self.logged_in_patcher.start()

    def tearDown(self):
        self.logged_in_patcher.stop()
        self.listener_patcher.stop()

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["email"] = self.email
            sess["username"] = self.username

    def test_unauthenticated_api_returns_401(self):
        resp = self.client.get("/api/tags")
        self.assertEqual(resp.status_code, 401)

    def test_add_and_get_tags_api(self):
        self._login()
        resp = self.client.post("/api/tags", json={
            "entity_type": "track",
            "entity_id": "t1",
            "tag": "#Workout",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["tag"], "workout")
        self.assertIn("workout", data["tags"])

        # Fetch tag list
        resp = self.client.get("/api/tags")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["tags"], [{"tag": "workout", "count": 1}])

    def test_remove_tag_api(self):
        self._login()
        self.client.post("/api/tags", json={"entity_type": "track", "entity_id": "t1", "tag": "workout"})
        resp = self.client.delete("/api/tags", json={"entity_type": "track", "entity_id": "t1", "tag": "workout"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["tags"], [])

    def test_playlist_preview_and_export_api(self):
        self._login()
        self.client.post("/api/tags", json={"entity_type": "track", "entity_id": "t1", "tag": "workout"})

        # Preview
        resp = self.client.get("/api/playlists/preview?tags=workout")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["track_count"], 1)

        # Export CSV
        resp = self.client.get("/playlist/export?tags=workout&format=csv")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/csv")
        self.assertIn("Rock Song", resp.get_data(as_text=True))

        # Export M3U
        resp = self.client.get("/playlist/export?tags=workout&format=m3u")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("audio/x-mpegurl", resp.mimetype)
        self.assertIn("spotify:track:t1", resp.get_data(as_text=True))

        # Export XSPF
        resp = self.client.get("/playlist/export?tags=workout&format=xspf")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/xspf+xml", resp.mimetype)
        self.assertIn("<location>spotify:track:t1</location>", resp.get_data(as_text=True))

    def test_playlist_export_sort_by_recent(self):
        self._login()
        self.client.post("/api/tags", json={"entity_type": "track", "entity_id": "t1", "tag": "workout"})

        resp = self.client.get("/playlist/export?tags=workout&format=csv&sort=recent")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("Rock Song", resp.get_data(as_text=True))

    def test_playlist_export_filename_is_header_safe(self):
        self._login()
        # A tag with a quote (injection) and a non-Latin-1 char (中, which would
        # make Werkzeug 500 when encoding the Content-Disposition header) must be
        # sanitized out of the download filename.
        resp = self.client.get('/playlist/export?tags=chill%E4%B8%AD%22&format=csv')

        self.assertEqual(resp.status_code, 200)
        cd = resp.headers["Content-Disposition"]
        self.assertNotIn('"out', cd)
        self.assertNotIn("中", cd)
        self.assertTrue(cd.startswith('attachment; filename="playlist_chill'))
        cd.encode("latin-1")   # must not raise - header would otherwise 500

    def test_playlists_page_renders(self):
        self._login()
        resp = self.client.get("/playlists")
        self.assertEqual(resp.status_code, 200)

    def test_nav_shows_playlists_link_by_default(self):
        self._login()
        resp = self.client.get("/profile")
        self.assertIn(b'>Playlists</a>', resp.data)


class TestTagsFeatureDisabled(AppTestCase):
    """Admin's instance-wide tags kill switch (isTagsEnabled): the Playlists
    page and every tag-related API endpoint 404 while it's off, but the
    tag-independent Wrapped Top 100 export branch of /playlist/export is
    unaffected (see routes/tags.py's playlistExport year branch)."""

    def setUp(self):
        self.dash = makeApp()
        self.client = self.dash.app.test_client()
        self.username = "testuser"
        self.email = "testuser@example.com"

        self.listener_patcher = patch("Database.database.Database.startListener")
        self.listener_patcher.start()

        self.dash.repo.upsertUser(self.username, self.email)
        self.dash.repo.setUserCookies(self.username, {"sp_dc": "fake_cookie"})
        self.user_db = self.dash.get_user_db(self.username, self.email)

        self.dash.repo.upsertTrack(makeTrack(trackId="t1", name="Rock Song", albumId="alb1", artistId="art1"))
        self.dash.repo.insertPlay(self.username, "t1", 1000.0, 200000)
        self.dash.repo.commit()

        self.logged_in_patcher = patch.object(self.dash, "is_user_logged_in", return_value=True)
        self.logged_in_patcher.start()

        self.dash.repo.setTagsEnabled(False)

    def tearDown(self):
        self.logged_in_patcher.stop()
        self.listener_patcher.stop()

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["email"] = self.email
            sess["username"] = self.username

    def test_playlists_page_404s(self):
        self._login()
        resp = self.client.get("/playlists")
        self.assertEqual(resp.status_code, 404)

    def test_tag_crud_apis_404(self):
        self._login()
        self.assertEqual(self.client.get("/api/tags").status_code, 404)
        self.assertEqual(self.client.post("/api/tags", json={
            "entity_type": "track", "entity_id": "t1", "tag": "workout"}).status_code, 404)
        self.assertEqual(self.client.delete("/api/tags", json={
            "entity_type": "track", "entity_id": "t1", "tag": "workout"}).status_code, 404)
        self.assertEqual(self.client.post("/api/tags/rename", json={
            "old_tag": "workout", "new_tag": "chill"}).status_code, 404)
        self.assertEqual(self.client.delete("/api/tags/workout").status_code, 404)

    def test_playlist_preview_and_tag_export_404(self):
        self._login()
        self.assertEqual(self.client.get("/api/playlists/preview?tags=workout").status_code, 404)
        self.assertEqual(self.client.get("/playlist/export?tags=workout&format=csv").status_code, 404)

    def test_nav_hides_playlists_link(self):
        self._login()
        resp = self.client.get("/profile")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'>Playlists</a>', resp.data)

    def test_wrapped_top100_export_unaffected(self):
        """The year branch (Wrapped's Top 100 export) doesn't depend on tags -
        it still runs its own validation (400 for an out-of-range year), not
        the tags kill switch's 404."""
        self._login()
        resp = self.client.get("/playlist/export?year=1900&format=csv")
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
