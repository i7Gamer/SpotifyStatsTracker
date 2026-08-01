"""Tag filter on /history (see routes/charts.py's historyPage and
templates/history.html), copied from the Top Songs/Artists/Albums pages (see
tests/test_top_lists_tag_filter.py, which this mirrors). Uses a real, seeded
repository rather than a MagicMock db, since the point is the actual
tag-resolution SQL end to end (Database.getEntriesCount/getEntriesFromNew/
searchEntries narrowed by getTaggedTrackIds), not just that the route calls
the right method."""
import unittest
from tests._app_factory import AppTestCase


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


class TestHistoryTagFilter(AppTestCase):
    def setUp(self):
        self.dash = self._makeApp()   #< registers shutdown(): get_user_db below starts real threads
        self.client = self.dash.app.test_client()
        self.username = "testuser"
        self.email = "testuser@example.com"

        from unittest.mock import patch
        self.listener_patcher = patch("Database.database.Database.startListener")
        self.listener_patcher.start()

        self.dash.repo.upsertUser(self.username, self.email)
        self.dash.repo.setUserCookies(self.username, {"sp_dc": "fake_cookie"})
        self.user_db = self.dash.get_user_db(self.username, self.email)

        self.dash.repo.upsertTrack(makeTrack("t1", "Tagged Song", "alb1", "Tagged Album", "art1", "Tagged Artist"))
        self.dash.repo.upsertTrack(makeTrack("t2", "Other Song", "alb2", "Other Album", "art2", "Other Artist"))
        self.dash.repo.insertPlay(self.username, "t1", 1000.0, 200000)
        self.dash.repo.insertPlay(self.username, "t2", 2000.0, 200000)
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

    def test_history_with_no_tags_hides_the_filter(self):
        self._login()
        resp = self.client.get("/history")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'id="tagFilter"', resp.data)

    def test_history_tag_filter_hidden_when_admin_disables_tags(self):
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t1")
        self.dash.repo.commit()
        self.dash.repo.setTagsEnabled(False)

        resp = self.client.get("/history")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'id="tagFilter"', resp.data)

    def test_history_tag_param_ignored_when_admin_disables_tags(self):
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t1")
        self.dash.repo.commit()
        self.dash.repo.setTagsEnabled(False)

        resp = self.client.get("/history?tag=roadtrip&ajax=true")

        body = resp.get_json()["resultsHtml"]
        self.assertIn("Tagged Song", body)
        self.assertIn("Other Song", body)   #< tag filter bypassed -> unfiltered

    def test_history_tag_filter_shows_once_a_tag_exists(self):
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t1")
        self.dash.repo.commit()

        resp = self.client.get("/history")

        self.assertIn(b'id="tagFilter"', resp.data)
        self.assertIn(b"#roadtrip (1)", resp.data)

    def test_history_filtered_by_tag_shows_only_that_song(self):
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t1")
        self.dash.repo.commit()

        resp = self.client.get("/history?tag=roadtrip&ajax=true")

        body = resp.get_json()["resultsHtml"]
        self.assertIn("Tagged Song", body)
        self.assertNotIn("Other Song", body)

    def test_history_tag_filter_expands_via_album_tag(self):
        """getTaggedTrackIds expands outward to a track's tagged album/artist
        (unlike getTaggedArtistIds/getTaggedAlbumIds) - see
        Database/queries/tags.py."""
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "album", "alb2")
        self.dash.repo.commit()

        resp = self.client.get("/history?tag=roadtrip&ajax=true")

        body = resp.get_json()["resultsHtml"]
        self.assertIn("Other Song", body)
        self.assertNotIn("Tagged Song", body)

    def test_unknown_tag_matches_nothing(self):
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t1")
        self.dash.repo.commit()

        resp = self.client.get("/history?tag=nonexistent&ajax=true")

        body = resp.get_json()["resultsHtml"]
        self.assertNotIn("Tagged Song", body)
        self.assertNotIn("Other Song", body)

    def test_tag_filter_combines_with_search(self):
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t1")
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t2")
        self.dash.repo.commit()

        resp = self.client.get("/history?tag=roadtrip&q=Tagged&ajax=true")

        body = resp.get_json()["resultsHtml"]
        self.assertIn("Tagged Song", body)
        self.assertNotIn("Other Song", body)

    def test_tag_filter_persists_across_sort_change(self):
        """The tag select must survive an unrelated filter change - see
        static/js/history-page.js's updateHistoryTagFilter/replaceHistoryUrl
        convention: every URL-mutating function starts from the current query
        string, so an active tag= must remain selected rather than silently
        reset."""
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t1")
        self.dash.repo.commit()

        ajaxBody = self.client.get("/history?tag=roadtrip&sort=oldest&ajax=true").get_data(as_text=True)
        self.assertIn("Tagged Song", ajaxBody)
        self.assertNotIn("Other Song", ajaxBody)

        # The tag dropdown lives in the shell; it must keep the active tag selected.
        shellBody = self.client.get("/history?tag=roadtrip&sort=oldest").get_data(as_text=True)
        self.assertIn('value="roadtrip" selected', shellBody)


if __name__ == "__main__":
    unittest.main()
