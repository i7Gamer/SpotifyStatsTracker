import sqlite3
import unittest
from unittest.mock import patch
from tests._app_factory import AppTestCase


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
        self.dash = self._makeApp()   #< registers shutdown(): get_user_db below starts real threads
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

    def test_a_malformed_payload_is_a_400_not_a_500(self):
        """These three endpoints read `request.get_json(silent=True) or
        request.form` and went straight to .get(). A JSON body that is not an
        object has no .get, and a field that is not a string has no .strip() -
        both raised AttributeError inside the view, i.e. an unhandled 500 with
        a traceback in the log for what is only a badly-shaped request.

        All three sites at once: they are byte-identical, and a fix landing in
        one of a set is this repo's most-repeated defect."""
        self._login()
        endpoints = (
            (self.client.post, "/api/tags"),
            (self.client.delete, "/api/tags"),
            (self.client.post, "/api/tags/rename"),
        )
        payloads = (
            ["entity_type", "entity_id", "tag"],   #< a JSON array: no .get
            "just-a-string",                       #< a JSON scalar: no .get either
            {"entity_type": None, "entity_id": "t1", "tag": "x"},      #< null: no .strip
            {"entity_type": 7, "entity_id": "t1", "tag": "x"},         #< number: no .strip
            {"old_tag": None, "new_tag": 12},                          #< the rename pair
        )
        for send, url in endpoints:
            for payload in payloads:
                with self.subTest(url=url, payload=payload):
                    resp = send(url, json=payload)
                    self.assertEqual(resp.status_code, 400)
                    self.assertIn("error", resp.get_json())

    def test_a_repository_failure_answers_json_on_every_endpoint(self):
        """All four endpoints of one API must fail the same shape. add/remove
        wrap their repo calls and return a JSON 500; rename/delete did not, so
        a transient sqlite error there reached the client as Flask's HTML error
        page - and every caller does res.json() on it (static/js/playlists.js).
        Same fix-landed-in-some-of-N shape as the payload guard above."""
        self._login()
        endpoints = (
            (self.client.post, "/api/tags",
             {"entity_type": "track", "entity_id": "t1", "tag": "x"}, "addTag"),
            (self.client.delete, "/api/tags",
             {"entity_type": "track", "entity_id": "t1", "tag": "x"}, "removeTag"),
            (self.client.post, "/api/tags/rename",
             {"old_tag": "workout", "new_tag": "gym"}, "renameTag"),
            (self.client.delete, "/api/tags/workout", None, "deleteTag"),
        )
        for send, url, payload, repoMethod in endpoints:
            with self.subTest(url=url):
                with patch.object(type(self.dash.repo), repoMethod,
                                  side_effect=sqlite3.OperationalError("database is locked")):
                    resp = send(url, json=payload) if payload else send(url)

                self.assertEqual(resp.status_code, 500)
                self.assertIn("error", resp.get_json())

    def test_a_well_formed_form_post_still_works(self):
        """The guard must not mistake request.form for a malformed payload -
        it is a MultiDict, not a plain dict."""
        self._login()
        resp = self.client.post("/api/tags", data={
            "entity_type": "track", "entity_id": "t1", "tag": "formtag"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["tag"], "formtag")

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

    def test_a_tag_whose_name_contains_a_comma_can_be_selected(self):
        """The selection travels as one `tags` query param PER TAG. The old
        protocol joined the selection with "," and split it back server-side,
        so a tag whose NAME contains a comma - normalizeTag allows one - could
        be created and rendered as a chip but never previewed or exported: the
        split turned it into two tags that don't exist and the page reported
        0 matches for a tag the user was looking at."""
        self._login()
        self.client.post("/api/tags", json={"entity_type": "track", "entity_id": "t1", "tag": "rock, classic"})

        resp = self.client.get("/api/playlists/preview?tags=rock%2C%20classic")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["track_count"], 1)

        resp = self.client.get("/playlist/export?tags=rock%2C%20classic&format=csv")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Rock Song", resp.get_data(as_text=True))

    def test_a_multi_tag_selection_travels_as_repeated_params(self):
        """?tags=workout&tags=chill - both tags must reach the filter, not
        just the first (request.args.get reads only one value)."""
        self._login()
        self.dash.repo.upsertTrack(makeTrack(trackId="t2", name="Newer Song", albumId="alb2", artistId="art2"))
        self.dash.repo.insertPlay(self.username, "t2", 9000.0, 200000)
        self.dash.repo.commit()
        self.client.post("/api/tags", json={"entity_type": "track", "entity_id": "t1", "tag": "workout"})
        self.client.post("/api/tags", json={"entity_type": "track", "entity_id": "t2", "tag": "chill"})

        resp = self.client.get("/api/playlists/preview?tags=workout&tags=chill")
        self.assertEqual(resp.get_json()["track_count"], 2)

        body = self.client.get("/playlist/export?tags=workout&tags=chill&format=csv").get_data(as_text=True)
        self.assertIn("Rock Song", body)
        self.assertIn("Newer Song", body)

    def test_playlist_export_sort_by_recent(self):
        """Two tagged tracks with different last-played times, so the ordering
        is actually observable - with one track this passed even if sorting was
        deleted entirely."""
        self._login()
        self.dash.repo.upsertTrack(makeTrack(trackId="t2", name="Newer Song", albumId="alb2", artistId="art2"))
        self.dash.repo.insertPlay(self.username, "t2", 9000.0, 200000)   #< played after t1
        self.dash.repo.commit()
        for trackId in ("t1", "t2"):
            self.client.post("/api/tags",
                             json={"entity_type": "track", "entity_id": trackId, "tag": "workout"})

        resp = self.client.get("/playlist/export?tags=workout&format=csv&sort=recent")

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertLess(body.index("Newer Song"), body.index("Rock Song"))

    def test_playlist_export_sort_by_plays_differs_from_recent(self):
        """The two sort modes must actually produce different orders here, so
        neither test can pass on a hardcoded ordering."""
        self._login()
        self.dash.repo.upsertTrack(makeTrack(trackId="t2", name="Newer Song", albumId="alb2", artistId="art2"))
        self.dash.repo.insertPlay(self.username, "t2", 9000.0, 200000)
        for extraPlay in (2000.0, 3000.0):        #< t1 has more plays, t2 is more recent
            self.dash.repo.insertPlay(self.username, "t1", extraPlay, 200000)
        self.dash.repo.commit()
        for trackId in ("t1", "t2"):
            self.client.post("/api/tags",
                             json={"entity_type": "track", "entity_id": trackId, "tag": "workout"})

        byPlays = self.client.get("/playlist/export?tags=workout&format=csv&sort=plays").get_data(as_text=True)
        byRecent = self.client.get("/playlist/export?tags=workout&format=csv&sort=recent").get_data(as_text=True)

        self.assertLess(byPlays.index("Rock Song"), byPlays.index("Newer Song"))
        self.assertLess(byRecent.index("Newer Song"), byRecent.index("Rock Song"))

    def test_playlist_export_filename_is_header_safe(self):
        self._login()
        # A tag with a quote (injection) and a non-Latin-1 char (中, which would
        # make Werkzeug 500 when encoding the Content-Disposition header) must be
        # sanitized out of the download filename.
        resp = self.client.get('/playlist/export?tags=chill%E4%B8%AD%22&format=csv')

        self.assertEqual(resp.status_code, 200)
        cd = resp.headers["Content-Disposition"]
        # The quote must not survive into the header at all: an unescaped one
        # would close filename="..." early and let the rest be read as further
        # header parameters. (The previous assertion looked for '"out', a
        # substring the input could never produce either way.)
        self.assertEqual(cd.count('"'), 2)   #< exactly the pair around the filename
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
        self.dash = self._makeApp()   #< registers shutdown(): get_user_db below starts real threads
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


