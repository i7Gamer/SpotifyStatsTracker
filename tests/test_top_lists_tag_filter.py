"""Tag filter on the Top Songs/Artists/Albums pages (see routes/charts.py's
topSongsPage/topArtistsPage/topAlbumsPage and templates/_page_card.html).
Uses a real, seeded repository (like test_tags_route.py) rather than a
MagicMock db, since the point of these tests is the actual tag-resolution SQL
end to end, not just that the route calls the right method."""
import unittest
from unittest.mock import patch
from tests._app_factory import AppTestCase, makeApp


def makeTrack(trackId, name, albumId, albumName, artistId, artistName):
    return {
        "id": trackId,
        "name": name,
        "url": f"http://example.com/track/{trackId}",
        "artists": [
            {"id": artistId, "name": artistName, "url": f"http://example.com/artist/{artistId}",
             "imageUrl": "", "imageId": artistId},
        ],
        "album": {
            "id": albumId, "name": albumName, "url": f"http://example.com/album/{albumId}",
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


class TestTopListsTagFilter(AppTestCase):
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

        self.dash.repo.upsertTrack(makeTrack("t1", "Tagged Song", "alb1", "Tagged Album", "art1", "Tagged Artist"))
        self.dash.repo.upsertTrack(makeTrack("t2", "Other Song", "alb2", "Other Album", "art2", "Other Artist"))
        self.dash.repo.insertPlay(self.username, "t1", 1000.0, 200000)
        self.dash.repo.insertPlay(self.username, "t2", 1000.0, 200000)
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

    def test_top_songs_with_no_tags_hides_the_filter(self):
        self._login()
        resp = self.client.get("/top-songs")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'id="tagFilter"', resp.data)

    def test_top_songs_tag_filter_hidden_when_admin_disables_tags(self):
        """The admin's instance-wide tags kill switch (isTagsEnabled) hides the
        filter even for a user who already has tags - see app.py's
        _injectTagsStatus and _page_card.html's {% if tags_enabled and
        user_tags %} guard."""
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t1")
        self.dash.repo.commit()
        self.dash.repo.setTagsEnabled(False)

        resp = self.client.get("/top-songs")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'id="tagFilter"', resp.data)

    def test_top_songs_tag_filter_shows_once_a_tag_exists(self):
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t1")
        self.dash.repo.commit()

        resp = self.client.get("/top-songs")

        self.assertIn(b'id="tagFilter"', resp.data)
        self.assertIn(b"#roadtrip (1)", resp.data)

    def test_top_songs_filtered_by_tag_shows_only_that_song(self):
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t1")
        self.dash.repo.commit()

        resp = self.client.get("/top-songs?tag=roadtrip")

        body = resp.get_data(as_text=True)
        self.assertIn("Tagged Song", body)
        self.assertNotIn("Other Song", body)

    def test_top_artists_filtered_by_tag_matches_direct_tag_only(self):
        """Tagging the track (not the artist) must NOT surface it here - see
        Database/queries/tags.py's getTaggedArtistIds direct-tag-only note."""
        self._login()
        self.dash.repo.addTag(self.username, "favorites", "artist", "art1")
        self.dash.repo.addTag(self.username, "favorites", "track", "t2")   # should not leak into artists
        self.dash.repo.commit()

        resp = self.client.get("/top-artists?tag=favorites")

        body = resp.get_data(as_text=True)
        self.assertIn("Tagged Artist", body)
        self.assertNotIn("Other Artist", body)

    def test_top_albums_filtered_by_tag_matches_direct_tag_only(self):
        self._login()
        self.dash.repo.addTag(self.username, "favorites", "album", "alb1")
        self.dash.repo.addTag(self.username, "favorites", "track", "t2")   # should not leak into albums
        self.dash.repo.commit()

        resp = self.client.get("/top-albums?tag=favorites")

        body = resp.get_data(as_text=True)
        self.assertIn("Tagged Album", body)
        self.assertNotIn("Other Album", body)

    def test_unknown_tag_matches_nothing(self):
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t1")
        self.dash.repo.commit()

        resp = self.client.get("/top-songs?tag=nonexistent")

        body = resp.get_data(as_text=True)
        self.assertNotIn("Tagged Song", body)
        self.assertNotIn("Other Song", body)

    def test_tag_filter_persists_across_sort_change(self):
        """The tag select must survive an unrelated filter change - see
        _page_card.html's updateFilters()/updateTagFilter() convention: every
        URL-mutating function starts from the current query string, so an
        active tag= must remain selected rather than silently reset."""
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t1")
        self.dash.repo.commit()

        resp = self.client.get("/top-songs?tag=roadtrip&sortBy=name")

        body = resp.get_data(as_text=True)
        self.assertIn("Tagged Song", body)
        self.assertNotIn("Other Song", body)
        self.assertIn('value="roadtrip" selected', body)


if __name__ == "__main__":
    unittest.main()
