"""The Top Songs/Artists/Albums pages' htmx contract (routes/charts.py's
_topListShell/_topListResults, templates/_page_card.html,
templates/_top_list_container.html, static/js/top-list.js).

The sibling of tests/test_history_htmx.py, which carries the fuller commentary
on why the transport looks like this. What is specific to these three pages:

- one filter card and one JS module serve all three, so a break here breaks
  three pages at once - each assertion runs against all three endpoints.
- "Full plays only" cannot be a plain form field. An unchecked checkbox is
  omitted from a submission entirely, and to this route an ABSENT fullOnly means
  the DEFAULT, which is ON - so unchecking would have read as checking. The
  hidden field is what makes the off state expressible, and the tests below are
  what stop someone "simplifying" it back into a bare checkbox.

The logged-out contract for this page is NOT here. HX-Redirect instead of a
302, an empty body, and the filters preserved through the login round-trip
are one app-wide rule with one implementation (app.py's
unauthenticatedResponse), so it is asserted once, parametrized over every
htmx page, in tests/test_ajax_unauthenticated.py. Eight copies of it lived
here and in the sibling files, and only half checked all three things.
"""
import os
import sys
import unittest
from unittest.mock import ANY, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from tests._app_factory import AppTestCase

#< what htmx puts on every request it makes
HX_HEADERS = {"HX-Request": "true"}

#< every page sharing _page_card.html / top-list.js
TOP_LIST_PATHS = ("/top-songs", "/top-artists", "/top-albums")


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


class TopListHtmxTestCase(AppTestCase):
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

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["email"] = self.email
            sess["username"] = self.username

    def _shell(self, path, query=""):
        self._login()
        return self.client.get(f"{path}{query}").get_data(as_text=True)

    def _fragment(self, path, query=""):
        self._login()
        return self.client.get(f"{path}{query}", headers=HX_HEADERS)


class TestFragmentBranch(TopListHtmxTestCase):
    def test_an_hx_request_gets_html_not_json(self):
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                resp = self._fragment(path)

                self.assertEqual(resp.status_code, 200)
                self.assertIn("text/html", resp.headers.get("Content-Type", ""))
                self.assertIsNone(resp.get_json(silent=True))

    def test_the_old_json_envelope_is_gone(self):
        """The key the previous fetch() layer read. Its absence is the point:
        htmx would swap the literal JSON into the page."""
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                self.assertNotIn("resultsHtml", self._fragment(path).get_data(as_text=True))

    def test_the_fragment_is_not_a_whole_document(self):
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                body = self._fragment(path).get_data(as_text=True)

                self.assertNotIn("<html", body.lower())
                self.assertNotIn('id="searchQuery"', body)   #< the filter card stays in the shell

    def test_the_fragment_carries_the_stat_cards_and_pagination(self):
        """Swapped in as one unit - a fragment missing either leaves the page
        with no totals or no way to reach page 2."""
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                body = self._fragment(path).get_data(as_text=True)

                self.assertIn("track-summary-card", body)
                self.assertIn('class="pagination"', body)

    def test_ajax_true_alone_no_longer_triggers_the_fragment(self):
        """?ajax=true was the old marker. Without HX-Request it is just an
        unknown query param, and the page renders its shell."""
        self._login()
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                resp = self.client.get(f"{path}?ajax=true")

                self.assertIsNone(resp.get_json(silent=True))
                self.assertIn('id="topListResults"', resp.get_data(as_text=True))


