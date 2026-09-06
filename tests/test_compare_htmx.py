"""The /compare page's htmx contract (routes/compare.py, templates/compare.html,
templates/_compare_results.html, templates/_compare_sortable_lists.html,
static/js/compare.js).

/compare follows /history off the hand-rolled fetch/swap layer, and it is the
first page whose second request refreshes MANY regions instead of one. That is
what most of this file is about:

- the response is HTML, not ``{"statsTableHtml": ..., "tasteMatch": ...}``.
  htmx swaps response bodies, so a JSON envelope would land in the page verbatim.
- with no single swap target there is no primary swap at all: every region is
  named by ``hx-swap-oob`` inside the one fragment, and the shell asks for
  ``hx-swap="none"``. A region missing from the fragment is the failure mode
  worth pinning - it does not error, it silently keeps stale content under a URL
  that already says otherwise.
- the NARROW refresh survives. A Sort by change still asks for
  ``?scope=sortable`` and still gets only the six individual my/their lists, so
  the shared lists, taste match, genres and trend - the expensive half on long
  ranges - are not recomputed to render identically.
- the URL updates by REPLACE, never push: an in-page filter change must not
  stack a history entry, so Back leaves the page. ``?scope=sortable`` is kept out
  of the address bar by answering that branch with an explicit ``HX-Replace-Url``
  - it is a transport detail, not part of the link people copy.
- a logged-out swap gets ``HX-Redirect``, and so does a swap whose share was
  revoked mid-session: htmx follows a redirect as transparently as fetch() did,
  and a full page answered into a swap either lands in the page or (with
  ``hx-swap="none"``) vanishes without a trace.

The page CONTENT - taste-match scoring, shared-list ranking, the stats table,
the authorization boundary on ?with= - is covered by tests/test_compare_route.py
against this same fragment; what is here is the transport.

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
from html.parser import HTMLParser
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase
from routes.compare import COMPARE_SORTABLE_SCOPE

#< what htmx puts on every request it makes
HX_HEADERS = {"HX-Request": "true"}

# Every region of the shell the full refresh replaces, by the id it is found
# under. Listed here rather than derived so that a region QUIETLY dropping out
# of the fragment fails - which is the whole failure mode out-of-band swaps
# have: htmx only complains about an oob element with no target, never about a
# target with no oob element, so a forgotten region just keeps stale content.
FULL_SWAP_REGION_IDS = (
    "compareStatsTable",
    "compareSimilarities",
    "compareGenres",
    "sharedSongsList",
    "sharedArtistsList",
    "sharedAlbumsList",
    "myTopSongsList", "theirTopSongsList",
    "myTopArtistsList", "theirTopArtistsList",
    "myTopAlbumsList", "theirTopAlbumsList",
    "tasteMatch",
    "compareTrendData",
    #< the hidden field carrying ?with= into every filter request, so switching
    #  counterpart and then changing a filter doesn't snap back
    "compareWithField",
)

# The narrow one: a Sort by change reorders only the individual my/their lists.
SORTABLE_REGION_IDS = (
    "myTopSongsList", "theirTopSongsList",
    "myTopArtistsList", "theirTopArtistsList",
    "myTopAlbumsList", "theirTopAlbumsList",
)


#< elements with no closing tag, so the depth walk below must not descend into
#  them. Only the ones this fragment can contain.
VOID_ELEMENTS = {"input", "img", "br", "hr", "meta", "link", "source", "col", "area"}


def oobMarkers(fragment):
    """The ids the fragment declares an out-of-band swap for."""
    return set(re.findall(r'id="([^"]+)"[^>]*\bhx-swap-oob=', fragment))


class _OobDepthParser(HTMLParser):
    """Where each hx-swap-oob element sits in the fragment's tree."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.topLevel = []
        self.nested = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "hx-swap-oob" in attributes:
            found = attributes.get("id", tag)
            (self.topLevel if self.depth == 0 else self.nested).append(found)
        if tag not in VOID_ELEMENTS:
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_ELEMENTS:
            self.depth -= 1

    def handle_endtag(self, tag):
        if tag not in VOID_ELEMENTS:
            self.depth -= 1


