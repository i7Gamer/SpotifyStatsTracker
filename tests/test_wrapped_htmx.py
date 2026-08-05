"""The /wrapped page's htmx contract (routes/wrapped.py, templates/wrapped.html,
templates/_wrapped_results.html, static/js/wrapped.js).

Modelled on tests/test_history_htmx.py, which pins the seam every migrated page
copies:

- the second request is marked by the ``HX-Request`` header, where it used to be
  ``?ajax=true``, and it is answered with an HTML FRAGMENT rather than the
  ``{"topSongsHtml": ..., "timeSeries": ...}`` envelope. htmx swaps response
  bodies, so an envelope would land in the page verbatim.
- a logged-out swap gets ``HX-Redirect``, not a 302 the swap would inline.
- the URL updates via ``hx-replace-url``, never ``hx-push-url``.

Two things are specific to this page and have no counterpart on /history:

- /wrapped renders its data on the FIRST GET (it is not a two-phase shell), and
  the regions a filter change touches are not contiguous - the hero title and
  the year badges sit above the filter form, the export button inside it, the
  share panel inside the modal. Those ride along as out-of-band swaps, so one
  response still updates all of them. TestOutOfBandRegions is that contract.
- the page has a PUBLIC twin at /shared/<token> rendered through
  layout_public.html, which is not behind @requiresUser. An expired or revoked
  share token is a 404, not a session problem, and must NOT acquire an
  HX-Redirect to /login - see TestSharedWrappedHtmx.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from test_share_link_routes import (
    ShareLinkRoutesTestCase, PublicSharedWrappedTestCase, _ts,
)

#< what htmx puts on every request it makes
HX_HEADERS = {"HX-Request": "true"}

#< the container everything year-scoped is swapped into, and the ids of the
#  regions that ride along out of band because they sit outside it
RESULTS_ID = "wrappedResults"
OOB_IDS = ("wrappedHero", "wrappedYearBadges", "wrappedYearField", "exportWrappedBtn",
           "shareLinkPanelBody")

#< the keys the retired JSON envelope was read by (see the deleted
#  _buildWrappedAjaxResponse) - their absence is the point of the migration
RETIRED_ENVELOPE_KEYS = ("topSongsHtml", "topArtistsHtml", "topAlbumsHtml",
                         "discoveredSongsHtml", "topGenresHtml", "sharePanelHtml")


def _song(trackId, name):
    return {
        "id": trackId, "name": name, "url": "https://open.spotify.com/track/" + trackId,
        "imageId": "img1", "duration": 0, "explicit": False, "isrc": "",
        "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
        "album": {"id": "alb1", "name": "Album", "url": "u", "imageId": "img1", "imageUrl": "",
                  "totalTracks": 1, "releaseDate": 0},
        "artists": [], "plays": 5, "totalTimeListened": 5000, "firstListenedAt": 0,
    }


class WrappedHtmxTestCase(ShareLinkRoutesTestCase):
    """A logged-in owner whose history reaches back to 2023, so every year
    badge from 2023 to the frozen 2026 is selectable."""

    def setUp(self):
        super().setUp()
        self.db = self._makeDb()
        self.db.getEntriesFromOld.return_value = [{"playedAt": _ts(2023), "timePlayed": 1}]
        self.client = self._loginAs("alice", "alice@example.com", db=self.db)

    def _page(self, query=""):
        return self.client.get("/wrapped" + query).get_data(as_text=True)

    def _fragment(self, query=""):
        return self.client.get("/wrapped" + query, headers=HX_HEADERS).get_data(as_text=True)


class TestFragmentBranch(WrappedHtmxTestCase):
    def test_an_hx_request_gets_html_not_json(self):
        resp = self.client.get("/wrapped", headers=HX_HEADERS)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        self.assertIsNone(resp.get_json(silent=True))

    def test_the_fragment_is_the_year_scoped_content_itself(self):
        self.db.getTopSongs.return_value = [_song("song1", "A Recorded Song")]

        body = self._fragment()

        self.assertIn("A Recorded Song", body)
        self.assertIn('class="track-summary-grid"', body)

    def test_the_fragment_carries_the_chart_canvas(self):
        """It is swapped in as one unit with the lists - the canvas is a new
        element every time, which is why the re-render is an afterSwap
        listener in static/js/wrapped.js rather than anything htmx owns."""
        body = self._fragment()

        self.assertIn('id="timeSeriesChart"', body)

    def test_the_fragment_carries_the_time_series_for_the_redraw(self):
        """The chart data used to arrive as a JSON key; it now rides in the
        same data island the full render uses, which the afterSwap listener
        re-parses."""
        body = self._fragment()

        self.assertIn('id="wrapped-bootstrap"', body)

    def test_the_fragment_is_not_a_whole_document(self):
        """htmx puts this response INSIDE #wrappedResults. A full page here
        would nest a second <html>/<body> and the site chrome inside it."""
        body = self._fragment()

        self.assertNotIn("<html", body.lower())
        self.assertNotIn("<body", body.lower())

    def test_the_old_json_envelope_is_gone(self):
        """The keys the previous fetch() layer read. Their absence is the
        point: htmx would swap the literal JSON into the page."""
        body = self._fragment()

        for key in RETIRED_ENVELOPE_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(key, body)

    def test_ajax_true_alone_no_longer_triggers_the_fragment(self):
        """?ajax=true was the old marker. Without HX-Request it is just an
        unknown query param, and the page renders in full."""
        resp = self.client.get("/wrapped?ajax=true")

        self.assertIsNone(resp.get_json(silent=True))
        self.assertIn('id="%s"' % RESULTS_ID, resp.get_data(as_text=True))

    def test_the_fragment_honours_the_requested_year(self):
        self.db.getTopSongs.return_value = [_song("song1", "A Recorded Song")]

        body = self._fragment("?year=2024")

        self.assertIn("2024", body)
        kwargs = self.db.getTopSongs.call_args.kwargs
        self.assertEqual(kwargs["startDate"].year, 2024)


class TestShell(WrappedHtmxTestCase):
    def test_the_page_serves_htmx_from_this_origin(self):
        """config.py's Content-Security-Policy allows script-src 'self' only,
        so a CDN tag would be blocked and every filter change would silently
        do nothing. See NOTICE for the pinned version."""
        self.assertIn("js/vendor/htmx.min.js", self._page())

    def test_the_page_wires_the_swap_target(self):
        body = self._page()

        self.assertIn('hx-target="#%s"' % RESULTS_ID, body)
        self.assertIn('hx-swap="innerHTML"', body)

    def test_filter_changes_replace_the_url_and_never_push_it(self):
        """The invariant the hand-written replaceState in wrapped.js existed
        for: an in-page filter change must not stack a history entry, so Back
        leaves the page instead of stepping through every year that was
        looked at."""
        body = self._page()

        self.assertIn('hx-replace-url="true"', body)
        self.assertNotIn("hx-push-url", body)

    def test_a_superseded_request_is_aborted(self):
        """hx-sync ...:replace is what activeWrappedLoad's AbortController
        bookkeeping did by hand - without it, a slow year switch can land on
        top of a newer one."""
        body = self._page()

        self.assertIn("hx-sync=", body)
        self.assertIn(":replace", body)

    def test_every_swap_dims_the_content_while_it_loads(self):
        """setWrappedFade's replacement. The indicator has to name the
        container on BOTH request sources - the filter form and the year
        badges - or a badge click would mark the badge itself busy and the
        page would sit there looking settled while the next year loaded."""
        body = self._page()

        self.assertEqual(body.count('hx-indicator="#%s"' % RESULTS_ID), 2)
        self.assertIn('class="htmx-fade-target"', body)

    def test_the_year_badges_are_boosted_rather_than_hand_intercepted(self):
        """The badges are links with the whole filter state in their href, so
        hx-boost covers them - that is the delegated click listener that used
        to preventDefault, re-read the three selects and rebuild the URL."""
        body = self._page()

        self.assertIn('hx-boost="true"', body)

    def test_a_year_badge_carries_the_filters_currently_in_effect(self):
        """The boosted href IS the request, and hx-replace-url writes it to
        the address bar - so switching year while sorted by name has to keep
        the sort, the way the old handler's re-read of the selects did."""
        body = self._page("?sortBy=name&limit=25")

        self.assertIn("sortBy=name", body)
        self.assertIn("limit=25", body)

    def test_a_badge_href_leaves_out_an_unset_trend_bucket(self):
        """Auto is the empty value. url_for would spell it `groupBy=`, and a
        boosted click would then replace the address bar with that - the
        hand-written builder dropped it, so the shared link stayed clean."""
        body = self._page()

        self.assertNotIn("groupBy=&", body)
        #< the same param sitting last in a href, i.e. `...&groupBy="`. The
        #  select's own `name="groupBy"` and `for="groupBy"` do not match.
        self.assertNotIn('groupBy="', body)

    def test_the_page_still_renders_its_data_on_the_first_get(self):
        """/wrapped is NOT a two-phase shell like /history - a plain GET is
        the whole recap. Nothing here may turn it into a placeholder."""
        self.db.getTopSongs.return_value = [_song("song1", "A Recorded Song")]

        body = self._page()

        self.assertIn("A Recorded Song", body)
        self.assertNotIn('hx-trigger="load"', body)


