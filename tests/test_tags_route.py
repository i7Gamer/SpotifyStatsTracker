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

    def test_playlists_page_renders(self):
        self._login()
        resp = self.client.get("/playlists")
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
