"""Rank movement on the Top lists: the wiring (routes/charts.py's
topListMovement, templates/_top_list_movement.html, _track_card.html).

The comparison itself is pure and tested in tests/test_rank_movement.py. What
is pinned here is everything around it, and the reason it is a THIRD phase
rather than more work inside the list:

  * the page the user waits for must not pay for it. The Top pages already
    defer the list; the movement request goes off after that lands, so the
    previous period's aggregate - the same cost as the list's own - never sits
    between a filter change and the rows.
  * the answer arrives as out-of-band swaps into placeholders the rows already
    carry. htmx errors loudly on an oob element whose target is missing (see
    templates/_compare_results.html), so the pairing is asserted, not assumed.
  * the previous window is ranked by the SAME query under the SAME filters. A
    comparison against a differently-filtered period is a wrong answer rather
    than a missing one.
"""
import html
import os
import re
import sys
import unittest
from html.parser import HTMLParser
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from config import PAGE_SIZE
from tests._app_factory import AppTestCase
from tests.test_top_lists_htmx import HX_HEADERS, makeTrack

# Two adjacent months, so the window arithmetic is exact rather than relative
# to a clock: [Mar 1, Apr 1) compares against [Jan 30, Mar 1).
_CURRENT = "?interval=custom&startDate=2026-03-01&endDate=2026-03-31"
_MARCH = 1772452800.0       #< 2026-03-02 12:00 UTC, comfortably inside the range
_LATE_MARCH = 1774440000.0  #< 2026-03-25 12:00 UTC
_FEBRUARY = 1770033600.0    #< 2026-02-02 12:00 UTC, inside the window before it
_JANUARY = 1768478400.0     #< 2026-01-15 12:00 UTC, BEFORE it - see the anchoring test

_MOVEMENT_PATH = "/api/top-list-movement"
_PATH_FOR_KIND = {"top_songs": "/top-songs", "top_artists": "/top-artists",
                  "top_albums": "/top-albums"}


def _oobDepths(fragment):
    """How deeply each hx-swap-oob element is nested in `fragment`; 0 is
    top-level, which is the only depth htmx reads by default."""
    class Walker(HTMLParser):
        def __init__(self):
            super().__init__()
            self.depth = 0
            self.depths = []

        def handle_starttag(self, tag, attrs):
            if any(name == "hx-swap-oob" for name, _ in attrs):
                self.depths.append(self.depth)
            self.depth += 1

        def handle_endtag(self, tag):
            self.depth -= 1

    walker = Walker()
    walker.feed(fragment)
    return walker.depths