class TestTagMutationRoutes(TestTagsRoutes):
    """Rename and delete only had coverage of their feature-disabled 404s - the
    success paths (param names, URL encoding, what actually changed in the DB)
    were never driven through the routes."""

    def _tag(self, trackId, tag):
        resp = self.client.post("/api/tags",
                                json={"entity_type": "track", "entity_id": trackId, "tag": tag})
        self.assertEqual(resp.status_code, 200)

    def test_rename_updates_the_stored_tag(self):
        self._login()
        self._tag("t1", "workout")

        resp = self.client.post("/api/tags/rename", json={"old_tag": "workout", "new_tag": "gym"})

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])
        self.assertEqual(self.dash.repo.getTagsForEntity(self.username, "track", "t1"), ["gym"])

    def test_rename_onto_an_existing_tag_merges_them(self):
        """The conflict branch: UPDATE OR IGNORE then a sweep-DELETE, so the
        rows that would violate the unique key are folded into the target
        instead of erroring."""
        self._login()
        self.dash.repo.upsertTrack(makeTrack(trackId="t2", name="Other Song", albumId="alb2", artistId="art2"))
        self.dash.repo.insertPlay(self.username, "t2", 2000.0, 200000)
        self.dash.repo.commit()
        self._tag("t1", "workout")
        self._tag("t1", "gym")
        self._tag("t2", "workout")

        resp = self.client.post("/api/tags/rename", json={"old_tag": "workout", "new_tag": "gym"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.dash.repo.getTagsForEntity(self.username, "track", "t1"), ["gym"])
        self.assertEqual(self.dash.repo.getTagsForEntity(self.username, "track", "t2"), ["gym"])
        self.assertEqual([t["tag"] for t in self.dash.repo.getUserTags(self.username)], ["gym"])

    def test_delete_removes_the_tag_everywhere(self):
        self._login()
        self._tag("t1", "workout")

        resp = self.client.delete("/api/tags/workout")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["count"], 1)
        self.assertEqual(self.dash.repo.getTagsForEntity(self.username, "track", "t1"), [])

    def test_a_tag_containing_a_slash_can_be_deleted(self):
        """normalizeTag allows slashes, so such a tag could be created - but the
        default URL converter rejects them, so %2F decoded back to a slash and
        matched no rule: Delete silently 404'd and the tag was permanent."""
        self._login()
        self._tag("t1", "rock/metal")
        self.assertEqual(self.dash.repo.getTagsForEntity(self.username, "track", "t1"), ["rock/metal"])

        resp = self.client.delete("/api/tags/rock%2Fmetal")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.dash.repo.getTagsForEntity(self.username, "track", "t1"), [])

    def test_a_tag_that_normalizes_to_nothing_is_rejected(self):
        """'#' alone is truthy, so the route accepted it, stored nothing, and
        answered {"success": true, "tag": null} - telling the client a tag
        existed that never did."""
        self._login()

        resp = self.client.post("/api/tags",
                                json={"entity_type": "track", "entity_id": "t1", "tag": "#"})

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.dash.repo.getTagsForEntity(self.username, "track", "t1"), [])


if __name__ == "__main__":
    unittest.main()
