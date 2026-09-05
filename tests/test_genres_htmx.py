"""The /genres page's htmx contract (routes/genres.py, templates/genres.html,
templates/_genres_results.html, templates/_genre_explore.html,
static/js/genres.js).

/genres follows /history off the hand-rolled fetch/swap layer, but it has two
things /history did not, and both are pinned here:

- TWO request shapes, not one. A time-filter change replaces the whole results
  block (overview charts + chips + drill-down); a chip click replaces only the
  chips + drill-down, so the unchanged overview keeps its (expensive) queries
  unrun. htmx names the region it is replacing in the ``HX-Target`` header, so
  that header - not a ``?scope=detail`` query param - is what the route branches
  on. Keeping the marker out of the query string is what keeps it out of the
  address bar, since ``hx-replace-url`` writes back the URL that was requested.
- CHART DATA, which is not HTML. htmx swaps response bodies, so the datasets
  ride along inside the fragment as ``<script type="application/json">``
  islands, and genres.js redraws the canvases from them in an
  ``htmx:afterSettle`` listener. That listener is the one thing htmx cannot own:
  it swaps the <canvas> element, it has no idea it has to be painted.

The unlock GATE is unchanged and re-asserted here from the htmx side: a
disabled instance and a locked library still render a shell with no htmx wiring
at all, so the second request is never issued. If the gate flips to locked
underneath a live page, a full swap is answered with 204 (no swap, placeholders
stay - the same "do not risk a reload loop" call the old ok:false branch made)
and a chip swap with HX-Redirect to the full page render, which is the
detailFallbackUrl recovery path the old loader took by hand.

The page CONTENT (gate thresholds, groupBy resolution, genre selection, query
scoping) is covered by tests/test_genres_page.py against the same fragment
branch; what is here is the transport.

The logged-out contract for this page is NOT here. HX-Redirect instead of a
302, an empty body, and the filters preserved through the login round-trip
are one app-wide rule with one implementation (app.py's
unauthenticatedResponse), so it is asserted once, parametrized over every
htmx page, in tests/test_ajax_unauthenticated.py. Eight copies of it lived
here and in the sibling files, and only half checked all three things.
"""
import json
import os
import re
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase
from routes.genres import GENRES_RESULTS_ID, GENRE_EXPLORE_ID

#< what htmx puts on every request it makes
HX_HEADERS = {"HX-Request": "true"}
#< ...plus the id of the region being replaced. A chip click targets the
#  drill-down only; everything else targets the whole results block.
HX_EXPLORE_HEADERS = {"HX-Request": "true", "HX-Target": GENRE_EXPLORE_ID}


def coverageDict(song, album, artist, total=1000):
    def category(percent):
        return {"covered": int(total * percent / 100), "total": total, "percent": percent}
    return {
        "song": category(song),
        "album": category(album),
        "artist": category(artist),
        "overall": {"percent": round((song + album + artist) / 3, 1)},
    }


UNLOCKED = coverageDict(80, 60, 90)


def islandJson(body, elementId):
    """The chart data the fragment carries for genres.js. Flask's tojson escapes
    `<`, so nothing inside can close the script tag early."""
    match = re.search(r'<script type="application/json" id="%s">(.*?)</script>' % elementId,
                      body, re.S)
    assert match, f"no {elementId} data island in the fragment"
    return json.loads(match.group(1))


class GenresHtmxTestCase(AppTestCase):
    def _makeDb(self, coverage=UNLOCKED, distribution=None, window="all time"):
        db = MagicMock()
        db.repo.getUserSettings.return_value = {"default_dashboard_window": window, "timezone": None}
        db.getGenreCoverage.return_value = coverage
        db.getGenreDistribution.return_value = {"rock": 120, "jazz": 40} if distribution is None else distribution
        db.getGenreTrends.return_value = {"buckets": ["2026-01"], "series": [{"name": "rock", "data": [1]}]}
        db.getGenreStats.return_value = {"plays": 10, "listenMs": 60000,
                                         "firstPlayedTs": None, "sharePercent": 25.0}
        db.getTopArtistsForGenre.return_value = []
        db.getTopTracksForGenre.return_value = []
        db.getGenreHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0}
                                                     for _ in range(24)] for _ in range(7)]
        db.getGenreArtistCounts.return_value = {"rock": 12, "jazz": 4}
        return db

    def _request(self, dash, db, query="", headers=None):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(f"/genres{query}", headers=headers or {})

    def _shell(self, query="", db=None, dash=None):
        dash = dash or self._makeApp()
        return self._request(dash, db or self._makeDb(), query).get_data(as_text=True)

    def _fragment(self, query="", db=None, headers=None):
        dash = self._makeApp()
        return self._request(dash, db or self._makeDb(), query,
                             headers=headers or HX_HEADERS).get_data(as_text=True)


