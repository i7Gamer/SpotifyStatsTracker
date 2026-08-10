import unittest
from unittest.mock import patch, MagicMock
import re
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# NOTE: unlike some sibling test modules, this file deliberately does NOT swap
# Database modules for MagicMocks in sys.modules. These tests only exercise the
# dashboard route with a per-test mock db (via get_user_db), so module mocks are
# unnecessary - and a module-level mock/restore here would poison the
# patch("Database.database...") targets of test files that run after this one.
import app as appModule
from app import SpotifyDashboardApp
from _app_factory import AppTestCase
from conftest import makeDashboardDbMock


class _ListRouteTestBase(AppTestCase):
    """Shared fixtures for exercising the list routes with a mocked per-user db."""

    def _makeDb(self, entryCount):
        #< the dashboard route's baseline comes from conftest; the list-page stubs
        #  below are what THESE tests (pagination across the Top pages) add
        db = makeDashboardDbMock()
        db.repo.getUserTags.return_value = []
        db.getEntriesFromNew.return_value = []
        db.getEntriesFromOld.return_value = []
        db.getEntriesCount.return_value = entryCount
        db.searchEntries.return_value = []
        db.searchEntriesCount.return_value = 0
        db.getTopSongs.return_value = []
        db.getSongsCount.return_value = 0
        db.getTopArtists.return_value = []
        db.getArtistsCount.return_value = 0
        db.getArtistTotals.return_value = (0, 0, 0)
        return db

    def _getPath(self, dash, db, path, headers=None):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(path, headers=headers or {})

    def _getHistory(self, dash, db, query=""):
        return self._getPath(dash, db, f"/history{query}")

    def _getHistoryList(self, dash, db, query=""):
        """/history is a two-phase load (see routes/charts.py's historyPage) -
        the list/pagination content only comes back for the second request.

        That request is made by htmx on this page, so it is marked with the
        HX-Request header rather than the ?ajax=true the other shell pages
        still use, and the response is the HTML fragment itself rather than a
        JSON envelope around it. The transport is pinned in
        tests/test_history_htmx.py; here it is just how you reach the list.

        Returns (resp, listHtml)."""
        resp = self._getPath(dash, db, f"/history{query}", headers={"HX-Request": "true"})
        return resp, resp.get_data(as_text=True)

    def _getTopSongs(self, dash, db, query=""):
        # The Top lists are a two-phase load like /history, and migrated with
        # it: the list/pagination/totals only come back for an HX-Request, and
        # the response is the fragment rather than a JSON envelope.
        return self._getPath(dash, db, f"/top-songs{query}", headers={"HX-Request": "true"})

    def _getTopArtists(self, dash, db, query=""):
        return self._getPath(dash, db, f"/top-artists{query}", headers={"HX-Request": "true"})