class MovementTestCase(AppTestCase):
    """One user, two tracks, and a swap in their order between the two months:

        February   t2 (5 plays), t1 (1 play)
        March      t1 (3 plays), t2 (1 play)

    so t1 climbed one place and t2 fell one."""

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
        self.db = self.dash.get_user_db(self.username, self.email)

        self.dash.repo.upsertTrack(makeTrack("t1", "Alpha Song"))
        self.dash.repo.upsertTrack(makeTrack("t2", "Beta Song"))
        self._plays("t1", _MARCH, 3)
        self._plays("t2", _MARCH, 1)
        self._plays("t2", _FEBRUARY, 5)
        self._plays("t1", _FEBRUARY, 1)
        self.dash.repo.commit()

        self.logged_in_patcher = patch.object(self.dash, "is_user_logged_in", return_value=True)
        self.logged_in_patcher.start()
        self.addCleanup(self.logged_in_patcher.stop)

    def _plays(self, trackId, atSecond, count):
        #< distinct played_at per play: (username, track_id, played_at) dedups
        for offset in range(count):
            self.dash.repo.insertPlay(self.username, trackId, atSecond + offset * 600, 200000)

    def _login(self):
        with self.client.session_transaction() as sess:
            sess["email"] = self.email
            sess["username"] = self.username

    def _triggerUrl(self, query=_CURRENT, kind="top_songs"):
        """The movement URL the page itself emits, or None when it asks for
        nothing at all.

        Every test drives this rather than a hand-built URL, because the ids
        the endpoint answers about now come FROM the list - so a URL assembled
        here could ask a question the page never asks, and would keep passing
        after the two stopped agreeing."""
        body = self._list(path=_PATH_FOR_KIND[kind], query=query)
        match = re.search(r'hx-get="([^"]*top-list-movement[^"]*)"', body)
        return html.unescape(match.group(1)) if match else None

    def _movement(self, query=_CURRENT, kind="top_songs"):
        url = self._triggerUrl(query=query, kind=kind)
        if url is None:
            return ""   #< the page asked for nothing, which IS the answer
        self._login()
        return self.client.get(url, headers=HX_HEADERS).get_data(as_text=True)

    def _rawMovement(self, **overrides):
        """A hand-built request, for the shapes a page never emits."""
        params = {"kind": "top_songs", "interval": "custom",
                  "startDate": "2026-03-01", "endDate": "2026-03-31",
                  "ids": ",".join(self._pageIds())}
        params.update(overrides)
        self._login()
        query = "&".join(f"{key}={value}" for key, value in params.items() if value != "")
        return self.client.get(f"{_MOVEMENT_PATH}?{query}", headers=HX_HEADERS)

    def _pageIds(self, query=_CURRENT, path="/top-songs"):
        return re.findall(r'id="rankMove-([^"]+)"', self._list(path=path, query=query))

    def _list(self, path="/top-songs", query=_CURRENT):
        self._login()
        return self.client.get(f"{path}{query}", headers=HX_HEADERS).get_data(as_text=True)

    def _spanFor(self, body, entityId):
        match = re.search(r'<span[^>]*id="rankMove-%s"[^>]*>.*?</span>' % re.escape(entityId), body)
        return match.group(0) if match else None


class TestWhatTheComparisonReports(MovementTestCase):
    def test_an_entry_that_climbed_is_reported_as_up(self):
        span = self._spanFor(self._movement(), "t1")

        self.assertIsNotNone(span, "no movement reported for the climbing entry")
        self.assertIn("rank-move-up", span)
        #< the distance, not just the direction: #2 -> #1
        self.assertIn('title="Up 1 from the previous period"', span)

    def test_an_entry_that_fell_is_reported_as_down(self):
        span = self._spanFor(self._movement(), "t2")

        self.assertIsNotNone(span)
        self.assertIn("rank-move-down", span)

    def test_an_entry_the_previous_month_never_heard_is_new(self):
        self.dash.repo.upsertTrack(makeTrack("t3", "Gamma Song"))
        self._plays("t3", _MARCH, 2)
        self.dash.repo.commit()

        span = self._spanFor(self._movement(), "t3")

        self.assertIsNotNone(span)
        self.assertIn("rank-move-new", span)

    def test_every_oob_element_sits_at_the_top_level_of_the_fragment(self):
        """htmx only looks inside nested elements while allowNestedOobSwaps is
        on, and this page's correctness is not a config setting - the rule
        _compare_results.html already follows.

        Depth, not a tag count: an arrow's own markup nests spans (the glyph is
        aria-hidden, with visually-hidden text beside it), and those are content
        rather than swap targets."""
        depths = _oobDepths(self._movement())

        self.assertTrue(depths, "nothing in the fragment asks to be swapped")
        self.assertEqual(set(depths), {0})

    def test_the_reported_ids_are_exactly_placeholders_the_list_rendered(self):
        """The pairing OOB depends on: htmx errors loudly on an oob element
        whose target is missing, and says nothing at all about a placeholder
        that never gets one."""
        placeholders = set(re.findall(r'id="rankMove-([^"]+)"', self._list()))
        reported = set(re.findall(r'id="rankMove-([^"]+)"', self._movement()))

        self.assertTrue(reported, "the endpoint reported nothing to swap in")
        self.assertTrue(reported <= placeholders,
                        f"nothing on the page to swap into: {reported - placeholders}")