class TestFragmentBranch(GenresHtmxTestCase):
    def test_an_hx_request_gets_html_not_json(self):
        dash = self._makeApp()

        resp = self._request(dash, self._makeDb(), headers=HX_HEADERS)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        self.assertIsNone(resp.get_json(silent=True))

    def test_the_fragment_is_the_overview_plus_the_drill_down(self):
        body = self._fragment()

        self.assertIn('id="genreDistChart"', body)
        self.assertIn('id="genreMixChart"', body)
        self.assertIn('id="genreChipRow"', body)
        self.assertIn('id="genreClockChart"', body)

    def test_the_fragment_is_not_a_whole_document(self):
        """htmx puts this response INSIDE #genresResults. A full page here would
        nest a second <html>/<body> and the site chrome inside the results."""
        body = self._fragment()

        self.assertNotIn("<html", body.lower())
        self.assertNotIn("<body", body.lower())

    def test_the_old_json_envelope_is_gone(self):
        """The keys the previous fetch() layer read. Their absence is the point:
        htmx would swap the literal JSON into the page."""
        body = self._fragment()

        self.assertNotIn("detailHtml", body)
        self.assertNotIn("chipsHtml", body)

    def test_ajax_true_alone_no_longer_triggers_the_fragment(self):
        """?ajax=true was the old marker. Without HX-Request it is just an
        unknown query param, and the page renders its shell."""
        dash = self._makeApp()

        resp = self._request(dash, self._makeDb(), query="?ajax=true")

        self.assertIsNone(resp.get_json(silent=True))
        self.assertIn(f'id="{GENRES_RESULTS_ID}"', resp.get_data(as_text=True))

    def test_the_chart_datasets_ride_along_as_a_json_island(self):
        """The one thing a body swap cannot carry as markup. genres.js reads
        these back out in htmx:afterSettle and paints the canvases."""
        data = islandJson(self._fragment(), "genres-overview-data")

        self.assertIn(["rock", 120], data["distributionPairs"])
        self.assertIn(["rock", 12], data["breadthPairs"])
        self.assertIn("buckets", data["mixTrend"])

    def test_a_range_with_no_genres_still_ships_well_formed_islands(self):
        """There is nothing to select, so the chip row and the drill-down render
        empty - but the islands still have to parse, or genres.js's afterSettle
        would bail before it cleared the previous range's charts."""
        body = self._fragment(db=self._makeDb(distribution={}))

        self.assertEqual(islandJson(body, "genres-overview-data")["distributionPairs"], [])
        detail = islandJson(body, "genres-detail-data")
        self.assertIsNone(detail["genre"])
        self.assertEqual(detail["selectedTrend"], {"buckets": [], "series": []})
        self.assertNotIn('id="genreClockChart"', body)

    def test_the_drill_down_carries_its_own_two_datasets(self):
        """Separate from the overview island because a chip swap re-sends only
        these two - the overview island is not in that response at all."""
        data = islandJson(self._fragment(), "genres-detail-data")

        self.assertEqual(data["genre"], "rock")
        self.assertIn("buckets", data["selectedTrend"])
        self.assertEqual(len(data["clock"]), 7)   #< days of the week