class TestOutOfBandRegions(WrappedHtmxTestCase):
    """The regions a year switch has to update that do not sit inside the swap
    container: the hero title above the filter form, the year badges above it,
    the form's own hidden year field, the export button's dataset inside it,
    and the share modal's panel beside it. Each was a
    document.querySelector(...).innerHTML = ... line in wrapped.js."""

    def test_the_fragment_marks_every_outside_region_out_of_band(self):
        body = self._fragment()

        for elementId in OOB_IDS:
            with self.subTest(elementId=elementId):
                self.assertIn('id="%s"' % elementId, body)
        self.assertEqual(body.count("hx-swap-oob"), len(OOB_IDS))

    def test_the_full_render_carries_no_out_of_band_markers(self):
        """The same partials render both ways. An hx-swap-oob left on the page
        itself would make htmx try to swap the element into itself on the next
        response that happened to contain it."""
        self.assertNotIn("hx-swap-oob", self._page())

    def test_the_hero_follows_the_year(self):
        body = self._fragment("?year=2024")

        hero = body[body.index('id="wrappedHero"'):]
        self.assertIn("2024 Wrapped", hero[:hero.index("</section>")])

    def test_the_year_badges_come_back_with_the_new_one_active(self):
        """The old handler moved the `active` class by hand; the server knows
        which year it just rendered, so the swapped-in nav carries it."""
        body = self._fragment("?year=2024")

        self.assertIn('wrapped-year-badge active"', body)
        badges = body[body.index('id="wrappedYearBadges"'):]
        active = badges[badges.index('wrapped-year-badge active"'):]
        self.assertIn(">2024<", active[:active.index("</a>") + len("</a>")])

    def test_the_forms_hidden_year_field_follows_the_year(self):
        """A later change to Items per category serializes the form, so a
        stale year field would silently ask for the year the user just left
        (the `hiddenYear.value = year` line in the old loader)."""
        body = self._fragment("?year=2024")

        field = body[body.index('id="wrappedYearField"'):]
        self.assertIn('value="2024"', field[:field.index(">")])

    def test_the_export_buttons_dataset_follows_the_year(self):
        """The PNG is drawn entirely from these data-* attributes, so an
        un-updated button exports the previous year's card under the new
        year's heading."""
        body = self._fragment("?year=2024")

        button = body[body.index('id="exportWrappedBtn"'):]
        self.assertIn('data-year="2024"', button[:button.index(">")])

    def test_the_share_panel_is_rebuilt_for_the_new_year(self):
        """Regression the old sharePanelHtml key existed for: the modal's
        create-form action URL, its revoke form's hidden year and which link
        counts as "current" are all year-scoped."""
        token = self.dash.repo.createShareLink(
            "alice", self.dash.repo.SHARE_LINK_KIND_WRAPPED, 2025, None)

        body = self._fragment("?year=2025")

        panel = body[body.index('id="shareLinkPanelBody"'):]
        self.assertIn("/shared/%s" % token, panel)
        self.assertIn("Revoke", panel)

    def test_no_share_panel_when_the_admin_has_disabled_share_links(self):
        """Matching the full render, which hides the whole modal - an
        out-of-band swap for an element that is not on the page is an
        htmx:oobErrorNoTarget, not a no-op."""
        self.dash.repo.setShareLinksEnabled(False)

        body = self._fragment()

        self.assertNotIn('id="shareLinkPanelBody"', body)