def _artist(artistId, name, **extra):
    return {"id": artistId, "name": name, **extra}


def _zeroHeatmapGrid():
    return [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]


class CompareHtmxTestCase(AppTestCase):
    def _makeStubDb(self):
        db = MagicMock()
        db.tz = None
        db.getPlayTotals.return_value = (0, 0)
        db.getTopSongs.return_value = []
        db.getTopArtists.return_value = []
        db.getTopAlbums.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getSongsCount.return_value = 0
        db.getArtistsCount.return_value = 0
        db.getCompletionStats.return_value = {"skips": 0, "completes": 0, "partials": 0}
        db.getExplicitRatio.return_value = {"explicit": 0, "clean": 0}
        db.getHourOfDayHeatmap.return_value = _zeroHeatmapGrid()
        return db

    def setUp(self):
        self.dash = self._makeApp()
        for username in ("alice", "bob", "carol"):
            self.dash.repo.upsertUser(username, f"{username}@example.com")
            self.dash.repo.setUserCookies(username, {"sp_dc": "test"})
        self.dbs = {u: self._makeStubDb() for u in ("alice", "bob", "carol")}
        self._accept("alice", "bob")
        # The counterpart's db comes from _getReadOnlyUserDb, not get_user_db
        # (2026-09-04 review, C1 - see routes/compare.py's comparePage);
        # AppTestCase._loginAs only stubs get_user_db, so without this an
        # unmocked _getReadOnlyUserDb would construct a REAL Database for
        # the counterpart instead of handing back self.dbs[...].
        patch.object(self.dash, '_getReadOnlyUserDb', side_effect=lambda u: self.dbs[u]).start()
        self.addCleanup(patch.stopall)

    def _accept(self, requester, recipient):
        self.dash.repo.createShareRequest(requester, recipient)
        shareId = self.dash.repo.getPendingIncomingShares(recipient)[0]["id"]
        self.dash.repo.respondToShareRequest(shareId, recipient, accept=True)


    def _shell(self, url="/compare"):
        return self._loginAs().get(url).get_data(as_text=True)

    def _fragment(self, url="/compare", client=None):
        client = client or self._loginAs()
        return client.get(url, headers=HX_HEADERS).get_data(as_text=True)


class TestFragmentBranch(CompareHtmxTestCase):
    def test_an_hx_request_gets_html_not_json(self):
        resp = self._loginAs().get("/compare", headers=HX_HEADERS)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        self.assertIsNone(resp.get_json(silent=True))

    def test_the_old_json_envelope_is_gone(self):
        """The keys the previous fetch() layer read. Their absence is the point:
        htmx would swap the literal JSON into the page."""
        body = self._fragment()

        for key in ("statsTableHtml", "similaritiesHtml", "genresHtml",
                    "sharedArtistsHtml", "myTopSongsHtml", "theirTopAlbumsHtml"):
            self.assertNotIn(key, body)

    def test_the_fragment_is_not_a_whole_document(self):
        """htmx distributes this response over the shell's regions. A full page
        here would nest a second <html>/<body> and the site chrome inside one."""
        body = self._fragment()

        self.assertNotIn("<html", body.lower())
        self.assertNotIn("<body", body.lower())

    def test_the_fragment_carries_the_rendered_lists(self):
        self.dbs["alice"].getTopSongs.return_value = [
            {"id": "s1", "name": "MyFragmentSong", "artists": [], "duration": 60000}]

        body = self._fragment()

        self.assertIn("MyFragmentSong", body)

    def test_ajax_true_alone_no_longer_triggers_the_fragment(self):
        """?ajax=true was the old marker. Without HX-Request it is just an
        unknown query param, and the page renders its shell."""
        resp = self._loginAs().get("/compare?ajax=true")

        self.assertIsNone(resp.get_json(silent=True))
        self.assertIn('id="compareStatsTable"', resp.get_data(as_text=True))


