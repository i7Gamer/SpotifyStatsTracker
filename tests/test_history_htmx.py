"""The /history page's htmx contract (routes/charts.py historyPage,
templates/history.html, static/js/history-page.js).

/history is the first page moved off the hand-rolled fetch/swap layer, so these
pin the seam the rest of the migration will copy:

- the second phase answers an ``HX-Request`` header with an HTML FRAGMENT, where
  it used to answer ``?ajax=true`` with ``{"resultsHtml": ...}``. htmx swaps
  response bodies, so a JSON envelope would land in the page verbatim.
- a logged-out swap gets ``HX-Redirect``, not a 302. htmx follows a redirect
  transparently exactly like fetch() did, so the old failure - the login page
  parsed as data - comes back in a new costume without this.
- the URL updates via ``hx-replace-url``, never ``hx-push-url``. An in-page
  filter change must not stack a history entry: Back has to leave the page
  rather than step backwards through every filter the user tried.

The list CONTENT (tag/search/sort/pagination correctness) is covered by
tests/test_history_tag_filter.py against the same fragment branch; what is
here is the transport.

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
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from tests._app_factory import AppTestCase

#< what htmx puts on every request it makes
HX_HEADERS = {"HX-Request": "true"}

REPO_ROOT = Path(__file__).resolve().parent.parent


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


class HistoryHtmxTestCase(AppTestCase):
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

    def _loggedInShell(self):
        self._login()
        return self.client.get("/history").get_data(as_text=True)


class TestFragmentBranch(HistoryHtmxTestCase):
    def test_an_hx_request_gets_html_not_json(self):
        self._login()

        resp = self.client.get("/history", headers=HX_HEADERS)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        self.assertIsNone(resp.get_json(silent=True))

    def test_the_fragment_is_the_list_itself(self):
        self._login()

        body = self.client.get("/history", headers=HX_HEADERS).get_data(as_text=True)

        self.assertIn("A Recorded Song", body)
        self.assertIn('class="track-list"', body)

    def test_the_fragment_carries_the_pagination_strip(self):
        """It is swapped in as one unit with the list - a fragment without it
        leaves the page with no way to reach page 2."""
        self._login()

        body = self.client.get("/history", headers=HX_HEADERS).get_data(as_text=True)

        self.assertIn('class="pagination"', body)

    def test_the_fragment_is_not_a_whole_document(self):
        """htmx puts this response INSIDE #historyResults. A full page here
        would nest a second <html>/<body> and the site chrome inside the list."""
        self._login()

        body = self.client.get("/history", headers=HX_HEADERS).get_data(as_text=True)

        self.assertNotIn("<html", body.lower())
        self.assertNotIn("<body", body.lower())

    def test_the_old_json_envelope_is_gone(self):
        """The key the previous fetch() layer read. Its absence is the point:
        htmx would swap the literal JSON into the page."""
        self._login()

        body = self.client.get("/history", headers=HX_HEADERS).get_data(as_text=True)

        self.assertNotIn("resultsHtml", body)

    def test_ajax_true_alone_no_longer_triggers_the_fragment(self):
        """?ajax=true was the old marker. Without HX-Request it is just an
        unknown query param, and the page renders its shell."""
        self._login()

        resp = self.client.get("/history?ajax=true")

        self.assertIsNone(resp.get_json(silent=True))
        self.assertIn('id="historyResults"', resp.get_data(as_text=True))


class TestShell(HistoryHtmxTestCase):
    def test_a_plain_get_returns_the_shell_without_the_list(self):
        self._login()

        body = self.client.get("/history").get_data(as_text=True)

        self.assertIn('id="historyResults"', body)
        self.assertNotIn("A Recorded Song", body)

    def test_the_shell_serves_htmx_from_this_origin(self):
        """config.py's Content-Security-Policy allows script-src 'self' only, so
        a CDN tag would be blocked and the page would silently never load its
        list. See NOTICE for the pinned version."""
        body = self._loggedInShell()

        self.assertIn("js/vendor/htmx.min.js", body)

    def test_the_shell_wires_the_swap_target(self):
        body = self._loggedInShell()

        self.assertIn('hx-target="#historyResults"', body)
        self.assertIn('hx-swap="innerHTML"', body)

    def test_filter_changes_replace_the_url_and_never_push_it(self):
        """The invariant the hand-written replaceHistoryUrl existed for: an
        in-page filter change must not stack a history entry, so Back leaves
        the page instead of stepping through every filter that was tried."""
        body = self._loggedInShell()

        self.assertIn('hx-replace-url="true"', body)
        self.assertNotIn("hx-push-url", body)

    def test_a_superseded_request_is_aborted(self):
        """hx-sync ...:replace is what the AbortController bookkeeping in the
        old loadHistoryResults did by hand - without it, a slow first response
        can land on top of a newer one."""
        body = self._loggedInShell()

        self.assertIn("hx-sync=", body)
        self.assertIn(":replace", body)

    def test_every_swap_dims_the_list_while_it_loads(self):
        """The indicator has to sit on the container, not only on the filter
        form: a boosted pagination link would otherwise mark ITSELF as busy and
        the list would sit there looking settled while the next page loaded.
        #historyResults.htmx-request is the fade (see static/css/style.css)."""
        body = self._loggedInShell()

        self.assertEqual(body.count('hx-indicator="#historyResults"'), 2)

    def test_the_placeholder_triggers_the_first_load(self):
        """The shell renders empty; something has to ask for the list."""
        body = self._loggedInShell()

        self.assertIn('hx-trigger="load"', body)

    def test_the_first_load_carries_the_filters_from_a_shared_url(self):
        """A link to /history?interval=week&q=abc has to open on THAT list, not
        on an unfiltered one that then disagrees with the controls above it."""
        self._login()

        body = self.client.get("/history?interval=week&q=abc").get_data(as_text=True)

        self.assertIn("interval=week", body)
        self.assertIn("q=abc", body)

    def test_the_first_load_url_holds_no_unvalidated_input(self):
        """The route coerces a junk interval for the query; echoing the raw one
        into the load URL would assert it again in the markup and disagree with
        the pagination links, which are built from the validated value."""
        self._login()

        body = self.client.get("/history?interval=bogus&page=notanumber").get_data(as_text=True)

        self.assertNotIn("bogus", body)
        self.assertNotIn("notanumber", body)

    def test_the_first_load_drops_a_range_that_is_not_in_effect(self):
        """Matching the disabled date inputs: an explicit range only scopes the
        list while Time Period is on Custom, so carrying the dates otherwise
        would ask for a list the form says is not selected."""
        self._login()

        body = self.client.get("/history?interval=week&startDate=2026-01-01"
                               "&endDate=2026-02-01").get_data(as_text=True)

        self.assertNotIn("startDate=2026-01-01", body)