class TestWhenThereIsNothingToCompare(MovementTestCase):
    def test_all_time_reports_nothing(self):
        """There is no period before all of one's history."""
        self.assertEqual(self._movement(query="?interval=").strip(), "")

    def test_a_name_sort_reports_nothing(self):
        """Alphabetical position moves when anything is inserted above it, so
        every row would carry an arrow that says nothing about listening."""
        self.assertEqual(self._movement(query=_CURRENT + "&sortBy=name").strip(), "")

    def test_a_skip_sort_reports_nothing(self):
        """Skip rank runs through a Bayesian prior computed over the window, so
        the prior itself differs between the two - an entry could 'climb'
        without a single play of its own changing."""
        self.assertEqual(self._movement(query=_CURRENT + "&sortBy=skips").strip(), "")

    def test_a_silent_previous_period_reports_nothing(self):
        """Not a page of "new" badges: that says one thing about the period and
        nothing about any entry on it.

        [Mar 20, Mar 31) has a play in it, so the page is NOT empty - what is
        empty is the window before it, [Mar 9, Mar 20)."""
        self._plays("t1", _LATE_MARCH, 2)
        self.dash.repo.commit()
        late = "?interval=custom&startDate=2026-03-20&endDate=2026-03-30"

        self.assertIn('id="rankMove-t1"', self._list(query=late))   #< the row is there
        self.assertEqual(self._movement(query=late).strip(), "")    #< and unjudged

    def test_an_unknown_kind_reports_nothing(self):
        self.assertEqual(self._rawMovement(kind="top_playlists").get_data(as_text=True).strip(), "")

    def test_an_empty_slot_in_the_ids_does_not_shift_the_ranks_below_it(self):
        """The endpoint reads the ids positionally. "t1,,t2" says t2 is third,
        and February had it first - so down 2. Collapsing the gap would make it
        second, and report down 1 about a rank it never held."""
        body = self._rawMovement(ids="t1,,t2").get_data(as_text=True)

        self.assertIn('title="Down 2 from the previous period"', self._spanFor(body, "t2"))

    def test_more_ids_than_a_page_holds_are_not_all_asked_about(self):
        """The cap is what keeps a crafted request from binding one SQL
        parameter per id straight into SQLITE_MAX_VARIABLE_NUMBER, which would
        turn a background badge request into a 500."""
        padding = [f"pad{index:03d}" for index in range(PAGE_SIZE)]
        body = self._rawMovement(ids=",".join(["t1"] + padding + ["t2"])).get_data(as_text=True)

        self.assertIsNotNone(self._spanFor(body, "t1"), "the first id was dropped")
        self.assertIsNone(self._spanFor(body, "t2"), "an id past the cap was answered about")

    def test_a_request_naming_no_entries_reports_nothing(self):
        """The ids ARE the question. Without them there is nothing to badge,
        and re-deriving them would be the ranking query this endpoint exists
        not to repeat."""
        self.assertEqual(self._rawMovement(ids="").get_data(as_text=True).strip(), "")

    def test_a_page_number_nobody_can_be_on_is_refused(self):
        """?page= is the rank this page starts at. Since the ids now come with
        the request it is no longer a SQL offset - so instead of overflowing
        int64 it would quietly render "Down 49,999,899" on an entry that never
        moved. A page beyond any real library is not a page, it is a crafted
        URL, and the answer to it is nothing.

        The digit count is checked before int(), which refuses a string of more
        than 4300 digits outright - a 500 rather than a badge."""
        self.assertEqual(self._rawMovement(page="99999999999999999999")
                         .get_data(as_text=True).strip(), "")
        self.assertEqual(self._rawMovement(page="9" * 5000)
                         .get_data(as_text=True).strip(), "")
        #< short enough to parse, still past any real library
        self.assertEqual(self._rawMovement(page="9999999")
                         .get_data(as_text=True).strip(), "")

    def test_a_page_a_large_library_could_really_be_on_is_honoured(self):
        """The refusal above must not swallow page 200 of someone's history."""
        body = self._rawMovement(page="200").get_data(as_text=True)

        self.assertIn("rank-move", body)

    def test_a_non_decimal_unicode_digit_page_is_refused_not_a_500(self):
        """'²' (superscript two) is str.isdigit()-true but int()-rejected.
        _movementPage used isdigit(), so this reached the unguarded int() and
        500'd; isdecimal() is exactly what int() accepts, so it is refused
        the same way an out-of-range page is (see the test above), not
        crashed."""
        resp = self._rawMovement(page="²")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(as_text=True).strip(), "")


