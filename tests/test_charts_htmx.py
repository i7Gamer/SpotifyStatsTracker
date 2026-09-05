"""The /charts page's htmx contract (routes/charts.py's chartsPage,
templates/charts.html, templates/_charts_results.html, static/js/charts-page.js).

The sibling of tests/test_history_htmx.py, which carries the fuller commentary
on why the transport looks like this. What is specific to this page:

- it is eight CANVASES, which htmx cannot swap. The migration is still a real
  one because the old payload was fifteen keys of which the client rebuilt a
  whole page: two headings' text, whether the artist-trend section exists at
  all, and the Top Genres section's entire locked/unlocked body. All three are
  markup, and the server was already rendering the third one into a string. Now
  it renders all of them, and the nine chart SERIES ride along in one JSON data
  island the afterSettle handler parses into window.__chartData.
- the charts are hand-rolled canvas drawing (static/js/chart-utils.js), not a
  library holding instances per <canvas>, so replacing the canvases wholesale on
  every filter change leaks nothing - each render sets its canvas up from
  scratch.

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
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase
from _charts_client import HX_HEADERS, chartData

#< every series the page draws; the island has to carry all of them or a canvas
#  silently renders its empty state
CHART_SERIES_KEYS = ("timeSeries", "heatmap", "artistTrend", "explicitRatio",
                     "decadeDistribution", "completionStats", "mostSkippedSongs",
                     "mostSkippedArtists", "genreDistribution")


class ChartsHtmxTestCase(AppTestCase):
    def _makeDb(self):
        db = MagicMock()
        db.getListeningTimeSeries.return_value = [
            {"label": "2026-07-01", "totalTimeListened": 1000, "plays": 1},
        ]
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0}
                                                for _ in range(24)] for _ in range(7)]
        db.getArtistTrend.return_value = {"buckets": [], "series": []}
        db.getExplicitRatio.return_value = {"explicit": 0, "clean": 0}
        db.getReleaseDecadeDistribution.return_value = {}
        db.getCompletionStats.return_value = {"skips": 0, "completes": 0, "partials": 0}
        db.getMostSkippedSongs.return_value = []
        db.getMostSkippedArtists.return_value = []
        db.repo.getUserSettings.return_value = {"default_dashboard_window": "month", "timezone": None}
        return db

    def _request(self, query="", headers=None, db=None):
        dash = self._makeApp()
        self.db = db or self._makeDb()
        client = dash.app.test_client()
        with patch.object(dash, "is_user_logged_in", return_value=True), \
             patch.object(dash, "get_username_for_email", return_value="alice"), \
             patch.object(dash, "get_user_db", return_value=self.db):
            with client.session_transaction() as sess:
                sess["email"] = "alice@example.com"
            return client.get(f"/charts{query}", headers=headers)

    def _shell(self, query=""):
        return self._request(query).get_data(as_text=True)

    def _fragment(self, query="", db=None):
        return self._request(query, headers=HX_HEADERS, db=db)


class TestFragmentBranch(ChartsHtmxTestCase):
    def test_an_hx_request_gets_html_not_json(self):
        resp = self._fragment()

        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        self.assertIsNone(resp.get_json(silent=True))

    def test_the_old_json_envelope_keys_are_gone_from_the_response_shape(self):
        """genreSectionHtml was a whole rendered section handed over as a JSON
        string for the client to inject. It is markup now, so the key that
        wrapped it - and the two label keys beside it - do not exist."""
        body = self._fragment().get_data(as_text=True)

        for key in ("genreSectionHtml", "intervalLabel", "lastDayDate"):
            with self.subTest(key=key):
                self.assertNotIn(key, body)

    def test_the_fragment_is_not_a_whole_document(self):
        body = self._fragment().get_data(as_text=True)

        self.assertNotIn("<html", body.lower())
        self.assertNotIn('id="interval"', body)   #< the filter card stays in the shell

    def test_the_fragment_carries_every_canvas_the_page_draws(self):
        body = self._fragment().get_data(as_text=True)

        for canvas in ("timeSeriesChart", "heatmapChart", "artistTrendChart", "explicitChart",
                       "completionChart", "mostSkippedSongsChart", "mostSkippedArtistsChart",
                       "decadeChart"):
            with self.subTest(canvas=canvas):
                self.assertIn(canvas, body)

    def test_every_series_rides_the_json_island(self):
        payload = chartData(self._fragment())

        for key in CHART_SERIES_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, payload)

    def test_the_island_carries_the_resolved_bucket_and_interval(self):
        """charts.js reads both off window.__chartData - the time-series axis
        labels itself from them."""
        payload = chartData(self._fragment("?interval=year&groupBy=month"))

        self.assertEqual(payload["groupBy"], "month")
        self.assertEqual(payload["interval"], "year")

    def test_a_single_day_range_omits_the_artist_trend_section(self):
        """It has no trend to draw (the route sends None), and an empty frame
        with a heading reads as "you listened to nobody". The client used to
        hide it after the fact; the server just doesn't render it."""
        body = self._fragment("?interval=day").get_data(as_text=True)

        self.assertNotIn("artistTrendChart", body)
        self.assertIn("timeSeriesChart", body)   #< the rest of the card is unaffected

    def test_the_headings_name_the_selected_range(self):
        body = self._fragment("?interval=year").get_data(as_text=True)

        self.assertIn("Last Year", body)

    def test_ajax_true_alone_no_longer_triggers_the_fragment(self):
        """?ajax=true was the old marker. Without HX-Request it is just an
        unknown query param, and the page renders its shell."""
        resp = self._request("?ajax=true")

        self.assertIsNone(resp.get_json(silent=True))
        self.assertIn('id="chartsCard"', resp.get_data(as_text=True))