class TestHistoryPagination(_ListRouteTestBase):
    """The play-history list lives on /history now. Without a search query it
    must only materialize the page being shown - joining full track metadata
    onto every entry ever recorded on every request gets slow once the history
    grows large."""

    def test_history_page_hosts_the_search_box(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getHistory(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'id="historySearch"', resp.data)
        self.assertIn(b'name="q"', resp.data)

    def test_history_track_card_shows_played_at(self):
        """The play-history cards must still show the "Played at" timestamp on
        /history (section='history'), the way the dashboard list used to
        (section='dashboard') - see templates/_track_card.html."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=1)
        db.getEntriesFromNew.return_value = [
            {"id": "t1", "name": "Test Song", "playedAtText": "20 Jul 2026, 15:30", "artists": []}]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs), \
             patch.object(dash, "_attachGenres", side_effect=lambda db, tracks, kind: tracks):
            resp, resultsHtml = self._getHistoryList(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("Test Song", resultsHtml)
        self.assertIn("Played at 20 Jul 2026, 15:30", resultsHtml)

    def test_without_search_fetches_only_one_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp, resultsHtml = self._getHistoryList(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=0, startDate=None, endDate=None, trackIds=None,
                                                     includeSkips=False, fullPlaysOnly=True)
        self.assertIn("Page 1 of 3", resultsHtml)

    def test_without_search_requests_correct_offset_for_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp, resultsHtml = self._getHistoryList(dash, db, query="?page=2")

        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=appModule.PAGE_SIZE, startDate=None, endDate=None, trackIds=None,
                                                     includeSkips=False, fullPlaysOnly=True)
        self.assertIn("Page 2 of 3", resultsHtml)

    def test_without_search_clamps_page_beyond_range(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp, resultsHtml = self._getHistoryList(dash, db, query="?page=99")

        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=2 * appModule.PAGE_SIZE, startDate=None, endDate=None, trackIds=None,
                                                     includeSkips=False, fullPlaysOnly=True)
        self.assertIn("Page 3 of 3", resultsHtml)

    def test_a_page_number_too_long_to_parse_is_junk_not_a_crash(self):
        """?page= reaches _positivePageArg as text, and it answered by calling
        int() on anything that isdigit(). CPython refuses to convert a string
        of more than 4300 digits (its int/str conversion limit), so a long
        enough page number was an unhandled ValueError - a 500 on the shell of
        /history and of all three Top pages alike, from a URL anyone can type.

        It is junk, and junk is already handled here: left out rather than
        echoed."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)
        absurd = "?page=" + "9" * 5000

        #< the SHELL, not the list: the list path reads ?page= through
        #  _getPageParam, which has always caught the conversion
        for path in ("/history", "/top-songs"):
            with self.subTest(path=path):
                resp = self._getPath(dash, db, f"{path}{absurd}")

                self.assertEqual(resp.status_code, 200)

    def test_without_search_handles_empty_database(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp, resultsHtml = self._getHistoryList(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=0, startDate=None, endDate=None, trackIds=None,
                                                     includeSkips=False, fullPlaysOnly=True)
        self.assertIn("Page 1 of 1", resultsHtml)

    def test_with_search_paginates_and_matches_in_sql(self):
        """Search is pushed into SQL (Repository.searchPlays) and paginated
        the same way as the non-search path - it must not fetch or count the
        unfiltered history at all."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)
        db.searchEntriesCount.return_value = 5

        resp, _ = self._getHistoryList(dash, db, query="?q=foo")

        self.assertEqual(resp.status_code, 200)
        db.searchEntriesCount.assert_called_once_with("foo", startDate=None, endDate=None, trackIds=None,
                                                      includeSkips=False, fullPlaysOnly=True)
        db.searchEntries.assert_called_once_with("foo", count=appModule.PAGE_SIZE, startIndex=0, startDate=None, endDate=None,
                                                 oldestFirst=False, trackIds=None,
                                                 includeSkips=False, fullPlaysOnly=True)
        db.getEntriesFromNew.assert_not_called()
        db.getEntriesCount.assert_not_called()

    def test_search_page_beyond_range_is_clamped_to_last_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.searchEntriesCount.return_value = 120

        resp, resultsHtml = self._getHistoryList(dash, db, query="?q=foo&page=9999")

        self.assertEqual(resp.status_code, 200)
        db.searchEntries.assert_called_once_with("foo", count=appModule.PAGE_SIZE, startIndex=2 * appModule.PAGE_SIZE, startDate=None, endDate=None,
                                                 oldestFirst=False, trackIds=None,
                                                 includeSkips=False, fullPlaysOnly=True)
        self.assertIn("Page 3 of 3", resultsHtml)

    def test_sort_oldest_fetches_entries_from_old(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp, _ = self._getHistoryList(dash, db, query="?sort=oldest")

        self.assertEqual(resp.status_code, 200)
        db.getEntriesFromOld.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=0, startDate=None, endDate=None, trackIds=None,
                                                     includeSkips=False, fullPlaysOnly=True)
        db.getEntriesFromNew.assert_not_called()

    def test_sort_junk_falls_back_to_newest(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp, _ = self._getHistoryList(dash, db, query="?sort=bogus")

        self.assertEqual(resp.status_code, 200)
        db.getEntriesFromNew.assert_called_once()
        db.getEntriesFromOld.assert_not_called()

    def test_search_with_sort_oldest_passes_oldest_first(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.searchEntriesCount.return_value = 5

        resp, _ = self._getHistoryList(dash, db, query="?q=foo&sort=oldest")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(db.searchEntries.call_args.kwargs.get("oldestFirst"))

    def test_pagination_links_carry_sort(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp, resultsHtml = self._getHistoryList(dash, db, query="?sort=oldest")

        self.assertIn("sort=oldest", resultsHtml)

    def test_sort_button_renders_in_shell_with_current_order(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        newest = self._getHistory(dash, db)
        oldest = self._getHistory(dash, db, query="?sort=oldest")

        self.assertIn('id="historySort"'.encode(), newest.data)
        self.assertIn("Date ↓".encode(), newest.data)
        self.assertIn("Date ↑".encode(), oldest.data)


class TestHistoryAjaxShell(_ListRouteTestBase):
    """/history is a two-phase load like /compare, /charts, /genres: the plain
    GET is just the filter form + an empty #historyResults placeholder, and the
    real list arrives in a second request right after first paint - see
    routes/charts.py's historyPage.

    Unlike the others, this page makes that second request with htmx, so it is
    marked with the HX-Request header and answered with the fragment itself.
    What these two tests pin is the SPLIT - the shell must not query the list,
    and the fragment must not be a whole page - which the change of transport
    leaves untouched. The transport is pinned in tests/test_history_htmx.py."""

    def test_shell_renders_the_placeholder_without_querying_the_list(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp = self._getHistory(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'id="historyResults"', resp.data)
        self.assertIn(b'id="historySearch"', resp.data)   #< the filter form still renders
        db.getEntriesFromNew.assert_not_called()
        db.getEntriesCount.assert_not_called()
        db.searchEntries.assert_not_called()
        db.searchEntriesCount.assert_not_called()

    def test_the_second_request_returns_a_partial_not_a_full_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp, listHtml = self._getHistoryList(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("track-list", listHtml)
        self.assertNotIn("<html", listHtml.lower())
        self.assertNotIn('id="historySearch"', listHtml)   #< the filter form isn't part of this chunk


class TestHistoryCustomRangeListScoping(_ListRouteTestBase):
    """The Time Period filter scopes the play-history list for every interval:
    a custom date range (the querystring shape a chart click-through produces -
    see static/js/charts.js) and a named interval (day/week/...) alike. A plain
    visit defaults to All Time, which resolves to (None, None) - the full
    history."""

    def test_custom_range_scopes_the_list(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp, _ = self._getHistoryList(dash, db, query="?interval=custom&startDate=2026-07-01&endDate=2026-07-05")

        self.assertEqual(resp.status_code, 200)
        kwargs = db.getEntriesFromNew.call_args.kwargs
        self.assertIsNotNone(kwargs["startDate"])
        self.assertIsNotNone(kwargs["endDate"])
        self.assertEqual(kwargs["startDate"].date(), appModule.datetime(2026, 7, 1).date())
        countKwargs = db.getEntriesCount.call_args.kwargs
        self.assertIsNotNone(countKwargs["startDate"])
        self.assertIsNotNone(countKwargs["endDate"])

    def test_custom_range_scopes_search_too(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp, _ = self._getHistoryList(
            dash, db, query="?q=foo&interval=custom&startDate=2026-07-01&endDate=2026-07-05")

        self.assertEqual(resp.status_code, 200)
        kwargs = db.searchEntries.call_args.kwargs
        self.assertIsNotNone(kwargs["startDate"])
        self.assertIsNotNone(kwargs["endDate"])

    def test_named_interval_scopes_the_list(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp, _ = self._getHistoryList(dash, db, query="?interval=week")

        self.assertEqual(resp.status_code, 200)
        # A named interval (Last Week) now scopes the /history list to that range.
        kwargs = db.getEntriesFromNew.call_args.kwargs
        self.assertIsNotNone(kwargs["startDate"])
        self.assertIsNotNone(kwargs["endDate"])
        countKwargs = db.getEntriesCount.call_args.kwargs
        self.assertIsNotNone(countKwargs["startDate"])

    def test_an_unrecognized_interval_renders_as_the_default_not_raw(self):
        """historyPage was the one filter page reading ?interval= raw - the
        data was safe (_getDateRange coerces junk to the default), but the raw
        value reached the template and the pagination links, so a stale or
        truncated URL left the Time Period select with no option selected and
        propagated the junk into every page link."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getHistory(dash, db, query="?interval=bogus")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"<option value=\"all time\" selected>", resp.data)
        self.assertNotIn(b"bogus", resp.data)

    def test_default_visit_is_all_time_and_shows_full_history(self):
        """A plain visit (no query params) defaults to All Time, so the list is
        unscoped (startDate/endDate None) - the full history."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp, _ = self._getHistoryList(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=0, startDate=None, endDate=None, trackIds=None,
                                                     includeSkips=False, fullPlaysOnly=True)

    def test_custom_without_valid_dates_falls_back_and_does_not_scope_the_list(self):
        """interval=custom with no/invalid startDate+endDate falls back to
        'all time' (see dashboard()'s own fallback) - not a scoped range."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp, _ = self._getHistoryList(dash, db, query="?interval=custom")

        self.assertEqual(resp.status_code, 200)
        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=0, startDate=None, endDate=None, trackIds=None,
                                                     includeSkips=False, fullPlaysOnly=True)


class TestHistoryFullPlaysWiring(_ListRouteTestBase):
    """?fullOnly reaches every one of the four queries /history can make, in
    both states. What the filter SELECTS is in tests/test_history_full_plays.py
    against a real repository; this is the wiring, which is where a path gets
    forgotten - the search pair and the non-search pair are entirely separate
    call sites.

    Two booleans, one checkbox: `fullPlaysOnly` is the completion test and
    `includeSkips` is the skip filter, and the route is the only place that
    couples them. They stay separate below the route because the song-detail
    Show Skips toggle drives includeSkips on its own."""

    def _historyCallKwargs(self, query=""):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)
        db.searchEntriesCount.return_value = 5

        self._getHistoryList(dash, db, query=query)
        return db

    def _historyListHtml(self, query=""):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)   #< 3 pages, so there are links to inspect

        _, resultsHtml = self._getHistoryList(dash, db, query=query)
        return resultsHtml

    def assertFilterState(self, mock, *, includeSkips, fullPlaysOnly):
        kwargs = mock.call_args.kwargs
        self.assertEqual(kwargs.get("includeSkips"), includeSkips, kwargs)
        self.assertEqual(kwargs.get("fullPlaysOnly"), fullPlaysOnly, kwargs)

    def test_the_default_asks_for_full_plays_and_no_skips(self):
        db = self._historyCallKwargs()

        self.assertFilterState(db.getEntriesCount, includeSkips=False, fullPlaysOnly=True)
        self.assertFilterState(db.getEntriesFromNew, includeSkips=False, fullPlaysOnly=True)

    def test_the_off_state_drops_both_clauses(self):
        """Unchecking makes /history the raw log: no completion test AND no
        skip filter, so a play that was hidden either way becomes visible."""
        db = self._historyCallKwargs("?fullOnly=0")

        self.assertFilterState(db.getEntriesCount, includeSkips=True, fullPlaysOnly=False)
        self.assertFilterState(db.getEntriesFromNew, includeSkips=True, fullPlaysOnly=False)

    def test_the_oldest_first_query_gets_it_too(self):
        db = self._historyCallKwargs("?sort=oldest&fullOnly=0")

        self.assertFilterState(db.getEntriesFromOld, includeSkips=True, fullPlaysOnly=False)

    def test_the_search_path_gets_it_in_both_states(self):
        onByDefault = self._historyCallKwargs("?q=foo")
        self.assertFilterState(onByDefault.searchEntriesCount, includeSkips=False, fullPlaysOnly=True)
        self.assertFilterState(onByDefault.searchEntries, includeSkips=False, fullPlaysOnly=True)

        optedOut = self._historyCallKwargs("?q=foo&fullOnly=0")
        self.assertFilterState(optedOut.searchEntriesCount, includeSkips=True, fullPlaysOnly=False)
        self.assertFilterState(optedOut.searchEntries, includeSkips=True, fullPlaysOnly=False)

    def test_only_an_explicit_zero_opts_out(self):
        """A tri-state where absence means ON: anything unrecognized has to
        read as the default, not as the opt-out."""
        for query in ("?fullOnly=bogus", "?fullOnly=", "?fullOnly=false", "?fullOnly=00"):
            with self.subTest(query=query):
                db = self._historyCallKwargs(query)

                self.assertFilterState(db.getEntriesFromNew, includeSkips=False, fullPlaysOnly=True)

    def test_pagination_links_carry_the_off_state(self):
        """Page 2 has to keep a filter the user can see is off. _buildPageUrl
        drops only None and "", so the string "0" survives - but only if the
        route hands it over at all."""
        self.assertIn("fullOnly=0", self._historyListHtml("?fullOnly=0"))

    def test_junk_never_reaches_the_pagination_links(self):
        """The links are built from validated values, like every other filter on
        this page - see historyPage's note on coercing fullOnly."""
        self.assertNotIn("bogus", self._historyListHtml("?fullOnly=bogus"))


