import unittest
import time
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


class TestTrendsRoute(AppTestCase):
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

        # Seed track and plays for obsession
        now_ts = time.time()
        self.dash.repo.upsertTrack(makeTrack(trackId="t1", name="Obsession Song"))
        for i in range(6):
            self.dash.repo.insertPlay(self.username, "t1", now_ts - (i * 3600), 200000)

        self.dash.repo.commit()

    def _login(self):
        self.logged_in_patcher = patch.object(self.dash, "is_user_logged_in", return_value=True)
        self.logged_in_patcher.start()
        with self.client.session_transaction() as sess:
            sess["email"] = self.email
            sess["username"] = self.username

    def tearDown(self):
        if hasattr(self, "logged_in_patcher"):
            self.logged_in_patcher.stop()
        self.listener_patcher.stop()

    def test_dashboard_trends_unauthorized(self):
        resp = self.client.get("/api/dashboard-trends")
        self.assertEqual(resp.status_code, 401)

    def test_dashboard_trends_authorized(self):
        self._login()
        resp = self.client.get("/api/dashboard-trends")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("trendsHtml", data)
        self.assertIn("Obsession Song", data["trendsHtml"])
