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

    def _movement(self, query=_CURRENT, kind="top_songs"):
        self._login()
        separator = "&" if query else "?"
        return self.client.get(f"{_MOVEMENT_PATH}{query}{separator}kind={kind}",
                               headers=HX_HEADERS).get_data(as_text=True)

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
        self.assertEqual(self._movement(kind="top_playlists").strip(), "")


class TestTheFiltersReachBothWindows(MovementTestCase):
    def test_a_search_narrows_the_previous_period_too(self):
        """Unfiltered, t1 climbed one place past t2. With a search that leaves
        only t1, it is #1 in both periods - so a delta here would prove the
        previous window was ranked against a list the page is not showing."""
        span = self._spanFor(self._movement(query=_CURRENT + "&q=Alpha"), "t1")

        self.assertIsNotNone(span)
        self.assertIn("rank-move-same", span)

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

    def test_no_trigger_when_there_is_nothing_to_ask_for(self):
        """An All Time list would otherwise pay for a request that can only
        answer with an empty body."""
        self.assertNotIn(_MOVEMENT_PATH, self._list(query="?interval="))

    def test_every_top_list_page_gets_the_same_treatment(self):
        for path, kind in (("/top-songs", "top_songs"), ("/top-artists", "top_artists"),
                           ("/top-albums", "top_albums")):
            with self.subTest(path=path):
                self.assertIn(f"kind={kind}", self._list(path=path))


class TestPagingComparesTheRightRanks(MovementTestCase):
    """startIndex is what makes page 2 compare #51..#100 rather than #1..#50.
    Without it every page but the first reads as a mass promotion."""

    def setUp(self):
        super().setUp()
        # A full page of tracks played MORE than t1/t2 in both months, so t1
        # and t2 land on page 2 with the same relative order as before.
        for index in range(PAGE_SIZE):
            trackId = f"filler{index:03d}"
            self.dash.repo.upsertTrack(makeTrack(trackId, f"Filler {index:03d}"))
            self._plays(trackId, _MARCH + 100 + index, 10)
            self._plays(trackId, _FEBRUARY + 100 + index, 10)
        self.dash.repo.commit()

    def test_a_second_page_entry_is_ranked_against_its_absolute_position(self):
        span = self._spanFor(self._movement(query=_CURRENT + "&page=2"), "t1")

        self.assertIsNotNone(span, "page 2 reported nothing")
        # t1 sits at #51 now and sat at #52 before, so it moved one place - the
        # DISTANCE is the assertion, because a startIndex of 0 would still call
        # this "up", just up 51 from a rank it never held.
        self.assertIn('title="Up 1 from the previous period"', span)


if __name__ == "__main__":
    unittest.main()