class TestOutOfBandRegions(CompareHtmxTestCase):
    def test_every_region_the_shell_renders_is_named_by_the_fragment(self):
        """The failure mode out-of-band swaps have: htmx errors on an oob
        element with no target, but never on a target with no oob element - a
        forgotten region just keeps stale content under a URL that already
        changed to say otherwise."""
        self._accept("alice", "carol")   #< so the counterpart badges render too
        client = self._loginAs()
        shell = client.get("/compare").get_data(as_text=True)
        fragment = self._fragment(client=client)

        declared = oobMarkers(fragment)
        for regionId in FULL_SWAP_REGION_IDS + ("compareUserBadges",):
            self.assertIn(f'id="{regionId}"', shell, f"{regionId} is not in the shell")
            self.assertIn(regionId, declared, f"{regionId} is never swapped")

    def test_the_picker_is_rendered_even_with_nobody_to_switch_to(self):
        """The shell and the fragment are two different requests, and each used
        to decide "picker or no picker" for itself. A share accepted between
        them made the fragment emit an out-of-band element whose target the
        shell had never rendered - htmx:oobErrorNoTarget, and the picker never
        appeared until a full reload (2026-09-03 review, L6)."""
        client = self._loginAs()   #< alice has exactly one accepted share

        shell = client.get("/compare").get_data(as_text=True)
        fragment = self._fragment(client=client)

        self.assertIn('id="compareUserBadges"', shell)
        self.assertIn('id="compareUserBadges"', fragment)

    def test_a_lone_counterpart_hides_the_picker_rather_than_dropping_it(self):
        """Hidden, not absent: there is nothing to pick, so it must not be on
        screen or in the accessibility tree - but the swap still needs a
        target."""
        shell = self._shell()

        nav = shell[shell.index('id="compareUserBadges"'):]
        self.assertIn("hidden", nav[:nav.index(">")])

    def test_a_second_counterpart_shows_it(self):
        self._accept("alice", "carol")

        shell = self._shell()

        nav = shell[shell.index('id="compareUserBadges"'):]
        self.assertNotIn("hidden", nav[:nav.index(">")])

    def test_a_share_accepted_mid_session_makes_the_picker_appear(self):
        """The case that broke: the shell was fetched while alice had one
        share, carol was accepted, and the NEXT fragment has to be able to put
        a working picker on that page."""
        client = self._loginAs()
        shell = client.get("/compare").get_data(as_text=True)
        self.assertIn('id="compareUserBadges"', shell)   #< the target exists

        self._accept("alice", "carol")
        fragment = self._fragment(client=client)

        nav = fragment[fragment.index('id="compareUserBadges"'):]
        opening = nav[:nav.index(">")]
        self.assertIn('hx-swap-oob="outerHTML"', opening)
        self.assertNotIn("hidden", opening)
        self.assertIn("with=carol", nav[:nav.index("</nav>")])

    def test_the_counterpart_name_labels_are_swapped_by_class_not_by_id(self):
        """The name appears in the hero and above all three of the counterpart's
        columns; they are not swap targets of their own, so one oob element
        addresses the lot by class."""
        fragment = self._fragment()

        self.assertIn('hx-swap-oob="innerHTML:.js-with-username"', fragment)

    def test_the_trend_data_rides_along_as_a_json_island(self):
        """The chart data is not HTML, and htmx swaps HTML. It travels as a data
        island the afterSwap listener in compare.js repaints the canvas from."""
        self.dbs["alice"].getListeningTimeSeries.return_value = [
            {"label": "2026-01-01", "totalTimeListened": 100, "plays": 1}]

        fragment = self._fragment()

        island = re.search(r'<script type="application/json" id="compareTrendData"'
                           r'[^>]*>(.*?)</script>', fragment, re.S)
        self.assertIsNotNone(island, "no trend data island in the fragment")
        trend = json.loads(island.group(1))
        self.assertEqual(trend["buckets"], ["2026-01-01"])
        self.assertEqual(len(trend["series"]), 2)

    def test_every_oob_element_sits_at_the_top_of_the_fragment(self):
        """htmx only descends into nested elements looking for oob swaps while
        htmx.config.allowNestedOobSwaps is on. It defaults on, but a page whose
        regions silently stop updating when someone turns a config flag off is
        not a page anyone can debug - so the fragment does not rely on it."""
        self._accept("alice", "carol")   #< the badges nav is oob too
        parser = _OobDepthParser()
        parser.feed(self._fragment())

        self.assertEqual(parser.nested, [])
        self.assertNotEqual(parser.topLevel, [])

    def test_the_shell_carries_the_island_the_swap_replaces(self):
        """An oob swap needs a target: without the placeholder htmx drops the
        trend data on the floor and the chart never leaves its empty state."""
        shell = self._shell()

        self.assertIn('id="compareTrendData"', shell)