class TestShell(TopListHtmxTestCase):
    def test_the_shell_does_not_contain_the_list(self):
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                body = self._shell(path)

                self.assertIn('id="topListResults"', body)
                self.assertNotIn("A Recorded Song", body)

    def test_the_shell_serves_htmx_from_this_origin(self):
        """config.py's Content-Security-Policy allows script-src 'self' only, so
        a CDN tag would be blocked and the page would never load its list."""
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                self.assertIn("js/vendor/htmx.min.js", self._shell(path))

    def test_filter_changes_replace_the_url_and_never_push_it(self):
        """An in-page filter change must not stack a history entry, so Back
        leaves the page instead of stepping through every filter tried."""
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                body = self._shell(path)

                self.assertIn('hx-replace-url="true"', body)
                self.assertNotIn("hx-push-url", body)

    def test_a_superseded_request_is_aborted(self):
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                body = self._shell(path)

                self.assertIn("hx-sync=", body)
                self.assertIn(":replace", body)

    def test_the_placeholder_triggers_the_first_load(self):
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                self.assertIn('hx-trigger="load"', self._shell(path))

    def test_enter_in_the_search_box_is_handled_by_htmx_not_a_native_submit(self):
        """Without `submit` in hx-trigger, htmx never listens for the form's
        submit event, so Enter in #searchQuery (the only enabled text field)
        falls through to the browser's native submission: a full navigation
        that PUSHES a history entry and carries an empty query string. The
        vendored htmx's shouldCancel only suppresses that native submit for a
        trigger it is actually listening for."""
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                body = self._shell(path)

                trigger = body[body.index('hx-trigger="') + len('hx-trigger="'):]
                trigger = trigger[:trigger.index('"')]
                self.assertIn("submit", [part.strip() for part in trigger.split(",")])

    def test_the_first_load_url_holds_no_unvalidated_input(self):
        """interval used to reach the template and the pagination links raw -
        the DATA was always safe (_getDateRange coerces junk), but a stale URL
        put ?interval=bogus into every link, and now into the load URL too."""
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                body = self._shell(path, "?interval=bogus&page=notanumber&sortBy=nonsense")

                self.assertNotIn("bogus", body)
                self.assertNotIn("notanumber", body)
                self.assertNotIn("nonsense", body)

    def test_the_first_load_carries_the_filters_from_a_shared_url(self):
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                body = self._shell(path, "?interval=week&q=abc&sortBy=plays")

                self.assertIn("interval=week", body)
                self.assertIn("q=abc", body)
                self.assertIn("sortBy=plays", body)


class TestFullPlaysOnlyToggle(TopListHtmxTestCase):
    """The one control that cannot be a plain form field - see the module
    docstring. These are the tests that stop it being "simplified" back."""

    def test_the_checkbox_is_backed_by_an_explicit_hidden_field(self):
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                body = self._shell(path)

                self.assertIn('id="fullOnlyValue"', body)
                self.assertIn('name="fullOnly"', body)

    def test_the_checkbox_itself_is_not_submitted(self):
        """If it carried name="fullOnly" it would submit alongside the hidden
        field and the route would read whichever came first."""
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                body = self._shell(path)
                checkbox = body[body.index('id="fullPlaysOnly"') - 60:body.index('id="fullPlaysOnly"') + 60]

                self.assertNotIn("name=", checkbox)

    def test_the_hidden_field_starts_matching_the_active_filter(self):
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                self.assertIn('id="fullOnlyValue" value="1"', self._shell(path))
                self.assertIn('id="fullOnlyValue" value="0"', self._shell(path, "?fullOnly=0"))

    def test_the_off_state_survives_into_the_first_load_url(self):
        """fullOnly=0 must be carried explicitly: its ABSENCE means the default,
        which is on - so dropping it would silently re-enable the filter."""
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                self.assertIn("fullOnly=0", self._shell(path, "?fullOnly=0"))


class TestEachRouteDelegatesToTopListPage(TopListHtmxTestCase):
    """CORE-7 (2026-09-02 review): the three Top routes are now one-line
    calls into dashboard._topListPage(username, db, section) - the shared
    _TOP_LIST_KINDS table is what tells them apart, not three copies of the
    route body. Patched on `dashboard` rather than imported from the module:
    _topListPage is a closure inside routes/charts.py's register(), and each
    route calls it as a free variable resolved from that enclosing scope, so
    patching the closure object itself would have no effect on what the
    route actually calls - dashboard._topListPage exists as a test seam for
    exactly this reason (see its assignment in register())."""

    _SECTION_BY_PATH = {
        "/top-songs": "top_songs",
        "/top-artists": "top_artists",
        "/top-albums": "top_albums",
    }

    def test_each_route_calls_topListPage_with_its_own_section(self):
        for path, section in self._SECTION_BY_PATH.items():
            with self.subTest(path=path):
                with patch.object(self.dash, "_topListPage", return_value=("", 200)) as mocked:
                    self._login()
                    resp = self.client.get(path)

                self.assertEqual(resp.status_code, 200)
                mocked.assert_called_once_with(self.username, ANY, section)


