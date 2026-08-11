"""Phase 6: a merged song has one page, and that page says so.

Phase 5 made the global lists count a merged song once, which leaves two loose
ends the pages have to tie off. A link to the version that lost the election
still exists - in a bookmark, in a share, in someone's history - and it has to
land somewhere sensible rather than on a page whose numbers no longer add up.
And the merge is invisible until the page admits it happened: a play count that
silently spans two releases is indistinguishable from a wrong one.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase

SINGLE = "A" * 22
ALBUM_CUT = "B" * 22
OTHER = "C" * 22


class TestOtherReleasesOfTheSameSong(DatabaseTestCase):
    def _seed(self, db, merge=True):
        conn = db.repo._conn()
        with conn:
            for albumId, name in (("albSingle", "The Single"), ("albLP", "The Album")):
                conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES (?, ?, '')",
                             (albumId, name))
            for trackId, albumId in ((SINGLE, "albSingle"), (ALBUM_CUT, "albLP")):
                conn.execute("INSERT INTO tracks (id, name, url, album_id, duration_ms) "
                             "VALUES (?, 'Shared Song', '', ?, 200000)", (trackId, albumId))
            if merge:
                conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?", (ALBUM_CUT, SINGLE))
        return db

    def test_the_canonical_lists_the_releases_it_absorbed(self):
        db = self._seed(self._makeDb({}, []))

        releases = db.repo.getMergedReleases(ALBUM_CUT)

        self.assertEqual([r["trackId"] for r in releases], [SINGLE])
        self.assertEqual(releases[0]["album"]["id"], "albSingle")
        self.assertEqual(releases[0]["album"]["name"], "The Single")

    def test_an_unmerged_song_has_no_other_releases(self):
        db = self._seed(self._makeDb({}, []), merge=False)

        self.assertEqual(db.repo.getMergedReleases(ALBUM_CUT), [])

    def test_asking_about_a_merged_track_answers_about_its_canonical(self):
        """So the detail page does not have to know which of the two it is
        holding before it can ask."""
        db = self._seed(self._makeDb({}, []))

        releases = db.repo.getMergedReleases(SINGLE)

        self.assertEqual([r["trackId"] for r in releases], [SINGLE])


class TestCanonicalResolution(DatabaseTestCase):
    def _seed(self, db):
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES ('alb', 'A', '')")
            for trackId in (SINGLE, ALBUM_CUT):
                conn.execute("INSERT INTO tracks (id, name, url, album_id, duration_ms) "
                             "VALUES (?, 'Shared Song', '', 'alb', 200000)", (trackId,))
            conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?", (ALBUM_CUT, SINGLE))
        return db

    def test_a_merged_id_resolves_to_the_canonical(self):
        db = self._seed(self._makeDb({}, []))

        self.assertEqual(db.repo.resolveCanonicalTrackId(SINGLE), ALBUM_CUT)

    def test_a_canonical_id_resolves_to_itself(self):
        db = self._seed(self._makeDb({}, []))

        self.assertEqual(db.repo.resolveCanonicalTrackId(ALBUM_CUT), ALBUM_CUT)

    def test_an_unknown_id_resolves_to_itself(self):
        """The caller 404s on it a moment later; this must not swallow that."""
        db = self._makeDb({}, [])

        self.assertEqual(db.repo.resolveCanonicalTrackId("nosuchtrack"), "nosuchtrack")


class TestTheSongPageRedirectsAMergedId(unittest.TestCase):
    """A link to the version that lost the election still exists - in a
    bookmark, a share, someone's own history - and has to land on the page whose
    numbers the rest of the site is already showing."""

    def _page(self, canonicalId, headers=None, query=""):
        from unittest.mock import MagicMock, patch
        from _app_factory import AppTestCase

        case = AppTestCase()
        case.setUp()
        self.addCleanup(case.tearDown)
        dash = case._makeApp()
        db = MagicMock()
        #< getSong answers about the whole group now: for a merged id it
        #  returns the CANONICAL row, and the route redirects on the mismatch
        #  between the id asked for and the id answered with
        db.getSong.return_value = {"id": canonicalId or SINGLE, "name": "Shared Song",
                                   "canonicalId": None,
                                   "artists": [], "album": {"id": "alb", "name": "A"}}
        client = dash.app.test_client()
        with patch.object(dash, "is_user_logged_in", return_value=True), \
             patch.object(dash, "get_username_for_email", return_value="alice"), \
             patch.object(dash, "get_user_db", return_value=db):
            with client.session_transaction() as sess:
                sess["email"] = "alice@example.com"
            return client.get(f"/song/{SINGLE}{query}", headers=headers or {})

    def test_a_merged_id_redirects_to_its_canonical(self):
        resp = self._page(ALBUM_CUT)

        self.assertEqual(resp.status_code, 302)
        self.assertIn(ALBUM_CUT, resp.headers["Location"])

    def test_an_unmerged_song_renders_in_place(self):
        resp = self._page(None)

        self.assertEqual(resp.status_code, 200)

    def test_an_htmx_request_is_never_redirected(self):
        """A redirect mid-swap replaces a region with a whole page - the same
        trap _missingEntityResponse documents. The shell has already resolved
        the id by the time the body is fetched."""
        resp = self._page(ALBUM_CUT, headers={"HX-Request": "true"})

        self.assertNotEqual(resp.status_code, 302)

    def test_the_query_string_survives_the_redirect(self):
        """Landing on the canonical must not silently drop the view someone
        linked to."""
        resp = self._page(ALBUM_CUT, query="?view=history")

        self.assertIn("view=history", resp.headers["Location"])

    def test_a_query_param_that_collides_with_the_route_does_not_500(self):
        """The query string is splatted into url_for beside an explicit
        track_id, so a param of that name reaches it twice - TypeError, an
        unhandled 500 on a URL anyone can construct off an "Also released on"
        link. The redirect must still name the canonical, from the route's
        answer rather than from whatever the caller sent."""
        resp = self._page(ALBUM_CUT, query="?track_id=" + OTHER)

        self.assertEqual(resp.status_code, 302)
        self.assertIn(ALBUM_CUT, resp.headers["Location"])
        self.assertNotIn(OTHER, resp.headers["Location"])

    def test_a_reserved_url_for_keyword_does_not_500_either(self):
        """Same splat, the other half: url_for's keyword-only _method/_scheme/
        _external/_anchor are not query params, and binding one sends the URL
        builder looking for a rule this GET-only endpoint has not got.

        `endpoint` is the one that is not underscored - it is url_for's first
        POSITIONAL parameter, already supplied here as "songDetailPage", so a
        query param of that name is the same TypeError as ?track_id= and was
        the case the underscore filter could not reach."""
        for param in ("_method=POST", "_external=1", "_scheme=gopher",
                      "endpoint=songDetailPage", "endpoint=nonexistent"):
            resp = self._page(ALBUM_CUT, query="?" + param)

            self.assertEqual(resp.status_code, 302, param)
            self.assertIn(ALBUM_CUT, resp.headers["Location"], param)


if __name__ == "__main__":
    unittest.main()


class TestTheSplitButton(unittest.TestCase):
    """The admin-only exit from a wrong merge, on the page where the merge is
    visible. Splitting records a manual verdict (unmergeTrack), which is the
    row the matcher refuses to overrule - so it holds across every later pass
    and across the toggle being cycled."""

    def _post(self, isAdmin=True, loggedIn=True, manager=None, unmergeError=None):
        from unittest.mock import MagicMock, patch
        from _app_factory import AppTestCase

        case = AppTestCase()
        case.setUp()
        self.addCleanup(case.tearDown)
        dash = case._makeApp()
        dash.repo.isAdmin = MagicMock(return_value=isAdmin)
        dash.repo.resolveCanonicalTrackId = MagicMock(return_value=ALBUM_CUT)
        dash.repo.unmergeTrack = MagicMock(side_effect=unmergeError)
        if manager is not None:
            #< attached BEFORE the request: a manager only records calls made
            #  after attachment, so attaching afterwards asserts on nothing
            manager.attach_mock(dash.repo.resolveCanonicalTrackId, "resolve")
            manager.attach_mock(dash.repo.unmergeTrack, "unmerge")
        client = dash.app.test_client()
        with patch.object(dash, "is_user_logged_in", return_value=loggedIn), \
             patch.object(dash, "get_username_for_email", return_value="alice"), \
             patch.object(dash, "get_user_db", return_value=MagicMock()):
            if loggedIn:
                with client.session_transaction() as sess:
                    sess["email"] = "alice@example.com"
            resp = client.post(f"/admin/split_track/{SINGLE}")
        return resp, dash

    def test_it_splits_and_lands_on_the_canonical_page(self):
        resp, dash = self._post()

        dash.repo.unmergeTrack.assert_called_once_with(SINGLE, decidedBy="alice")
        self.assertEqual(resp.status_code, 302)
        self.assertIn(ALBUM_CUT, resp.headers["Location"])

    def test_the_canonical_is_resolved_before_the_split(self):
        """Afterwards the member resolves to itself, and the admin would land
        on the freshly split member instead of the page they were on. Pinned by
        call order on a shared manager, since "before" is the whole point."""
        from unittest.mock import MagicMock, call
        manager = MagicMock()
        resp, dash = self._post(manager=manager)

        self.assertEqual(manager.mock_calls,
                         [call.resolve(SINGLE), call.unmerge(SINGLE, decidedBy="alice")])

    def test_a_non_admin_is_refused(self):
        resp, dash = self._post(isAdmin=False)

        self.assertEqual(resp.status_code, 403)
        dash.repo.unmergeTrack.assert_not_called()

    def test_an_unknown_track_is_a_400_not_a_crash(self):
        """unmergeTrack refuses ids that are not tracks (ValueError); the
        route answers 400 the way the review queue's verdict routes do,
        instead of letting the raise become a 500."""
        resp, dash = self._post(unmergeError=ValueError("unknown track"))

        self.assertEqual(resp.status_code, 400)
