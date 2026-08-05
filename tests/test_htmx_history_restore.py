"""A history-restore request is a page load, not a swap.

Every migrated route decides "fragment or whole page" on the presence of the
``HX-Request`` header. htmx sends that header on one more kind of request than
the migration accounted for.

When the user presses Back to an htmx-managed history entry, htmx first looks
the URL up in its own snapshot cache. On a MISS it re-fetches the page - and in
2.0.9 the vendored build ships ``historyRestoreAsHxRequest: true``, so that
fetch carries ``HX-Request: true`` alongside ``HX-History-Restore-Request:
true``. The response is then swapped into the *history element*, which is
``document.body`` here (nothing carries ``hx-history-elt``).

So the header alone means the restore of any migrated page would put a bare
fragment - a list, a card, a play log - in place of the entire document: no
chrome, no nav, no scripts. The page would look destroyed and a reload would fix
it, which is the shape of bug that gets reported as "it randomly breaks".

The cache makes it rare rather than impossible, and only rare by luck: an entry
is evicted after ``historyCacheSize`` (10) other URLs, ``sessionStorage`` can
refuse the write when a page snapshot is large (htmx then drops entries until it
fits, and stores nothing if even one will not), and a browser that clears
session storage misses every time.

Fixing it client-side would mean setting ``historyRestoreAsHxRequest: false``
and trusting every future htmx upgrade not to flip a default back. The server
knows the answer for certain, so the guard lives here: ``HX-History-Restore-
Request`` means a full render, whatever else the request carries.

Note this is exactly why ``htmx.config.historyCacheSize = 0`` would have been
the WRONG way to keep page snapshots out of session storage - it does not
disable restoring, it makes every restore a cache miss, turning a rare bug into
a guaranteed one.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase

#< what htmx puts on every request it makes
HX_HEADERS = {"HX-Request": "true"}
#< ...and the extra one it adds when the request is a Back/Forward restore
HX_RESTORE_HEADERS = dict(HX_HEADERS, **{"HX-History-Restore-Request": "true"})

# Every page htmx drives, same roster as tests/test_ajax_unauthenticated.py.
# /shared/<token> is absent for the same reason it is absent there: it is not a
# session-backed page.
HTMX_PATHS = ("/", "/charts", "/genres", "/history", "/top-songs", "/top-artists",
              "/top-albums", "/wrapped", "/compare", "/song/t1", "/artist/art1",
              "/album/alb1")


def makeTrack(trackId, name):
    return {
        "id": trackId,
        "name": name,
        "url": f"http://example.com/track/{trackId}",
        "artists": [{"id": "art1", "name": "An Artist", "url": "http://example.com/artist/art1",
                     "imageUrl": "", "imageId": "art1"}],
        "album": {
            "id": "alb1", "name": "An Album", "url": "http://example.com/album/alb1",
            "imageId": "alb1", "imageUrl": "http://img.example.com/a.jpg",
            "totalTracks": 10, "releaseDate": 12345.0,
        },
        "imageUrl": "http://img.example.com/a.jpg",
        "imageId": "alb1",
        "duration": 200000,
        "explicit": False,
        "isrc": "US1234567890",
        "discNumber": 1,
        "trackNumber": 3,
        "releaseDate": 12345.0,
    }


class HistoryRestoreTestCase(AppTestCase):
    def setUp(self):
        self.dash = self._makeApp()
        self.client = self.dash.app.test_client()
        self.username = "testuser"
        self.email = "testuser@example.com"

        self.listener_patcher = patch("Database.database.Database.startListener")
        self.listener_patcher.start()
        self.addCleanup(self.listener_patcher.stop)

        self.dash.repo.upsertUser(self.username, self.email)
        self.dash.repo.setUserCookies(self.username, {"sp_dc": "fake_cookie"})
        self.user_db = self.dash.get_user_db(self.username, self.email)

        self.dash.repo.upsertTrack(makeTrack("t1", "A Recorded Song"))
        self.dash.repo.insertPlay(self.username, "t1", 1000.0, 200000)
        self.dash.repo.commit()

        self.logged_in_patcher = patch.object(self.dash, "is_user_logged_in", return_value=True)
        self.logged_in_patcher.start()
        self.addCleanup(self.logged_in_patcher.stop)

        with self.client.session_transaction() as sess:
            sess["email"] = self.email
            sess["username"] = self.username


class TestARestoreGetsAWholeDocument(HistoryRestoreTestCase):
    def test_every_htmx_page_answers_a_restore_with_a_full_page(self):
        """The response replaces document.body, so a fragment here erases the
        site chrome on Back."""
        for path in HTMX_PATHS:
            with self.subTest(path=path):
                body = self.client.get(
                    path, headers=HX_RESTORE_HEADERS).get_data(as_text=True)

                self.assertIn("<html", body.lower(),
                              f"{path} answered a history restore with a fragment")

    def test_the_restored_page_still_carries_the_site_chrome(self):
        """What "full page" is actually protecting: a fragment technically has
        markup too, so asserting on <html> alone could pass on a fluke."""
        body = self.client.get("/history", headers=HX_RESTORE_HEADERS).get_data(as_text=True)

        self.assertIn("<body", body.lower())
        self.assertIn("</html>", body.lower())

    def test_a_restore_keeps_the_filters_in_the_url(self):
        """The restored URL is the one the user was on, so the page has to come
        back in the state its query string describes - the whole point of
        hx-replace-url writing that state into the address bar."""
        body = self.client.get("/top-songs?interval=year&sortBy=plays",
                               headers=HX_RESTORE_HEADERS).get_data(as_text=True)

        self.assertIn("<html", body.lower())
        self.assertIn('value="year"', body)


class TestAnOrdinarySwapIsUnaffected(HistoryRestoreTestCase):
    """The guard keys on the restore header alone, so nothing else may move."""

    def test_every_htmx_page_still_answers_a_plain_swap_with_a_fragment(self):
        for path in HTMX_PATHS:
            with self.subTest(path=path):
                body = self.client.get(path, headers=HX_HEADERS).get_data(as_text=True)

                self.assertNotIn("<html", body.lower(),
                                 f"{path} answered an ordinary swap with a whole page")

    def test_a_plain_page_load_is_still_a_whole_page(self):
        for path in HTMX_PATHS:
            with self.subTest(path=path):
                body = self.client.get(path).get_data(as_text=True)

                self.assertIn("<html", body.lower())


if __name__ == "__main__":
    unittest.main()
