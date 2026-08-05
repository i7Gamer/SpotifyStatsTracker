"""The dashboard's htmx contract (routes/charts.py's dashboardIndex,
dashboardTrends and dashboardDiscover; templates/tracks.html;
static/js/dashboard-page.js).

The sibling of tests/test_history_htmx.py, which carries the fuller commentary
on why the transport looks like this. What is specific to this page:

- it is not a shell. The filtered cards are rendered inline on the first GET, so
  there is no first-load placeholder for them - only a filter CHANGE is a second
  request.
- it had four fetches, and they did not all move. Three did: the filter swap,
  and the two cards deferred past first paint because their queries scan the
  whole history. The fourth is the 15s now-playing poll, which stays hand-written
  - see TestTheNowPlayingPollStaysHandWritten below for why, and dashboard-page.js's
  header for the same note at the source.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase
from test_dashboard_cards import _DashboardHelpers, coverageDict

#< what htmx puts on every request it makes
HX_HEADERS = {"HX-Request": "true"}


class DashboardHtmxTestCase(_DashboardHelpers, AppTestCase):
    def _get(self, dash, db, path="/", headers=None):
        client = dash.app.test_client()
        with patch.object(dash, "is_user_logged_in", return_value=True), \
             patch.object(dash, "get_username_for_email", return_value="alice"), \
             patch.object(dash, "get_user_db", return_value=db):
            with client.session_transaction() as sess:
                sess["email"] = "alice@example.com"
            return client.get(path, headers=headers)

    def _page(self, path="/", db=None):
        return self._get(self._makeApp(), db or self._makeDb(), path).get_data(as_text=True)

    def _swap(self, path="/", db=None, dash=None):
        return self._get(dash or self._makeApp(), db or self._makeDb(), path, headers=HX_HEADERS)


class TestSummarySwap(DashboardHtmxTestCase):
    def test_an_hx_request_gets_html_not_json(self):
        resp = self._swap()

        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        self.assertIsNone(resp.get_json(silent=True))

    def test_the_old_json_envelope_is_gone(self):
        """The key the previous fetch() layer read. Its absence is the point:
        htmx would swap the literal JSON into the page."""
        self.assertNotIn("summaryHtml", self._swap().get_data(as_text=True))

    def test_the_fragment_is_only_the_filtered_cards(self):
        body = self._swap().get_data(as_text=True)

        self.assertNotIn("<html", body.lower())
        self.assertNotIn('id="interval"', body)          #< the filter card stays put
        self.assertNotIn("dashboard-live", body)         #< so does the unfiltered panel

    def test_the_swap_reruns_only_the_filtered_query(self):
        """The point of the split: the live cards, the calendar and the
        milestones below are unfiltered, so a filter change must not pay for
        them again."""
        dash = self._makeApp()
        db = self._makeDb()

        self._swap(db=db, dash=dash)

        db.getOverallStats.assert_called_once()
        db.getCurrentStreak.assert_not_called()
        db.getOnThisDay.assert_not_called()
        db.getListeningCalendar.assert_not_called()

    def test_ajax_true_alone_no_longer_triggers_the_fragment(self):
        """?ajax=true was the old marker. Without HX-Request it is just an
        unknown query param, and the page renders in full."""
        resp = self._get(self._makeApp(), self._makeDb(), "/?ajax=true")

        self.assertIsNone(resp.get_json(silent=True))
        self.assertIn('id="dashboardSummary"', resp.get_data(as_text=True))


class TestPage(DashboardHtmxTestCase):
    def test_the_filtered_cards_are_rendered_inline_not_deferred(self):
        """Unlike /history and the Top pages this is NOT a two-phase shell: the
        summary is on the first paint, and only a filter change is a swap."""
        body = self._page()

        self.assertIn('id="dashboardSummary"', body)
        self.assertIn("summary-card", body)

    def test_the_page_serves_htmx_from_this_origin(self):
        """config.py's Content-Security-Policy allows script-src 'self' only, so
        a CDN tag would be blocked and no filter change would ever apply."""
        self.assertIn("js/vendor/htmx.min.js", self._page())

    def test_the_page_serves_the_shared_filter_helpers(self):
        """dashboard-page.js reads HtmxFilters at load time, and the same module
        installs the boosted-link modifier fix every migrated page needs."""
        self.assertIn("js/htmx-filters.js", self._page())

    def test_filter_changes_replace_the_url_and_never_push_it(self):
        body = self._page()

        self.assertIn('hx-replace-url="true"', body)
        self.assertNotIn("hx-push-url", body)

    def test_a_superseded_request_is_aborted(self):
        body = self._page()

        self.assertIn("hx-sync=", body)
        self.assertIn(":replace", body)

    def test_a_stale_custom_range_cannot_be_serialized(self):
        """`disabled`, not merely hidden: a disabled control is not submitted,
        which is what keeps a leftover startDate/endDate out of the request -
        and so out of the URL, since hx-replace-url writes back what was asked
        for. That is what params.delete('startDate') used to do by hand."""
        body = self._page("/?interval=week")

        dateInput = body[body.index('id="startDate"'):]
        self.assertIn("disabled", dateInput[:dateInput.index(">")])

    def test_an_active_custom_range_is_serialized(self):
        body = self._page("/?interval=custom&startDate=2026-01-01&endDate=2026-02-01")

        dateInput = body[body.index('id="startDate"'):]
        self.assertNotIn("disabled", dateInput[:dateInput.index(">")])


class TestDeferredCards(DashboardHtmxTestCase):
    """The two cards whose queries scan the whole history, so they load after
    first paint. Both used to answer JSON that the client turned into DOM."""

    def test_the_trends_endpoint_answers_with_the_fragment(self):
        resp = self._get(self._makeApp(), self._makeDb(), "/api/dashboard-trends")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        self.assertIsNone(resp.get_json(silent=True))
        self.assertNotIn("trendsHtml", resp.get_data(as_text=True))
        self.assertIn("Current Obsession", resp.get_data(as_text=True))

    def test_the_discover_endpoint_answers_with_the_fragment(self):
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          recommendations=[{"id": "art1", "name": "Fresh Artist",
                                            "imageId": "img1", "playCount": 2,
                                            "sharedGenreCount": 2,
                                            "matchedGenres": ["rock", "indie"]}])

        resp = self._get(self._makeApp(), db, "/api/dashboard-discover")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        self.assertIsNone(resp.get_json(silent=True))
        body = resp.get_data(as_text=True)
        self.assertIn("Fresh Artist", body)
        self.assertIn("/artist/art1", body)
        self.assertIn("rock", body)   #< the matched genre, as the row's subtitle

    def test_the_locked_state_is_a_rendered_message_not_a_flag(self):
        """`unlocked: false` was a JSON flag the client turned into one of three
        hidden paragraphs. The server picks the paragraph now."""
        db = self._makeDb(coverage=coverageDict(10, 10, 10))

        body = self._get(self._makeApp(), db, "/api/dashboard-discover").get_data(as_text=True)

        self.assertIn("Unlock artist recommendations", body)
        db.getRecommendedArtists.assert_not_called()

    def test_an_unlocked_but_empty_library_says_so(self):
        db = self._makeDb(coverage=coverageDict(80, 60, 90), recommendations=[])

        body = self._get(self._makeApp(), db, "/api/dashboard-discover").get_data(as_text=True)

        self.assertIn("No fresh picks right now", body)
        self.assertNotIn("Unlock artist recommendations", body)

    def test_both_cards_load_themselves_after_first_paint(self):
        db = self._makeDb(coverage=coverageDict(80, 60, 90))

        body = self._page(db=db)

        self.assertIn("/api/dashboard-trends", body)
        self.assertIn("/api/dashboard-discover", body)
        self.assertEqual(body.count('hx-trigger="load"'), 2)

    def test_each_card_still_says_something_when_its_load_fails(self):
        """htmx swaps nothing on a non-2xx, so a card left alone keeps a
        placeholder that claims work is still in progress. Both cards' old
        catch-blocks had an answer for that and both answers survived - they
        just differ, which is the part worth pinning."""
        source = open(os.path.join(os.path.dirname(__file__), "..", "static", "js",
                                   "dashboard-page.js"), encoding="utf-8").read()

        self.assertIn("htmx:responseError", source)
        self.assertIn("discoverCard", source)
        #< blank, not the locked/empty message: neither is true of a 500
        self.assertIn("replaceChildren", source)
        #< the trends row gets the shared error + Retry instead
        self.assertIn("dashboardTrendsContainer", source)
        self.assertIn("renderInto", source)

    def test_the_page_itself_still_runs_neither_card_s_queries(self):
        db = self._makeDb(coverage=coverageDict(80, 60, 90))

        self._page(db=db)

        db.getGenreCoverage.assert_not_called()
        db.getRecommendedArtists.assert_not_called()
        db.getDashboardTrends.assert_not_called()


class TestTheNowPlayingPollStaysHandWritten(DashboardHtmxTestCase):
    """The one fetch that did not move, pinned so nobody "finishes the job".

    htmx can issue the request (hx-trigger="every 15s"), but not the two things
    this poll exists to do. It renders DATA into a dozen elements with per-link
    logic (an internal /song/<id> link only when the track has actually been
    played before, a Spotify link otherwise, plain text with no id at all), so
    there is no fragment to swap without inventing one. And it must STOP on a
    401 rather than navigate: a background poll yanking the page to /login
    mid-read is exactly the behaviour the 401 branch was added to prevent, while
    every htmx request in the app is answered with HX-Redirect, which navigates.
    Stopping a poll from the client needs hx-on:: - and that compiles a JS
    expression with the Function constructor, which the CSP denies this page.
    """

    def test_the_poll_endpoint_still_answers_json(self):
        db = self._makeDb()
        db.getNowPlaying.return_value = None   #< nothing playing; a Mock is not serializable

        resp = self._get(self._makeApp(), db, "/api/now-playing")

        self.assertEqual(resp.mimetype, "application/json")

    def test_the_dashboard_still_ships_the_hand_written_poller(self):
        source = open(os.path.join(os.path.dirname(__file__), "..", "static", "js",
                                   "dashboard-page.js"), encoding="utf-8").read()

        self.assertIn("/api/now-playing", source)
        self.assertIn("setInterval", source)
        #< the 401 branch: stop the timer, do not navigate
        self.assertIn("clearInterval", source)


class TestDeferredCardsAreNotRedirected(DashboardHtmxTestCase):
    """The dashboard's own share of the unauthenticated contract. The page-level
    part of it - HX-Redirect instead of a 302, an empty body, the filters
    preserved - is app-wide and parametrized over every htmx page in
    tests/test_ajax_unauthenticated.py; what is dashboard-specific is that the
    two deferred CARD endpoints deliberately do NOT join in."""

    def setUp(self):
        self.dash = self._makeApp()
        self.client = self.dash.app.test_client()
        self.patcher = patch.object(self.dash, "is_user_logged_in", return_value=False)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_the_deferred_cards_keep_their_401(self):
        """@requiresUser(api=True), unchanged: htmx reports the error and swaps
        nothing, so the card keeps its placeholder rather than the whole
        dashboard navigating away under a card that failed to load."""
        for path in ("/api/dashboard-trends", "/api/dashboard-discover"):
            with self.subTest(path=path):
                resp = self.client.get(path, headers=HX_HEADERS)

                self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