class TestTheFiltersReachBothWindows(MovementTestCase):
    def test_a_search_narrows_the_previous_period_too(self):
        """Unfiltered, t1 climbed one place past t2. With a search that leaves
        only t1, it is #1 in both periods - so a delta here would prove the
        previous window was ranked against a list the page is not showing."""
        span = self._spanFor(self._movement(query=_CURRENT + "&q=Alpha"), "t1")

        self.assertIsNotNone(span)
        self.assertIn("rank-move-same", span)

    def test_a_tag_narrows_the_previous_period_too(self):
        """Same shape as the search test, on the other filter that changes WHICH
        entries are ranked. Tagged alone, t1 leads both periods; unfiltered it
        climbed past t2, so a delta here means the previous window was ranked
        against a list this page is not showing."""
        self.dash.repo.addTag(self.username, "chill", "track", "t1")
        self.dash.repo.commit()

        span = self._spanFor(self._movement(query=_CURRENT + "&tag=chill"), "t1")

        self.assertIsNotNone(span, "the tagged page reported no movement at all")
        self.assertIn("rank-move-same", span)

    def test_full_plays_only_is_applied_to_the_previous_period_too(self):
        """t3's February plays are 1s of a 200s track. With the page's default
        "full plays only" they are not listens, so February never heard it and
        March is new; opt out and February placed it, so it moved instead.

        Both directions asserted, because a filter that reaches neither window
        would also produce one of them."""
        self.dash.repo.upsertTrack(makeTrack("t3", "Gamma Song"))
        for offset in range(4):
            self.dash.repo.insertPlay(self.username, "t3", _MARCH + 50 + offset * 600, 200_000)
            self.dash.repo.insertPlay(self.username, "t3", _FEBRUARY + 50 + offset * 600, 1_000)
        self.dash.repo.commit()

        self.assertIn("rank-move-new", self._spanFor(self._movement(), "t3"))
        self.assertNotIn("rank-move-new",
                         self._spanFor(self._movement(query=_CURRENT + "&fullOnly=0"), "t3"))

    def test_a_truncated_scan_still_tells_new_from_unplaceable(self):
        """The whole point of asking the previous period who played at all.

        With the scan cut to one row, February places only t2. t1 played then
        and cannot be placed, so it gets no badge; t3 did not play then at all,
        and IS new. Before the existence check, absence from a truncated scan
        was indistinguishable and neither could be claimed - which on any range
        past a few months is every entry, since a year of listening runs
        thousands deep."""
        self.dash.repo.upsertTrack(makeTrack("t3", "Gamma Song"))
        self._plays("t3", _MARCH, 2)
        self.dash.repo.commit()

        with patch("routes.charts.PREVIOUS_WINDOW_SCAN_LIMIT", 1):
            body = self._movement()

        self.assertIn("rank-move-new", self._spanFor(body, "t3"))
        self.assertIsNone(self._spanFor(body, "t1"),
                          "an entry that played, below the scan line, was given a badge")

    def test_the_previous_window_reaches_back_exactly_one_span(self):
        """January is outside [Jan 30, Mar 1) and has to stay outside it. These
        20 plays would make t1 the previous period's leader - so t1 reading
        "same" instead of "up" is what a too-wide or wrongly-anchored window
        looks like, and neither shows up in the up/down tests above."""
        self._plays("t1", _JANUARY, 20)
        self.dash.repo.commit()

        self.assertIn("rank-move-up", self._spanFor(self._movement(), "t1"))