class TestSortableScope(CompareHtmxTestCase):
    def _sortable(self, url=f"/compare?scope={COMPARE_SORTABLE_SCOPE}"):
        return self._loginAs().get(url, headers=HX_HEADERS)

    def test_it_swaps_only_the_six_individual_lists(self):
        body = self._sortable().get_data(as_text=True)

        self.assertEqual(oobMarkers(body), set(SORTABLE_REGION_IDS))

    def test_it_skips_the_work_those_lists_do_not_need(self):
        """The shared lists, similarities, genres, taste match and trend render
        identically under any sortBy and are the expensive half on long ranges -
        observable via the trend series queries never running."""
        self._sortable()

        self.dbs["alice"].getListeningTimeSeries.assert_not_called()
        self.dbs["bob"].getListeningTimeSeries.assert_not_called()

    def test_an_unknown_scope_still_degrades_to_the_full_refresh(self):
        """Only the exact scope the frontend sends narrows the response."""
        body = self._sortable("/compare?scope=bogus").get_data(as_text=True)

        self.assertIn("sharedArtistsList", oobMarkers(body))

    def test_the_scope_marker_is_kept_out_of_the_address_bar(self):
        """hx-replace-url writes back the URL that was REQUESTED, so the
        transport marker would otherwise become part of the link people copy.
        The route answers with the canonical URL instead."""
        resp = self._sortable(f"/compare?sortBy=name&scope={COMPARE_SORTABLE_SCOPE}")

        replaced = resp.headers.get("HX-Replace-Url", "")
        self.assertIn("sortBy=name", replaced)
        self.assertNotIn("scope", replaced)

    def test_the_full_refresh_leaves_the_url_to_the_attribute(self):
        """Its request URL is already the canonical one - it is built from the
        form's own fields - so nothing has to correct it."""
        resp = self._loginAs().get("/compare", headers=HX_HEADERS)

        self.assertNotIn("HX-Replace-Url", resp.headers)