class TestChipSwap(GenresHtmxTestCase):
    """A chip click replaces the drill-down only, keyed on HX-Target."""

    def test_it_returns_the_chips_and_detail_without_the_overview(self):
        body = self._fragment(query="?genre=jazz", headers=HX_EXPLORE_HEADERS)

        self.assertIn('id="genreChipRow"', body)
        self.assertIn('id="genreClockChart"', body)
        self.assertNotIn('id="genreDistChart"', body)
        self.assertNotIn('id="genreMixChart"', body)

    def test_it_skips_the_queries_only_the_overview_needs(self):
        """The whole reason the two shapes exist: a genre switch changes neither
        the distribution bars nor the mix trend nor the breadth chart, and
        re-running them on every chip click is what the old scope=detail payload
        existed to avoid."""
        db = self._makeDb()

        self._fragment(query="?genre=jazz", db=db, headers=HX_EXPLORE_HEADERS)

        db.getGenreArtistCounts.assert_not_called()
        #< only the selected genre's trend, not the mix-over-time top-N
        for call in db.getGenreTrends.call_args_list:
            self.assertEqual(call.args[0], ["jazz"])

    def test_it_marks_the_clicked_chip_selected(self):
        """The class the old setSelectedChip() toggled by hand; the server now
        renders the row, so the markup and the drill-down cannot disagree."""
        body = self._fragment(query="?genre=jazz", headers=HX_EXPLORE_HEADERS)

        self.assertRegex(body, r'class="genre-chip selected"[^>]*data-genre="jazz"')

    def test_the_chip_hrefs_carry_the_whole_filter_state(self):
        """The href IS the request URL now (hx-boost) and, through
        hx-replace-url, the address bar afterwards - so dropping a filter here
        would silently reset it on the next chip click. groupBy was missing from
        the old fallback href for exactly this reason."""
        body = self._fragment(query="?interval=week&groupBy=month")

        self.assertRegex(body, r'href="[^"]*genre=jazz[^"]*"')
        self.assertRegex(body, r'href="[^"]*interval=week[^"]*"')
        self.assertRegex(body, r'href="[^"]*groupBy=month[^"]*"')

    def test_the_chip_row_is_boosted_and_the_drill_down_is_not(self):
        """hx-boost is scoped to the chip row on purpose: boosting the whole
        drill-down would capture every artist/track link in the top lists below
        and swap a DETAIL PAGE into the results area instead of navigating."""
        body = self._fragment()

        chipRow = re.search(r'<div class="genre-chip-row" id="genreChipRow"[^>]*>', body)
        self.assertIsNotNone(chipRow)
        self.assertIn('hx-boost="true"', chipRow.group(0))
        self.assertEqual(body.count('hx-boost'), 1)


class TestShell(GenresHtmxTestCase):
    def test_the_shell_serves_htmx_and_its_filter_helpers_from_this_origin(self):
        """config.py's Content-Security-Policy allows script-src 'self' only, so
        a CDN tag would be blocked and the page would silently never load."""
        body = self._shell()

        self.assertIn("js/vendor/htmx.min.js", body)
        self.assertIn("js/htmx-filters.js", body)

    def test_the_shell_wires_the_swap_target(self):
        body = self._shell()

        self.assertIn(f'hx-target="#{GENRES_RESULTS_ID}"', body)
        self.assertIn('hx-swap="innerHTML"', body)

    def test_filter_changes_replace_the_url_and_never_push_it(self):
        """The invariant the hand-written replaceGenresUrl existed for: an
        in-page filter change must not stack a history entry, so Back leaves the
        page instead of stepping through every filter that was tried."""
        body = self._shell()

        self.assertIn('hx-replace-url="true"', body)
        self.assertNotIn("hx-push-url", body)

    def test_a_superseded_request_is_aborted(self):
        """hx-sync ...:replace is what the shared loadToken did by hand. Both
        shapes sync on the SAME element, so a chip click during an in-flight
        filter load supersedes it rather than racing it."""
        body = self._shell()

        self.assertIn(f'hx-sync="#{GENRES_RESULTS_ID}:replace"', body)

    def test_every_swap_dims_the_results_while_it_loads(self):
        """On the container as well as the form: a boosted chip would otherwise
        mark ITSELF as busy and the charts would sit there looking settled."""
        body = self._shell()

        self.assertEqual(body.count(f'hx-indicator="#{GENRES_RESULTS_ID}"'), 2)
        self.assertIn("htmx-fade-target", body)

    def test_the_placeholder_triggers_the_first_load(self):
        """The shell renders empty; something has to ask for the data."""
        body = self._shell()

        self.assertIn('hx-trigger="load"', body)
        #< this one request asks for the canonical form of the URL the browser
        #  already shows, so rewriting the address bar would be a silent redirect
        self.assertIn('hx-replace-url="false"', body)

    def test_the_first_load_carries_the_filters_from_a_shared_url(self):
        """A link to /genres?interval=week&groupBy=month has to open on THAT
        view, not on the default one that then disagrees with the controls."""
        body = self._shell("?interval=week&groupBy=month")

        self.assertIn("interval=week", body)
        self.assertIn("groupBy=month", body)

    def test_the_first_load_url_holds_no_unvalidated_input(self):
        """The route coerces a junk interval and a junk bucket size for the
        queries; echoing the raw ones into the load URL would assert them again
        in the markup and disagree with the chip hrefs built beside them."""
        body = self._shell("?interval=bogus&groupBy=fortnight")

        self.assertNotIn("bogus", body)
        self.assertNotIn("fortnight", body)

    def test_the_first_load_drops_a_range_that_is_not_in_effect(self):
        """Matching the disabled date inputs: an explicit range only scopes the
        data while Time Period is on Custom."""
        body = self._shell("?interval=week&startDate=2026-01-01&endDate=2026-02-01")

        self.assertNotIn("startDate=2026-01-01", body)

    def test_the_custom_range_inputs_are_disabled_unless_they_are_in_effect(self):
        """`disabled`, not merely hidden: htmx serializes every ENABLED control
        in the form, so a hidden-but-live date pair would put a stale range back
        into the request - and, through hx-replace-url, into the address bar."""
        body = self._shell("?interval=week")

        self.assertRegex(body, r'id="startDate"[^>]*disabled')
        self.assertNotRegex(self._shell("?interval=custom&startDate=2026-01-01&endDate=2026-02-01"),
                            r'id="startDate"[^>]*disabled')

    def test_the_selected_genre_rides_in_a_hidden_form_field(self):
        """A filter change serializes the FORM, and the drill-down selection has
        no visible control in it - without this field, changing the time period
        would silently reset the page to the new range's top genre."""
        body = self._shell("?genre=jazz")

        self.assertRegex(body, r'<input type="hidden" name="genre"[^>]*value="jazz"')

    def test_the_first_load_url_passes_the_requested_genre_through(self):
        """The one filter the shell cannot validate: deciding whether a genre
        exists needs the distribution query this phase deliberately defers. The
        data request resolves it - falling back to the range's top genre - so an
        unknown value costs a fallback, never a wrong render."""
        body = self._shell("?genre=doesnotexist")

        self.assertIn("genre=doesnotexist", body)

    def test_the_swap_ids_are_the_ones_the_route_branches_on(self):
        """The route reads HX-Target to pick a fragment, so a renamed id in the
        template would silently send the full payload on every chip click."""
        body = self._shell()

        self.assertIn(f'id="{GENRES_RESULTS_ID}"', body)
        self.assertIn(f'hx-target="#{GENRE_EXPLORE_ID}"', self._fragment())
        self.assertIn(f'id="{GENRE_EXPLORE_ID}"', self._fragment())