class TestItStaysOffThePagesCriticalPath(MovementTestCase):
    def test_the_list_request_runs_one_ranking_query_not_two(self):
        """The whole reason this is a separate endpoint. A second aggregate
        inside the list request would be invisible in every assertion above and
        would double the wait for the rows."""
        with patch.object(self.db, "getTopSongs", wraps=self.db.getTopSongs) as topSongs:
            self._list()

        self.assertEqual(topSongs.call_count, 1)

    def test_the_list_carries_the_trigger_that_asks_for_the_movement(self):
        body = self._list()

        self.assertIn(_MOVEMENT_PATH, body)
        self.assertIn('hx-trigger="load"', body)

    def test_the_trigger_does_not_inherit_the_swap_it_would_break(self):
        """#topListResults sets hx-swap/hx-target/hx-replace-url for everything
        inside it, so an un-overridden trigger would swap a pile of spans over
        the whole list and put the API URL in the address bar."""
        body = self._list()
        trigger = re.search(r"<div[^>]*%s[^>]*>" % re.escape(_MOVEMENT_PATH), body).group(0)

        self.assertIn('hx-swap="none"', trigger)
        self.assertIn('hx-replace-url="false"', trigger)

    def test_the_trigger_owns_its_own_failures_instead_of_the_lists(self):
        """The inherited hx-target is the dangerous one, because nothing about
        it is visible in a successful run.

        htmx resolves a request's target from hx-target even when the swap is
        "none", and puts it on htmx:responseError / htmx:sendError. top-list.js
        registers HtmxFilters.onSwapFailure('topListResults'), which blanks
        whatever target matches that id - so a movement request that 502s mid
        deploy, or dies on a wifi blip, would replace a perfectly good list of
        50 rows with "couldn't load / Retry". onSwapFailure's own comment warns
        about the day a page grows a second region; this is that day."""
        body = self._list()
        trigger = re.search(r"<div[^>]*%s[^>]*>" % re.escape(_MOVEMENT_PATH), body).group(0)

        self.assertIn('hx-target="this"', trigger)

    def test_a_page_with_no_rows_asks_for_nothing(self):
        """Nothing to badge, so the previous-period scan would be a full-range
        aggregate answering an empty page."""
        empty = "?interval=custom&startDate=2026-03-20&endDate=2026-03-30"

        self.assertNotIn(_MOVEMENT_PATH, self._list(query=empty))

    def test_an_id_containing_the_separator_forfeits_its_badge_not_the_page(self):
        """The ids ride the URL comma-separated and positionally, so an id with
        a comma in it would add a phantom slot: an oob span nothing can receive,
        and every rank below it off by one.

        Spotify's own ids are alphanumeric, but an IMPORTED one is whatever the
        export file said - StreamingHistoryImporter takes it from
        spotify_track_uri without validating it - so this is not an invariant
        to rank by. The entry keeps its slot and loses only its own badge."""
        commaId = "we,ird"
        self.dash.repo.upsertTrack(makeTrack(commaId, "Comma Song"))
        self._plays(commaId, _MARCH, 99)   #< tops the March list
        self.dash.repo.commit()

        url = self._triggerUrl()
        body = self._movement()

        self.assertIn("ids=,t1,t2", url.replace("%2C", ","))
        self.assertIsNone(self._spanFor(body, "we"), "a phantom slot was emitted")
        #< t1 is #2 now and was #2 in February: the rank below the blank slot
        #  is still judged correctly
        self.assertIn("rank-move-same", self._spanFor(body, "t1"))

    def test_an_entry_with_no_id_keeps_its_slot_in_the_url(self):
        """The other half of the positional contract, and the half no fixture
        can reach on its own: every ranking query inner-joins its catalog
        table, so an id-less entry cannot occur today. That is precisely why
        the URL must not depend on it - a dropped slot shifts every rank below
        it, and the badges still land on real entries while reporting the
        wrong distance."""
        idless = dict(makeTrack("t9", "Nameless"))
        del idless["id"]
        with patch.object(self.db, "getTopSongs",
                          return_value=[idless, makeTrack("t1", "Alpha Song")]):
            url = self._triggerUrl()

        self.assertIsNotNone(url)
        self.assertIn("ids=,t1", url.replace("%2C", ","))

    def test_no_trigger_when_there_is_nothing_to_ask_for(self):
        """An All Time list would otherwise pay for a request that can only
        answer with an empty body."""
        self.assertNotIn(_MOVEMENT_PATH, self._list(query="?interval="))

    def test_every_top_list_page_gets_the_same_treatment(self):
        for path, kind in (("/top-songs", "top_songs"), ("/top-artists", "top_artists"),
                           ("/top-albums", "top_albums")):
            with self.subTest(path=path):
                self.assertIn(f"kind={kind}", self._list(path=path))