class TestShell(ChartsHtmxTestCase):
    def test_the_shell_does_not_run_the_data_queries(self):
        self._shell()

        self.db.getListeningTimeSeries.assert_not_called()
        self.db.getArtistTrend.assert_not_called()
        self.db.getHourOfDayHeatmap.assert_not_called()

    def test_the_shell_holds_the_placeholder_but_no_canvases(self):
        body = self._shell()

        self.assertIn('id="chartsCard"', body)
        self.assertNotIn("timeSeriesChart", body)

    def test_the_shell_serves_htmx_from_this_origin(self):
        """config.py's Content-Security-Policy allows script-src 'self' only, so
        a CDN tag would be blocked and the page would never load its charts."""
        self.assertIn("js/vendor/htmx.min.js", self._shell())

    def test_the_shell_serves_the_shared_filter_helpers(self):
        """charts-page.js reads HtmxFilters at load time (the date-range check
        and the empty-param pruning), and the same module installs the
        boosted-link modifier fix every migrated page needs."""
        self.assertIn("js/htmx-filters.js", self._shell())

    def test_the_placeholder_triggers_the_first_load(self):
        self.assertIn('hx-trigger="load"', self._shell())

    def test_the_first_load_swaps_into_the_card_and_not_the_placeholder(self):
        """The one thing that makes the canvases draw. charts-page.js redraws
        in htmx:afterSettle and keys its guard on the swapped target being
        #chartsCard, so the first load has to land THERE. htmx defaults a
        request's target to the element that fired it, and the placeholder is
        not that element - without an hx-target on the card for it to inherit
        it swaps into itself, afterSettle carries an id-less div, the guard bails,
        and the page paints eight blank canvases under their headings.

        Asserted on the card rather than on the placeholder because inheriting
        it is the point: /history and /genres put the same four attributes on
        their swap container for the same reason (see their templates)."""
        body = self._shell()
        card = body[body.index('id="chartsCard"'):]
        card = card[:card.index('hx-trigger="load"')]   #< the card's own tag only

        self.assertIn('hx-target="#chartsCard"', card)
        self.assertIn('hx-swap="innerHTML"', card)

    def test_the_first_load_dims_the_card_and_joins_the_abort_queue(self):
        """The other two inherited attributes: a slow first load gets the same
        fade every filter change gets, and a filter changed while it is still
        in flight aborts it instead of racing it."""
        body = self._shell()
        card = body[body.index('id="chartsCard"'):]
        card = card[:card.index('hx-trigger="load"')]

        self.assertIn('hx-indicator="#chartsCard"', card)
        self.assertIn('hx-sync="#chartsCard:replace"', card)

    def test_filter_changes_replace_the_url_and_never_push_it(self):
        body = self._shell()

        self.assertIn('hx-replace-url="true"', body)
        self.assertNotIn("hx-push-url", body)

    def test_a_superseded_request_is_aborted(self):
        body = self._shell()

        self.assertIn("hx-sync=", body)
        self.assertIn(":replace", body)

    def test_the_first_load_url_holds_no_unvalidated_input(self):
        body = self._shell("?interval=bogus&groupBy=nonsense")

        self.assertNotIn("bogus", body)
        self.assertNotIn("nonsense", body)

    def test_the_first_load_carries_the_filters_from_a_shared_url(self):
        body = self._shell("?interval=week&groupBy=month")

        self.assertIn("interval=week", body)
        self.assertIn("groupBy=month", body)

    def test_a_stale_custom_range_cannot_be_serialized(self):
        """`disabled`, not merely hidden: a disabled control is not submitted,
        which is what keeps a leftover startDate/endDate out of the request -
        and so out of the URL, since hx-replace-url writes back what was asked
        for."""
        body = self._shell("?interval=week")

        dateInput = body[body.index('id="startDate"'):]
        self.assertIn("disabled", dateInput[:dateInput.index(">")])

    def test_an_active_custom_range_is_serialized(self):
        body = self._shell("?interval=custom&startDate=2026-01-01&endDate=2026-02-01")

        dateInput = body[body.index('id="startDate"'):]
        self.assertNotIn("disabled", dateInput[:dateInput.index(">")])

    def test_an_inverted_custom_range_is_not_claimed_by_the_heading(self):
        """start > end is refused server-side exactly like an unparseable
        date (dashboard/date_ranges.py, CORE-4): the fragment renders the
        default window under the default window's heading, not
        "Custom range: 2024-06-01 to 2020-01-01" over empty charts."""
        body = self._fragment("?interval=custom&startDate=2024-06-01&endDate=2020-01-01").get_data(as_text=True)

        self.assertNotIn("Custom range:", body)
        self.assertIn("Last Month", body)   #< the mocked default_dashboard_window