class TestGateStillDecidesTheShell(GenresHtmxTestCase):
    """The unlock gate is unchanged by the migration: it decides what the shell
    renders, and a shell with no htmx wiring never makes the second request."""

    def test_a_disabled_instance_renders_no_htmx_wiring(self):
        dash = self._makeApp()
        dash.repo.setLastfmGenreBackfillEnabled(False)

        body = self._request(dash, self._makeDb(), "").get_data(as_text=True)

        self.assertIn("turned off for this instance", body)
        self.assertNotIn("hx-get", body)
        self.assertNotIn("htmx.min.js", body)

    def test_a_locked_library_renders_progress_and_no_htmx_wiring(self):
        body = self._shell(db=self._makeDb(coverage=coverageDict(10, 10, 10)))

        self.assertIn("Genre insights unlock", body)
        self.assertNotIn("hx-get", body)
        self.assertNotIn("htmx.min.js", body)

    def test_a_full_swap_against_a_locked_gate_leaves_the_page_alone(self):
        """The gate is all-time and stable, so this should not happen once the
        shell rendered unlocked. 204 is htmx's "no swap": the placeholders stay
        rather than being replaced by something that might ask again."""
        dash = self._makeApp()

        resp = self._request(dash, self._makeDb(coverage=coverageDict(10, 10, 10)),
                             headers=HX_HEADERS)

        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp.get_data(as_text=True), "")
        self.assertIsNone(resp.headers.get("HX-Redirect"))

    def test_a_chip_swap_against_a_locked_gate_falls_back_to_the_full_page(self):
        """The deliberate recovery path the old loader took by hand
        (detailFallbackUrl, documented in tests/test_ajax_loader_error_handling.py):
        the server declined the swap, and the full page render is the one that
        can explain why. HX-Redirect is htmx's word for that navigation."""
        dash = self._makeApp()

        resp = self._request(dash, self._makeDb(coverage=coverageDict(10, 10, 10)),
                             query="?genre=jazz&interval=week", headers=HX_EXPLORE_HEADERS)

        target = resp.headers.get("HX-Redirect", "")
        self.assertIn("/genres", target)
        self.assertIn("genre=jazz", target)
        self.assertIn("interval=week", target)
        self.assertEqual(resp.get_data(as_text=True), "")