def _trackBy(trackId, name, artistId, albumId):
    """A track pinned to a specific artist and album, so the artist and album
    lists have something of their own to rank."""
    track = makeTrack(trackId, name)
    track["artists"] = [{"id": artistId, "name": f"Artist {artistId}", "imageId": artistId,
                         "url": f"http://example.com/artist/{artistId}", "imageUrl": ""}]
    track["album"] = dict(track["album"], id=albumId, name=f"Album {albumId}",
                          url=f"http://example.com/album/{albumId}", imageId=albumId)
    return track


class TestEveryKindIsWiredEndToEnd(MovementTestCase):
    """_MOVEMENT_KINDS is three getters, three tag lookups and three id kwargs,
    and each kind aggregates differently - songs group by track, albums through
    tracks, artists through track_artists. "It works for songs" therefore says
    nothing about the other two, which is exactly the sibling-call-site shape
    this codebase keeps getting caught by.

    aA/aB (and albums xA/xB) swap places between the two months:

        February   B (7 plays), the base fixture's shared artist (6), A (1)
        March      A (5 plays), the base fixture's shared artist (4), B (1)"""

    def setUp(self):
        super().setUp()
        self.dash.repo.upsertTrack(_trackBy("t3", "Third Song", "aA", "xA"))
        self.dash.repo.upsertTrack(_trackBy("t4", "Fourth Song", "aB", "xB"))
        self._plays("t3", _MARCH, 5)
        self._plays("t4", _MARCH, 1)
        self._plays("t3", _FEBRUARY, 1)
        self._plays("t4", _FEBRUARY, 7)
        self.dash.repo.commit()

    def test_the_artist_list_ranks_artists(self):
        body = self._movement(kind="top_artists")

        self.assertIn('title="Up 2 from the previous period"', self._spanFor(body, "aA"))
        self.assertIn('title="Down 2 from the previous period"', self._spanFor(body, "aB"))

    def test_the_album_list_ranks_albums(self):
        body = self._movement(kind="top_albums")

        self.assertIn('title="Up 2 from the previous period"', self._spanFor(body, "xA"))
        self.assertIn('title="Down 2 from the previous period"', self._spanFor(body, "xB"))

    def test_each_kind_asks_about_its_own_entities_when_deciding_new(self):
        """The existence check takes an entity KIND, and getting it wrong fails
        in the direction that produces a badge rather than none: artist ids
        matched against track ids find nothing, and everything unplaceable
        would read as new.

        With the scan cut to one row, aA and xA played in February and cannot
        be placed - so silence is the only correct answer for them."""
        for kind in ("top_artists", "top_albums"):
            with self.subTest(kind=kind), patch("routes.charts.PREVIOUS_WINDOW_SCAN_LIMIT", 1):
                body = self._movement(kind=kind)

                for entityId in ("aA", "aB") if kind == "top_artists" else ("xA", "xB"):
                    span = self._spanFor(body, entityId)
                    self.assertNotIn("rank-move-new", span or "",
                                     f"{entityId} played in February but was called new")

    def test_each_kind_reports_into_its_own_pages_placeholders(self):
        for path, kind in (("/top-songs", "top_songs"), ("/top-artists", "top_artists"),
                           ("/top-albums", "top_albums")):
            with self.subTest(kind=kind):
                placeholders = set(re.findall(r'id="rankMove-([^"]+)"', self._list(path=path)))
                reported = set(re.findall(r'id="rankMove-([^"]+)"', self._movement(kind=kind)))

                self.assertTrue(reported, f"{kind} reported nothing to swap in")
                self.assertTrue(reported <= placeholders,
                                f"{kind} would swap into nothing: {reported - placeholders}")