class TestShell(CompareHtmxTestCase):
    def test_the_shell_serves_htmx_from_this_origin(self):
        """config.py's Content-Security-Policy allows script-src 'self' only, so
        a CDN tag would be blocked and the page would silently never load."""
        shell = self._shell()

        self.assertIn("js/vendor/htmx.min.js", shell)
        #< rangeProblem lives there, shared with /history and the Top lists
        self.assertIn("js/htmx-filters.js", shell)

    def test_the_shell_asks_for_no_primary_swap(self):
        """Every region is named by hx-swap-oob in the response, so there is no
        "main" one to pick - saying none is what stops htmx swapping the
        left-over whitespace into whatever element happened to fire."""
        shell = self._shell()

        self.assertIn('hx-swap="none"', shell)

    def test_filter_changes_replace_the_url_and_never_push_it(self):
        """The invariant the hand-written replaceCompareUrl existed for: an
        in-page filter change must not stack a history entry, so Back leaves
        the page instead of stepping through every filter that was tried."""
        shell = self._shell()

        self.assertIn('hx-replace-url="true"', shell)
        self.assertNotIn("hx-push-url", shell)

    def test_a_superseded_request_is_aborted(self):
        """hx-sync ...:replace is what the AbortController bookkeeping in the
        old loadCompareData did by hand. One queue for all three request
        shapes, because a sort change used to abort a pending filter load too.

        Scoped to the FORM element itself (not a substring anywhere in the
        shell) - #sortBy carries a DIFFERENT hx-sync value on the same
        queue, see test_sort_by_queues_behind_a_refresh_rather_than_aborting_it,
        so a plain substring check could no longer tell the two apart."""
        import bs4
        shell = self._shell()

        soup = bs4.BeautifulSoup(shell, "html.parser")
        self.assertEqual(soup.select_one("#compareFilters")["hx-sync"], "#compareFilters:replace")

    def test_sort_by_queues_behind_a_refresh_rather_than_aborting_it(self):
        """#sortBy inherits hx-sync from the form unless overridden, and it
        must be: a sort request's response carries only the six sortable
        lists, not the whole page. Left on the form's ...:replace (or with
        none of its own), changing the sort while a full refresh is in
        flight - the first-load placeholder or a filter change - would
        ABORT that refresh, whose six "Loading..." placeholders would then
        never resolve, since the sort response cannot stand in for one
        (reproduced in a real browser - see plan.md R2).

        Rejected alternatives:
        - "#sortBy:replace" (its own queue): a sort issued before a filter
          change could then land AFTER the refresh and repaint the lists
          with stale data.
        - "drop": the select would silently show a sort the lists were
          never asked to apply.

        "queue last" on the SAME #compareFilters queue instead: when a
        filter change lands while a sort is queued, the form's ...:replace
        aborts whatever is in flight and the queue drains to the (last)
        queued sort; the form's own refresh then queues behind THAT - two
        requests fire in a fixed order and the end state is correct. htmx
        only collects a queued request's hx-include values when it is
        actually issued, so the queued sort reflects the filters as they
        stand then, not as they stood when it was queued.

        This is an attribute test - it pins the markup, not the abort
        semantics above, which were verified by hand in a browser."""
        import bs4
        shell = self._shell()

        soup = bs4.BeautifulSoup(shell, "html.parser")
        self.assertEqual(soup.select_one("#sortBy")["hx-sync"], "#compareFilters:queue last")

    def test_the_first_load_placeholder_still_aborts_on_a_refresh(self):
        """The first-load placeholder issues a full refresh too (it is what
        populates the stats table), so it keeps the form's ...:replace
        rather than adopting #sortBy's queued strategy."""
        import bs4
        shell = self._shell()

        soup = bs4.BeautifulSoup(shell, "html.parser")
        placeholder = soup.select_one("#compareStatsTable p.loading[hx-get]")
        self.assertIsNotNone(placeholder)
        self.assertEqual(placeholder["hx-sync"], "#compareFilters:replace")

    def test_the_placeholder_triggers_the_first_load(self):
        """The shell renders no data; something has to ask for it. It lives on
        the placeholder INSIDE a swapped region, so it fires once and is then
        swapped away - no separate "have we loaded yet" state."""
        shell = self._shell()

        self.assertIn('hx-trigger="load"', shell)
        #< that one request asks for the canonical form of the URL the browser
        #  already shows; rewriting the address bar on first paint would be a
        #  silent redirect
        self.assertIn('hx-replace-url="false"', shell)

    def test_the_first_load_carries_the_filters_from_a_shared_url(self):
        """A link to /compare?interval=week&limit=25 has to open on THAT
        comparison, not on an unfiltered one that then disagrees with the
        controls above it."""
        shell = self._shell("/compare?interval=week&limit=25")

        loadUrl = re.search(r'hx-get="([^"]*)"[^>]*hx-trigger="load"', shell).group(1)
        self.assertIn("interval=week", loadUrl)
        self.assertIn("limit=25", loadUrl)

    def test_the_first_load_url_holds_no_unvalidated_input(self):
        """The route coerces a junk interval and a junk limit for the query;
        echoing the raw ones into the load URL would assert them again in the
        markup and disagree with the badge links built beside it."""
        shell = self._shell("/compare?interval=bogus&limit=13&sortBy=nonsense&groupBy=century")

        self.assertNotIn("bogus", shell)
        self.assertNotIn("nonsense", shell)
        self.assertNotIn("century", shell)
        self.assertNotIn("limit=13", shell)

    def test_the_first_load_keeps_an_empty_interval_even_though_it_is_blank(self):
        """The one param whose blank value is a VALUE: All Time is "", while an
        ABSENT interval falls back to the user's saved default_dashboard_window
        - so pruning it would silently move an All Time view to Last Month."""
        self.dash.repo.updateUserSettings("alice", "month", None)

        shell = self._shell("/compare?interval=")

        loadUrl = re.search(r'hx-get="([^"]*)"[^>]*hx-trigger="load"', shell).group(1)
        self.assertIn("interval=", loadUrl)
        self.assertNotIn("interval=month", loadUrl)

    def test_the_first_load_drops_a_range_that_is_not_in_effect(self):
        """Matching the disabled date inputs: a custom range only scopes the
        comparison while both dates are set, so carrying half of one would ask
        for a view the form says is not selected."""
        shell = self._shell("/compare?interval=week&startDate=2026-01-01")

        self.assertNotIn("startDate=2026-01-01", shell)

    def test_the_counterpart_badges_are_boosted_and_carry_the_current_filters(self):
        """Switching counterpart is the same in-place refresh every other
        control does. The href is the whole request now (it used to be read for
        ?with= alone), so a stale one would silently reset the other filters."""
        self._accept("alice", "carol")

        shell = self._shell("/compare?interval=week&limit=25")

        start = shell.index('id="compareUserBadges"')
        nav = shell[start:shell.index("</nav>", start)]   #< the layout's own nav closes earlier
        self.assertIn('hx-boost="true"', nav)
        self.assertIn("with=carol", nav)
        self.assertIn("interval=week", nav)
        self.assertIn("limit=25", nav)

    def test_the_date_inputs_are_disabled_until_a_custom_range_is_in_effect(self):
        """A disabled control is not serialized, which is what keeps a stale
        custom range out of the request - and so out of the URL, since
        hx-replace-url writes back what was requested."""
        shell = self._shell()

        customDates = shell[shell.index('id="compareCustomDates"'):]
        customDates = customDates[:customDates.index("</div>")]
        self.assertEqual(customDates.count("disabled"), 2)

    def test_a_custom_range_in_effect_enables_them(self):
        shell = self._shell("/compare?interval=custom&startDate=2026-01-01&endDate=2026-01-31")

        customDates = shell[shell.index('id="compareCustomDates"'):]
        customDates = customDates[:customDates.index("</div>")]
        self.assertNotIn("disabled", customDates)