class TestFullPlaysOnlyToggle(HistoryHtmxTestCase):
    """The "Full plays only" checkbox, which cannot be a plain form field -
    mirrors tests/test_top_lists_htmx.py's class of the same name, because both
    pages now render the same partial (templates/_full_plays_toggle.html).

    What the filter actually SELECTS is in tests/test_history_full_plays.py;
    these are the markup invariants that stop it being "simplified" back."""

    def test_the_checkbox_is_backed_by_an_explicit_hidden_field(self):
        body = self._loggedInShell()

        self.assertIn('id="fullOnlyValue"', body)
        self.assertIn('name="fullOnly"', body)

    def test_the_checkbox_itself_is_not_submitted(self):
        """If it carried name="fullOnly" it would submit alongside the hidden
        field and the route would read whichever came first."""
        body = self._loggedInShell()
        checkbox = body[body.index('id="fullPlaysOnly"') - 60:body.index('id="fullPlaysOnly"') + 60]

        self.assertNotIn("name=", checkbox)

    def test_the_hidden_field_starts_matching_the_active_filter(self):
        self._login()

        self.assertIn('id="fullOnlyValue" value="1"',
                      self.client.get("/history").get_data(as_text=True))
        self.assertIn('id="fullOnlyValue" value="0"',
                      self.client.get("/history?fullOnly=0").get_data(as_text=True))

    def test_the_handler_it_calls_ships_with_this_page(self):
        """tests/test_inline_handler_targets.py resolves inline handlers against
        EVERY file in static/js, so a handler living only in top-list.js - which
        /history does not load - passes that scan and fails silently in the
        browser. This page loads htmx-filters.js, so that is where it has to be."""
        body = self._loggedInShell()
        self.assertIn("updateFullPlaysFilter()", body)

        source = (REPO_ROOT / "static" / "js" / "htmx-filters.js").read_text(encoding="utf-8")
        self.assertIn("window.updateFullPlaysFilter", source)

    def test_the_toggle_does_not_add_a_second_load_indicator(self):
        """Guards the exact count test_every_swap_dims_the_list_while_it_loads
        asserts - the new markup must not carry hx-indicator of its own."""
        self.assertEqual(self._loggedInShell().count('hx-indicator="#historyResults"'), 2)


