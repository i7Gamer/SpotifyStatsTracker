"""Rank movement on the Top lists (services/rank_movement.py).

Pure comparison logic, tested against plain id lists - no DB - mirroring how
services/listening_calendar.py is unit-tested. The wiring that supplies those
lists (and keeps them off the page's own critical path) is covered in
tests/test_top_list_movement.py."""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.rank_movement import (
    DOWN, NEW, PREVIOUS_WINDOW_SCAN_LIMIT, SAME, UP,
    previousWindow, rankMovements,
)

_MARCH = datetime.datetime(2026, 3, 1)
_APRIL = datetime.datetime(2026, 4, 1)


class TestPreviousWindow(unittest.TestCase):
    def test_it_is_the_equal_span_immediately_before(self):
        self.assertEqual(previousWindow(_MARCH, _APRIL),
                         (datetime.datetime(2026, 1, 29), _MARCH))

    def test_the_two_windows_touch_but_do_not_overlap(self):
        """[start, end) both times, so the play at the boundary is counted
        once - by the current window, the same way the page counts it."""
        previousStart, previousEnd = previousWindow(_MARCH, _APRIL)

        self.assertEqual(previousEnd, _MARCH)
        self.assertEqual(_MARCH - previousStart, _APRIL - _MARCH)

    def test_all_time_has_nothing_to_compare_against(self):
        """An unbounded start is All Time. A window "as long as your whole
        history" would sit in an empty prehistory and call everything new."""
        self.assertIsNone(previousWindow(None, _APRIL))

    def test_an_unbounded_end_is_refused_too(self):
        self.assertIsNone(previousWindow(_MARCH, None))

    def test_an_empty_or_inverted_range_is_refused(self):
        self.assertIsNone(previousWindow(_APRIL, _APRIL))
        self.assertIsNone(previousWindow(_APRIL, _MARCH))


class TestRankMovements(unittest.TestCase):
    def test_an_entry_that_climbed_reports_how_far(self):
        moves = rankMovements(["a"], ["x", "y", "z", "a"])

        self.assertEqual(moves["a"], {"direction": UP, "amount": 3})

    def test_an_entry_that_fell_reports_how_far(self):
        moves = rankMovements(["x", "y", "a"], ["a", "x", "y"])

        self.assertEqual(moves["a"], {"direction": DOWN, "amount": 2})

    def test_an_entry_that_held_its_place_says_so(self):
        """Not silence: "we checked and it did not move" is an answer, and the
        caller distinguishes the two by whether an id is reported at all."""
        moves = rankMovements(["a", "b"], ["a", "b"])

        self.assertEqual(moves["a"], {"direction": SAME, "amount": 0})
        self.assertEqual(moves["b"], {"direction": SAME, "amount": 0})

    def test_ranks_are_absolute_so_page_two_compares_page_two(self):
        """startIndex is the rank above this page. Without it every page would
        compare against 1..20 and page 3 would read as a mass promotion."""
        moves = rankMovements(["a"], ["x"] * 40 + ["a"], startIndex=40, previousIsComplete=True)

        #< was #41, still #41
        self.assertEqual(moves["a"], {"direction": SAME, "amount": 0})

    def test_an_entry_the_previous_period_never_saw_is_new(self):
        moves = rankMovements(["a"], ["x", "y"], previousIsComplete=True)

        self.assertEqual(moves["a"], {"direction": NEW, "amount": 0})

    def test_new_is_only_claimed_when_the_previous_list_is_the_whole_period(self):
        """A list that hit the scan limit was truncated, so "not in it" means
        "we did not look far enough" - which is not something to tell a user."""
        moves = rankMovements(["a"], ["x", "y"], previousIsComplete=False)

        self.assertNotIn("a", moves)

    def test_a_previous_period_with_no_plays_reports_nothing_at_all(self):
        """Twenty "new" badges say one thing about the period and nothing about
        any entry on the page."""
        self.assertEqual(rankMovements(["a", "b"], [], previousIsComplete=True), {})

    def test_entries_are_judged_independently(self):
        moves = rankMovements(["a", "b", "c"], ["b", "a", "c"], previousIsComplete=True)

        self.assertEqual(moves["a"], {"direction": UP, "amount": 1})     #< #2 -> #1
        self.assertEqual(moves["b"], {"direction": DOWN, "amount": 1})   #< #1 -> #2
        self.assertEqual(moves["c"], {"direction": SAME, "amount": 0})   #< #3 -> #3

    def test_nothing_on_the_page_is_nothing_to_report(self):
        self.assertEqual(rankMovements([], ["a"], previousIsComplete=True), {})

    def test_the_scan_limit_is_deep_enough_to_be_worth_having(self):
        """It is the difference between "it wasn't there" and "we didn't look",
        and a page of 20 sitting inside a scan of 20 would never prove either."""
        self.assertGreaterEqual(PREVIOUS_WINDOW_SCAN_LIMIT, 100)


if __name__ == "__main__":
    unittest.main()