class TestRevokedShareMidSession(CompareHtmxTestCase):
    """The mutual-accept model is what makes this page exist at all, and the
    counterpart can withdraw at any moment. The route's answer is unchanged -
    it renders no comparison - but a full page answered INTO a swap is worse
    than useless: with hx-swap="none" it would vanish silently, leaving the
    stale comparison of a share that no longer exists on screen."""

    def _revoke(self):
        shares = self.dash.repo.getAcceptedShares("alice")
        self.assertTrue(self.dash.repo.revokeShare(shares[0]["id"], "alice"))

    def test_a_swap_after_the_share_is_gone_redirects_instead_of_swapping(self):
        client = self._loginAs()
        self._revoke()

        resp = client.get("/compare", headers=HX_HEADERS)

        self.assertNotIn(resp.status_code, (301, 302, 303, 307, 308))
        self.assertIn("/compare", resp.headers.get("HX-Redirect", ""))
        self.assertEqual(resp.get_data(as_text=True), "")

    def test_the_swap_never_answers_with_another_users_data(self):
        """The authorization boundary, from the htmx side: the fragment is
        built for the counterpart the route resolved, never the requested one."""
        self._accept("bob", "carol")   #< carol shares with bob, NOT with alice
        client = self._loginAs()

        fragment = client.get("/compare?with=carol", headers=HX_HEADERS).get_data(as_text=True)

        self.dbs["carol"].getPlayTotals.assert_not_called()
        self.dbs["bob"].getPlayTotals.assert_called()
        self.assertNotIn("carol", fragment)

    def test_a_plain_get_still_renders_the_empty_state(self):
        client = self._loginAs()
        self._revoke()

        resp = client.get("/compare")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("haven't connected with anyone yet", resp.get_data(as_text=True))

    def test_the_instance_wide_kill_switch_still_wins(self):
        client = self._loginAs()
        self.dash.repo.setDataSharingEnabled(False)

        resp = client.get("/compare", headers=HX_HEADERS)

        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()

