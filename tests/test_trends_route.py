import unittest
import time
from unittest.mock import patch
from tests._app_factory import AppTestCase


SECONDS_PER_DAY = 86400


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
        self.dash = self._makeApp()   #< registers shutdown(): get_user_db below starts real threads
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
        """The partial IS the response now - htmx swaps it straight into the
        trends row, so the {"trendsHtml": ...} envelope it used to travel in
        would land in the page as literal JSON text."""
        self._login()
        resp = self.client.get("/api/dashboard-trends")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/html")
        html = resp.get_data(as_text=True)
        self.assertNotIn("trendsHtml", html)
        self.assertIn("Obsession Song", html)

    def test_trend_card_links_artist_to_artist_page(self):
        self._login()
        html = self.client.get("/api/dashboard-trends").get_data(as_text=True)
        self.assertIn('href="/artist/art1"', html)
        self.assertIn("summary-top-artist-link", html)

    def test_the_middle_card_becomes_a_fresh_find_when_there_is_no_rediscovery(self):
        """The fixture user has no comeback at any gap tier, so the slot that
        would say "Recent Rediscovery" carries the newest arrival instead."""
        now_ts = time.time()
        self.dash.repo.upsertTrack(makeTrack(trackId="t2", name="Fresh Song",
                                             albumId="alb2", artistId="art2"))
        for i in range(3):
            self.dash.repo.insertPlay(self.username, "t2",
                                      now_ts - (4 * SECONDS_PER_DAY) + (i * 3600), 200000)
        self.dash.repo.commit()
        self._login()

        html = self.client.get("/api/dashboard-trends").get_data(as_text=True)

        self.assertIn("Fresh Find", html)
        self.assertIn("Fresh Song", html)
        self.assertIn('href="/artist/art2"', html)
        self.assertNotIn("Recent Rediscovery", html)

    def test_the_middle_card_keeps_its_empty_state_when_there_is_neither(self):
        """t1 is the obsession, and the obsession is never also the fresh find -
        so this user has nothing for the slot at all."""
        self._login()

        html = self.client.get("/api/dashboard-trends").get_data(as_text=True)

        self.assertIn("Recent Rediscovery", html)
        self.assertIn("No rediscovery matches found", html)
        self.assertNotIn("Fresh Find", html)