class TestGenreCardIsNotRecomputedPerFilterChange(WrappedHtmxTestCase):
    """The genre card's data depends on the YEAR and nothing else, so a trend-
    bucket / items-per-category / sort change must not pay for it.

    Collapsing the old `?ajax=true&type=chart|lists` partial modes into one
    fragment made every filter change run getGenreCoverage + getGenreDistribution
    again - two year-scoped aggregations for a card whose content could not have
    moved. The fix is not a cache: the filter form cannot change the year (its
    year input is hidden and rewritten out of band, and the year badges are
    links outside the form), so a request the FORM makes is exactly the case
    where the card is already correct. It marks itself, and the response sends
    an hx-preserve stub, which makes htmx keep the card already on the page.

    Deliberately NOT solved by caching coverage per (user, year): the numbers
    grow while the Last.fm backfill runs and the admin's inherited-genres toggle
    moves them retroactively, which is why _buildWrappedContext computes them
    live and never from the user_wrapped cache. This keeps them live and just
    stops asking when the answer cannot have changed."""

    #< what the form sends, and only the form: see wrapped.html's hx-headers
    FILTER_CHANGE = dict(HX_HEADERS, **{"X-Wrapped-Filter-Change": "1"})

    def test_a_filter_change_runs_no_genre_queries(self):
        self.db.getGenreCoverage.reset_mock()
        self.db.getGenreDistribution.reset_mock()

        self.client.get("/wrapped?groupBy=week&sortBy=name", headers=self.FILTER_CHANGE)

        self.db.getGenreCoverage.assert_not_called()
        self.db.getGenreDistribution.assert_not_called()

    def test_a_filter_change_preserves_the_card_already_on_the_page(self):
        """An innerHTML swap would otherwise delete it - skipping the queries
        without this would blank the card instead of leaving it alone."""
        body = self._fragmentFilterChange("?groupBy=week")

        self.assertIn('id="wrappedGenresCard"', body)
        self.assertIn("hx-preserve", body)

    def test_a_year_change_does_run_them_and_sends_real_content(self):
        """The one thing that CAN move the card. A year badge is a boosted link
        outside the form, so it carries no filter-change marker."""
        self.db.getGenreCoverage.reset_mock()

        body = self._fragment("?year=2024")

        self.db.getGenreCoverage.assert_called()
        self.assertIn('id="wrappedGenresCard"', body)
        self.assertNotIn("hx-preserve", body)

    def test_a_full_page_render_always_computes_them(self):
        """No card is on the page yet, so there is nothing to preserve - and a
        stub here would render the recap with a permanently empty genre card."""
        self.db.getGenreCoverage.reset_mock()

        body = self._page()

        self.db.getGenreCoverage.assert_called()
        self.assertNotIn("hx-preserve", body)

    def test_the_marker_alone_does_not_strip_the_card_from_a_full_render(self):
        """The header is only meaningful for a swap. A plain GET carrying it
        (a crafted request, a proxy replaying headers) must still render a whole
        usable page rather than one with a hollow card."""
        body = self.client.get("/wrapped", headers={"X-Wrapped-Filter-Change": "1"}).get_data(as_text=True)

        self.assertNotIn("hx-preserve", body)
        self.db.getGenreCoverage.assert_called()

    def test_the_form_still_cannot_change_the_year(self):
        """The whole inference rests on this. The form carries the year only as
        the hidden field that gets rewritten out of band; if a visible year
        control is ever added to it, a filter change COULD move the year and
        preserving the card would show the wrong year's genres."""
        page = self._page()
        #< from the filter form's own id, not from the first </form> on the page
        #  - layout.html's logout form closes long before this one opens
        start = page.index('id="wrappedFilters"')
        form = page[start:page.index("</form>", start)]

        self.assertIn('type="hidden" name="year"', form)
        #< the only year input in the form is that hidden one
        self.assertEqual(form.count('name="year"'), 1)
        self.assertNotIn("<select id=\"year\"", form)

    def _fragmentFilterChange(self, query=""):
        return self.client.get("/wrapped" + query,
                               headers=self.FILTER_CHANGE).get_data(as_text=True)


