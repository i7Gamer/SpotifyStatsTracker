"""Tag filter on /history (see routes/charts.py's historyPage and
templates/history.html), copied from the Top Songs/Artists/Albums pages (see
tests/test_top_lists_tag_filter.py, which this mirrors). Uses a real, seeded
repository rather than a MagicMock db, since the point is the actual
tag-resolution SQL end to end (Database.getEntriesCount/getEntriesFromNew/
searchEntries narrowed by getTaggedTrackIds), not just that the route calls
the right method.

The list itself arrives in a second request, which /history now makes with htmx
rather than its own fetch() code: the marker is the HX-Request header instead of
?ajax=true, and the response is the HTML fragment instead of a JSON envelope
around it. tests/test_history_htmx.py covers that transport; these tests just
have to speak it. The Top-list mirrors of this file still use ?ajax=true, since
those pages have not migrated."""
import unittest
from tests._app_factory import AppTestCase

#< what htmx puts on every request it makes; asking for the list, not the shell
HX_HEADERS = {"HX-Request": "true"}


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

        resp = self.client.get("/history?tag=roadtrip", headers=HX_HEADERS)

        body = resp.get_data(as_text=True)
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

        resp = self.client.get("/history?tag=roadtrip", headers=HX_HEADERS)

        body = resp.get_data(as_text=True)
        self.assertIn("Tagged Song", body)
        self.assertNotIn("Other Song", body)

    def test_history_tag_filter_expands_via_album_tag(self):
        """getTaggedTrackIds expands outward to a track's tagged album/artist
        (unlike getTaggedArtistIds/getTaggedAlbumIds) - see
        Database/queries/tags.py."""
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "album", "alb2")
        self.dash.repo.commit()

        resp = self.client.get("/history?tag=roadtrip", headers=HX_HEADERS)

        body = resp.get_data(as_text=True)
        self.assertIn("Other Song", body)
        self.assertNotIn("Tagged Song", body)

    def test_unknown_tag_matches_nothing(self):
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t1")
        self.dash.repo.commit()

        resp = self.client.get("/history?tag=nonexistent", headers=HX_HEADERS)

        body = resp.get_data(as_text=True)
        self.assertNotIn("Tagged Song", body)
        self.assertNotIn("Other Song", body)

    def test_tag_filter_combines_with_search(self):
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t1")
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t2")
        self.dash.repo.commit()

        resp = self.client.get("/history?tag=roadtrip&q=Tagged", headers=HX_HEADERS)

        body = resp.get_data(as_text=True)
        self.assertIn("Tagged Song", body)
        self.assertNotIn("Other Song", body)

    def test_tag_filter_persists_across_sort_change(self):
        """The tag select must survive an unrelated filter change. htmx submits
        the whole filter form on every change, so an active tag rides along in
        the request instead of having to be re-derived from the current query
        string - but the shell still has to render it selected, or the next
        submission drops it."""
        self._login()
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t1")
        self.dash.repo.commit()

        listBody = self.client.get("/history?tag=roadtrip&sort=oldest",
                                   headers=HX_HEADERS).get_data(as_text=True)
        self.assertIn("Tagged Song", listBody)
        self.assertNotIn("Other Song", listBody)

        # The tag dropdown lives in the shell; it must keep the active tag selected.
        shellBody = self.client.get("/history?tag=roadtrip&sort=oldest").get_data(as_text=True)
        self.assertIn('value="roadtrip" selected', shellBody)


if __name__ == "__main__":
    unittest.main()