class TestPagingComparesTheRightRanks(MovementTestCase):
    """startIndex is what makes page 2 compare #51..#100 rather than #1..#50.
    Without it every page but the first reads as a mass promotion."""

    def setUp(self):
        super().setUp()
        # A full page of tracks played MORE than t1/t2 in both months, so t1
        # and t2 land on page 2 with the same relative order as before.
        for index in range(PAGE_SIZE):
            #< 22 characters, the length of a real Spotify id, so the URL-length
            #  assertion below measures what a real page would send
            trackId = f"trk{index:019d}"
            self.dash.repo.upsertTrack(makeTrack(trackId, f"Filler {index:03d}"))
            self._plays(trackId, _MARCH + 100 + index, 10)
            self._plays(trackId, _FEBRUARY + 100 + index, 10)
        self.dash.repo.commit()

    def test_a_full_pages_trigger_url_stays_within_a_safe_request_line(self):
        """The page's ids ride in the URL so the endpoint does not have to
        re-run the list's own ranking to learn them. That trade has a ceiling:
        raise PAGE_SIZE far enough and the request line grows past what proxies
        and servers accept, and the badges would vanish behind a 414 that
        nothing on the page reports.

        50 ids of 22 characters is about 1.2KB; 2000 is the conservative limit
        every browser and proxy honours."""
        url = self._triggerUrl()

        self.assertIsNotNone(url)
        self.assertEqual(url.count(","), PAGE_SIZE - 1, "not a full page of ids")
        self.assertLess(len(url), 2000, f"the trigger URL is {len(url)} characters")

    def test_the_rank_the_page_shows_is_the_rank_the_badge_is_judged_at(self):
        """The two halves of one number, computed in two places: the card
        renders startIndex + loop.index, and the endpoint judges the entry at
        startIndex + position. Nothing asserted the rendered half - so a
        template that overrode it would put "#7, up 2" on an entry sitting at
        #51, and every test would still pass.

        _track_card.html had exactly such an override, reading an absoluteIndex
        key that nothing has set since 2960f55 moved pagination into SQL. This
        is what its removal leaves behind."""
        body = self._list(query=_CURRENT + "&page=2")

        shown = [int(number) for number in
                 re.findall(r'<div class="track-number">(\d+)', body)]

        self.assertEqual(shown, [PAGE_SIZE + 1, PAGE_SIZE + 2])

    def test_a_second_page_entry_is_ranked_against_its_absolute_position(self):
        span = self._spanFor(self._movement(query=_CURRENT + "&page=2"), "t1")

        self.assertIsNotNone(span, "page 2 reported nothing")
        # t1 sits at #51 now and sat at #52 before, so it moved one place - the
        # DISTANCE is the assertion, because a startIndex of 0 would still call
        # this "up", just up 51 from a rank it never held.
        self.assertIn('title="Up 1 from the previous period"', span)


if __name__ == "__main__":
    unittest.main()