class TestUnauthenticatedSwap(WrappedHtmxTestCase):
    """An expired session mid-swap. htmx follows a 302 as transparently as
    fetch() did, so without HX-Redirect the login page would be swapped into
    the recap - the same bug wrappedPage's ajax 401 branch was added for,
    reachable again through a different client."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(self.dash, "is_user_logged_in", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_hx_request_gets_hx_redirect_rather_than_a_302(self):
        resp = self.client.get("/wrapped", headers=HX_HEADERS)

        self.assertNotIn(resp.status_code, (301, 302, 303, 307, 308))
        self.assertIn("/login", resp.headers.get("HX-Redirect", ""))

    def test_the_hx_redirect_body_is_empty(self):
        """htmx swaps the body of any 2xx, so anything here would be injected
        into the page before the redirect happens."""
        resp = self.client.get("/wrapped", headers=HX_HEADERS)

        self.assertEqual(resp.get_data(as_text=True), "")

    def test_the_hx_redirect_preserves_the_year_and_filters(self):
        resp = self.client.get("/wrapped?year=2024&sortBy=name", headers=HX_HEADERS)

        target = resp.headers.get("HX-Redirect", "")
        self.assertIn("year%3D2024", target)
        self.assertIn("sortBy%3Dname", target)

    def test_a_plain_get_still_redirects(self):
        resp = self.client.get("/wrapped")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers.get("Location", ""))


class TestSharedWrappedHtmx(PublicSharedWrappedTestCase):
    """The public twin. It renders through layout_public.html, which this
    migration must not edit - so the htmx tag has to come from wrapped.html's
    own content block, and both layouts have to keep loading ajax-status.js
    (the public one shipped without it once, which silently swallowed every
    error the page tried to report)."""

    def _sharedFragment(self, token, query="", db=None):
        client = self.dash.app.test_client()
        with patch.object(self.dash, '_getReadOnlyUserDb', return_value=db or self._makeDb()):
            return client.get("/shared/%s%s" % (token, query), headers=HX_HEADERS)

    def test_the_public_page_serves_htmx_from_this_origin(self):
        """layout_public.html is off limits, so the tag rides in the page's own
        block - which is also what keeps the two layouts in step."""
        token = self._createLink()

        body = self._getShared(token).get_data(as_text=True)

        self.assertIn("js/vendor/htmx.min.js", body)

    def test_the_public_page_still_loads_ajax_status(self):
        """It is a hard dependency of the failure banner the swap error
        handler renders, not an optional nicety."""
        token = self._createLink()

        body = self._getShared(token).get_data(as_text=True)

        self.assertIn("js/ajax-status.js", body)

    def test_an_hx_request_gets_the_fragment(self):
        token = self._createLink()

        resp = self._sharedFragment(token)

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.get_json(silent=True))
        self.assertNotIn("<html", resp.get_data(as_text=True).lower())

    def test_the_public_fragment_never_carries_a_share_panel(self):
        """Safety regression: an anonymous visitor must never receive
        share-panel data (create-link forms, existing tokens) for the owner's
        account, on any path."""
        token = self._createLink()

        body = self._sharedFragment(token).get_data(as_text=True)

        self.assertNotIn("shareLinkPanelBody", body)
        self.assertNotIn("Share this Wrapped", body)

    def test_the_public_fragment_has_no_export_button_either(self):
        token = self._createLink()

        body = self._sharedFragment(token).get_data(as_text=True)

        self.assertNotIn("exportWrappedBtn", body)

    def test_the_public_fragment_keeps_the_token_keyed_image_route(self):
        """An anonymous viewer can't authorize against /img/<owner>/..., so a
        swapped-in card falling back to it shows placeholder artwork."""
        token = self._createLink()
        db = self._makeDb()
        db.getTopSongs.return_value = [_song("song1", "Song")]

        body = self._sharedFragment(token, db=db).get_data(as_text=True)

        self.assertIn('src="/shared/%s/img/tracks/img1.jpeg"' % token, body)
        self.assertNotIn('src="/img/alice/', body)

    def test_a_single_year_link_sends_no_year_badge_swap(self):
        """Its badges are never rendered, so an out-of-band nav in the
        response would have nothing to swap into (htmx:oobErrorNoTarget)."""
        token = self._createLink(year=2026)

        body = self._sharedFragment(token).get_data(as_text=True)

        self.assertNotIn("wrappedYearBadges", body)

    def test_a_multi_year_link_does_send_one(self):
        token = self._createLink(year=None)
        db = self._makeDb()
        db.getEntriesFromOld.return_value = [{"playedAt": _ts(2023), "timePlayed": 1}]

        body = self._sharedFragment(token, db=db).get_data(as_text=True)

        self.assertIn('id="wrappedYearBadges"', body)
        self.assertIn("hx-swap-oob", body)

    def test_a_revoked_token_still_404s_for_an_htmx_request(self):
        """This route is NOT behind @requiresUser: a dead share token is a
        missing page, not an expired session. Answering it with the shared
        HX-Redirect would send an anonymous visitor to a login screen for an
        account that is not theirs."""
        token = self._createLink()
        self.dash.repo.revokeShareLink(self.dash.repo.getShareLink(token)["id"], "alice")

        resp = self._sharedFragment(token)

        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("HX-Redirect", resp.headers)

    def test_an_unknown_token_404s_for_an_htmx_request_too(self):
        resp = self._sharedFragment("does-not-exist")

        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("HX-Redirect", resp.headers)

    def test_a_plain_get_on_a_dead_token_is_unchanged(self):
        """Control for the two above: the non-htmx path keeps the same 404."""
        resp = self._getShared("does-not-exist")

        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