class TestTopSongsPagination(_ListRouteTestBase):
    """/top-songs must only ask the DB layer for the current page (SQL-level
    LIMIT/OFFSET, mirroring the dashboard's getEntriesCount/getEntriesFromNew
    pattern) when there's no search query - search still needs the full list
    to filter text across name/artist/album."""

    def test_without_search_fetches_only_one_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getTopSongs(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getSongsCount.assert_called_once()
        db.getTopSongs.assert_called_once()
        kwargs = db.getTopSongs.call_args.kwargs
        self.assertEqual(kwargs["limit"], appModule.PAGE_SIZE)
        self.assertEqual(kwargs["offset"], 0)
        self.assertEqual(kwargs["by"], "totalTimeListened")   #< topSongsPage's default sortBy

    def test_without_search_requests_correct_offset_for_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.getSongsCount.return_value = 120

        resp = self._getTopSongs(dash, db, query="?page=2")

        self.assertEqual(resp.status_code, 200)
        kwargs = db.getTopSongs.call_args.kwargs
        self.assertEqual(kwargs["offset"], appModule.PAGE_SIZE)
        self.assertIn(b"Page 2 of 3", resp.data)

    def test_without_search_passes_requested_sort(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getTopSongs(dash, db, query="?sortBy=plays")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.getTopSongs.call_args.kwargs["by"], "plays")

    def test_without_search_handles_empty_database(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getTopSongs(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Page 1 of 1", resp.data)

    def test_with_search_paginates_and_matches_in_sql(self):
        """Search is matched and paginated in SQL (Repository.getSongsPage)
        the same way as the non-search path, not by fetching everything and
        filtering in Python."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.getSongsCount.return_value = 5

        resp = self._getTopSongs(dash, db, query="?q=foo")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.getSongsCount.call_count, 2)
        db.getSongsCount.assert_any_call(None, None, fullPlaysOnly=True, trackIds=None)
        db.getSongsCount.assert_any_call(None, None, searchQuery="foo", trackIds=None, fullPlaysOnly=True,
                                          sortBy=appModule.DEFAULT_SORT_BY)
        kwargs = db.getTopSongs.call_args.kwargs
        self.assertEqual(kwargs["limit"], appModule.PAGE_SIZE)
        self.assertEqual(kwargs["offset"], 0)
        self.assertEqual(kwargs["searchQuery"], "foo")

    def test_totals_come_from_get_play_totals_independent_of_list(self):
        """totalPlays/totalTime must reflect the whole-range aggregate (via the
        cheap getPlayTotals call), not just whatever getTopSongs happens to
        return for the current page."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.getPlayTotals.return_value = (42, 999000)

        resp = self._getTopSongs(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getPlayTotals.assert_called_once()
        self.assertIn('<p class="summary-value">42</p>', resp.get_data(as_text=True))

    def test_totals_are_fetched_in_search_branch_too(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.getPlayTotals.return_value = (7, 1000)

        resp = self._getTopSongs(dash, db, query="?q=foo")

        self.assertEqual(resp.status_code, 200)
        db.getPlayTotals.assert_called_once()
        self.assertIn('<p class="summary-value">7</p>', resp.get_data(as_text=True))

    def test_unknown_sortby_falls_back_to_default_instead_of_500(self):
        """Repository.getSongsPage raises ValueError for a sortBy outside
        SONG_SORT_COLUMNS - an unvalidated query param would otherwise 500."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getTopSongs(dash, db, query="?sortBy=not_a_real_column")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.getTopSongs.call_args.kwargs["by"], appModule.DEFAULT_SORT_BY)

    def test_page_beyond_range_is_clamped_to_last_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.getSongsCount.return_value = 120

        resp = self._getTopSongs(dash, db, query="?page=9999")

        self.assertEqual(resp.status_code, 200)
        kwargs = db.getTopSongs.call_args.kwargs
        self.assertEqual(kwargs["offset"], 2 * appModule.PAGE_SIZE)   #< last page (3) of 120/50
        self.assertIn(b"Page 3 of 3", resp.data)


class TestSkipSortKeepsTheOtherFilters(_ListRouteTestBase):
    """The sort control shares its card with the search box, the tag dropdown
    and the full-plays checkbox. Picking Most Skipped changes the order of the
    page, not which page it is - and the count has to be narrowed the same way
    as the list, or the pager offers pages the list can't fill."""

    def test_the_skip_sort_asks_for_its_own_count(self):
        """Without a search or tag there is nothing else to force the dedicated
        count, so this is the only thing standing between Most Skipped and a
        pager sized from every song ever played - pages 2..N mostly empty."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        self._getTopSongs(dash, db, query="?sortBy=skips")

        counted = [c.kwargs for c in db.getSongsCount.call_args_list]
        self.assertTrue(any(c.get("sortBy") == "skips" for c in counted), counted)

    def test_the_search_reaches_both_the_list_and_the_count(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        self._getTopSongs(dash, db, query="?sortBy=skips&q=foo")

        self.assertEqual(db.getTopSongs.call_args.kwargs["searchQuery"], "foo")
        self.assertEqual(db.getSongsCount.call_args.kwargs["searchQuery"], "foo")
        self.assertEqual(db.getSongsCount.call_args.kwargs["sortBy"], "skips")

    def test_the_full_plays_checkbox_reaches_both(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        self._getTopSongs(dash, db, query="?sortBy=skips&fullOnly=0")

        skipCount = [c.kwargs for c in db.getSongsCount.call_args_list
                     if c.kwargs.get("sortBy") == "skips"]
        self.assertIs(db.getTopSongs.call_args.kwargs["fullPlaysOnly"], False)
        self.assertIs(skipCount[-1]["fullPlaysOnly"], False)

    def test_a_page_that_cannot_rank_by_skips_falls_back(self):
        """/compare and /wrapped read the same ?sortBy= but have no skip-ranked
        path, so the value has to be rejected there rather than quietly
        producing a differently-shaped list."""
        dash = self._makeApp()

        with dash.app.test_request_context("/compare?sortBy=skips"):
            self.assertEqual(dash._getSortByParam(default="plays"), "plays")
        with dash.app.test_request_context("/top-songs?sortBy=skips"):
            self.assertEqual(dash._getSortByParam(allowed=appModule.TOP_LIST_SORT_BY), "skips")

    def test_artists_and_albums_narrow_their_counts_too(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.getAlbumsCount.return_value = 0
        db.getTopAlbums.return_value = []

        self._getTopArtists(dash, db, query="?sortBy=skips&q=foo")
        self._getPath(dash, db, "/top-albums?sortBy=skips&q=foo", headers={"HX-Request": "true"})

        self.assertEqual(db.getArtistsCount.call_args.kwargs["searchQuery"], "foo")
        self.assertEqual(db.getAlbumsCount.call_args.kwargs["searchQuery"], "foo")

    def test_artists_and_albums_ask_for_their_own_counts_too(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.getAlbumsCount.return_value = 0
        db.getTopAlbums.return_value = []

        self._getTopArtists(dash, db, query="?sortBy=skips")
        self._getPath(dash, db, "/top-albums?sortBy=skips", headers={"HX-Request": "true"})

        for mock in (db.getArtistsCount, db.getAlbumsCount):
            calls = [c.kwargs for c in mock.call_args_list]
            self.assertTrue(any(c.get("sortBy") == "skips" for c in calls), calls)


class TestPageParamParsing(_ListRouteTestBase):
    """A non-numeric ?page= must not 500 any list route - it falls back to page 1."""

    def test_dashboard_survives_non_numeric_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp, resultsHtml = self._getHistoryList(dash, db, query="?page=abc")

        self.assertEqual(resp.status_code, 200)
        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=0, startDate=None, endDate=None, trackIds=None,
                                                     includeSkips=False, fullPlaysOnly=True)
        self.assertIn("Page 1 of 3", resultsHtml)

    def test_dashboard_clamps_negative_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp, _ = self._getHistoryList(dash, db, query="?page=-5")

        self.assertEqual(resp.status_code, 200)
        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=0, startDate=None, endDate=None, trackIds=None,
                                                     includeSkips=False, fullPlaysOnly=True)

    def test_top_songs_survives_non_numeric_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getPath(dash, db, "/top-songs?page=abc")

        self.assertEqual(resp.status_code, 200)

    def test_top_artists_survives_non_numeric_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getPath(dash, db, "/top-artists?page=abc")

        self.assertEqual(resp.status_code, 200)


class TestTopArtistsSortAndPageClamp(_ListRouteTestBase):
    """/top-artists is paginated in SQL (getArtistsCount()/getTopArtists()
    LIMIT+OFFSET) the same way as top-songs/top-albums, not via getPage()."""

    def _makeArtistsDb(self, artistCount=0):
        db = self._makeDb(entryCount=0)
        db.getArtistsCount.return_value = artistCount
        return db

    def test_unknown_sortby_falls_back_to_default_instead_of_500(self):
        """Repository.getArtistAggregates raises ValueError for a sortBy
        outside ARTIST_SORT_COLUMNS - an unvalidated query param would
        otherwise turn into a 500."""
        dash = self._makeApp()
        db = self._makeArtistsDb()

        resp = self._getTopArtists(dash, db, query="?sortBy=not_a_real_column")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.getTopArtists.call_args.kwargs["by"], appModule.DEFAULT_SORT_BY)

    def test_page_beyond_range_is_clamped_to_last_page(self):
        dash = self._makeApp()
        db = self._makeArtistsDb(artistCount=120)

        resp = self._getTopArtists(dash, db, query="?page=9999")

        self.assertEqual(resp.status_code, 200)
        kwargs = db.getTopArtists.call_args.kwargs
        self.assertEqual(kwargs["offset"], 2 * appModule.PAGE_SIZE)   #< last page (3) of 120/50
        self.assertIn("Page 3 of 3", resp.get_data(as_text=True))

    def test_search_query_is_passed_through_to_sql(self):
        dash = self._makeApp()
        db = self._makeArtistsDb(artistCount=1)

        resp = self._getTopArtists(dash, db, query="?q=queen")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.getArtistsCount.call_args.kwargs["searchQuery"], "queen")
        self.assertEqual(db.getTopArtists.call_args.kwargs["searchQuery"], "queen")


class TestPaginationExtras(_ListRouteTestBase):
    """Page-number links, 'Showing X-Y of Z', and the jump-to-page input,
    added alongside the existing Prev/Next + 'Page N of M' pagination."""

    def test_page_number_links_are_windowed_with_ellipsis(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=500)   #< 10 pages of PAGE_SIZE=50

        _, body = self._getHistoryList(dash, db, query="?page=5")

        for page in (1, 3, 4, 5, 6, 7, 10):
            self.assertIn(f">{page}<", body)
        self.assertNotIn(">2<", body)   #< skipped, covered by the ellipsis instead
        self.assertNotIn(">9<", body)
        self.assertIn("&hellip;", body)

    def test_current_page_link_is_marked_active(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=500)

        _, resultsHtml = self._getHistoryList(dash, db, query="?page=5")

        self.assertIn('class="pagination-page active"', resultsHtml)

    def test_no_ellipsis_when_all_pages_fit_in_the_window(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)   #< 3 pages, well within the window

        _, body = self._getHistoryList(dash, db, query="?page=2")

        self.assertNotIn("&hellip;", body)
        for page in (1, 2, 3):
            self.assertIn(f">{page}<", body)

    def test_showing_x_of_y_on_first_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        _, resultsHtml = self._getHistoryList(dash, db)

        self.assertIn("Showing 1-50 of 120", resultsHtml)

    def test_showing_x_of_y_on_last_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        _, resultsHtml = self._getHistoryList(dash, db, query="?page=3")

        self.assertIn("Showing 101-120 of 120", resultsHtml)

    def test_showing_x_of_y_with_no_results(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        _, resultsHtml = self._getHistoryList(dash, db)

        self.assertIn("Showing 0-0 of 0", resultsHtml)

    def test_jump_to_page_input_max_matches_total_pages(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        _, resultsHtml = self._getHistoryList(dash, db)

        self.assertIn('max="3"', resultsHtml)


class TestHistoryConnectionEmptyState(_ListRouteTestBase):
    """A brand-new user with zero history and Spotify not authorized sees a
    banner pointing at Profile/Import instead of the generic 'go listen to
    some music' message, which doesn't help someone who hasn't set up
    tracking at all yet. Last.fm is genre-enrichment only - it never
    produces listening history by itself - so it must not count as
    'connected' for this banner."""

    def _makeDb(self, entryCount, hasApi=False, isAuthenticated=False):
        db = super()._makeDb(entryCount)
        credentials = None
        if hasApi:
            credentials = {
                "client_id": "id", "client_secret": "secret",
                "refresh_token": "token" if isAuthenticated else None,
            }
        db.getUserSpotifyCredentials.return_value = credentials
        return db

    def test_shows_connect_banner_when_nothing_connected_and_no_data(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        _, resultsHtml = self._getHistoryList(dash, db)

        self.assertIn("haven't connected Spotify yet", resultsHtml)
        self.assertNotIn("No history tracks found", resultsHtml)

    def test_shows_generic_empty_message_when_spotify_connected_but_no_data(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0, hasApi=True, isAuthenticated=True)

        _, resultsHtml = self._getHistoryList(dash, db)

        self.assertIn("No history tracks found", resultsHtml)
        self.assertNotIn("haven't connected Spotify yet", resultsHtml)

    def test_shows_connect_banner_when_only_lastfm_connected(self):
        """Last.fm alone can't produce any plays, so being connected there
        must not suppress the Spotify-connect banner."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.getUserLastfmApiKey.return_value = "key"

        _, resultsHtml = self._getHistoryList(dash, db)

        self.assertIn("haven't connected Spotify yet", resultsHtml)
        self.assertNotIn("No history tracks found", resultsHtml)

    def test_connect_banner_absent_once_history_exists(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        _, resultsHtml = self._getHistoryList(dash, db)

        self.assertNotIn("haven't connected Spotify yet", resultsHtml)

    def test_connect_banner_does_not_hijack_a_no_match_search(self):
        """Searching for text with zero hits is a normal empty search result,
        not a 'you have no history at all' state - even for a disconnected
        account that does have some imported history."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.searchEntriesCount.return_value = 0

        _, resultsHtml = self._getHistoryList(dash, db, query="?q=nonexistent")

        self.assertIn("No history tracks found", resultsHtml)
        self.assertNotIn("haven't connected Spotify yet", resultsHtml)

    def test_connect_banner_does_not_hijack_an_empty_custom_range(self):
        """A custom date range with no plays just means nothing happened in
        that window, not that the account is disconnected."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        _, resultsHtml = self._getHistoryList(
            dash, db, query="?interval=custom&startDate=2020-01-01&endDate=2020-01-02")

        self.assertIn("No history tracks found", resultsHtml)
        self.assertNotIn("haven't connected Spotify yet", resultsHtml)


class TestHistoryFilterSpacing(unittest.TestCase):
    """On /history the filter form is the last thing in the hero, so
    .filter-section's bottom margin (18px, the page rhythm) would only pad the
    hero's own padding out. The dashboard keeps it - there the summary cards
    follow the form."""

    def setUp(self):
        cssPath = os.path.join(os.path.dirname(__file__), "..", "static", "css", "style.css")
        with open(cssPath, encoding="utf-8") as handle:
            self.css = handle.read()

    def _block(self, selector):
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(match, f"{selector} missing from style.css")
        return match.group(1)

    def test_a_trailing_filter_form_drops_its_bottom_margin(self):
        self.assertIn("margin-bottom: 0", self._block(".hero-content > .filter-section:last-child"))

    def test_the_shared_rule_still_spaces_a_followed_filter_form(self):
        #< 18px: the one vertical rhythm every stacked block shares now
        self.assertIn("margin-bottom: 18px", self._block(".filter-section"))

    def test_history_puts_nothing_after_the_filter_form_in_the_hero(self):
        """The scoped rule above only bites while this stays true."""
        templatePath = os.path.join(os.path.dirname(__file__), "..", "templates", "history.html")
        with open(templatePath, encoding="utf-8") as handle:
            template = handle.read()
        self.assertIn("</form>\n    </div>\n  </section>", template)


if __name__ == "__main__":
    unittest.main()
