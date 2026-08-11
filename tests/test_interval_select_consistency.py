# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The Time Period select offers the same options in the same order everywhere.

It is spelled out in eight templates rather than shared, and the copies had
drifted into two orders: "All Time" led on /, /history and the Top pages, but
sat second-to-last (after Last 5 Years, before Custom) on /charts, /genres and
/profile. Same eight values either way, so nothing was broken - it just moved
under you between pages. Worst on /profile, which is where the DEFAULT window
is chosen: the list you pick from was ordered the opposite way to the pages the
choice applies to.

Found in the browser by reading the rendered options off each page. Pinned here
rather than by extracting a partial: the selects differ in id, onchange handler
and surrounding markup, and this asserts the one thing they must agree on.
"""
import glob
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, "templates")

#< widest first, then ascending by length, Custom last - the order /, /history
#  and the Top pages already used, and the one a reader meets first
CANONICAL_ORDER = ["all time", "today", "day", "week", "month", "year", "5years", "custom"]

_SELECT_NAMES = ("interval", "default_dashboard_window", "default_top_list_window")


def _selectsIn(source):
    """[(name, [option values])] for every period select in one template."""
    out = []
    for match in re.finditer(r"<select\b[^>]*>", source):
        tag = match.group(0)
        nameMatch = re.search(r'name="([^"]+)"', tag)
        if not nameMatch or nameMatch.group(1) not in _SELECT_NAMES:
            continue
        end = source.index("</select>", match.end())
        body = source[match.end():end]
        out.append((nameMatch.group(1), re.findall(r'<option value="([^"]*)"', body)))
    return out


def _periodSelects():
    for path in sorted(glob.glob(os.path.join(_TEMPLATES, "**", "*.html"), recursive=True)):
        with open(path, encoding="utf-8") as fh:
            for name, values in _selectsIn(fh.read()):
                yield os.path.basename(path), name, values


def _orderOf(values):
    """The option order, with compare.html's blank read as its all-time entry.

    Compare spells All Time as "" on purpose (see the test below); that is a
    difference in the VALUE, not in where the option sits, and this file is
    about the sequence."""
    return ["all time" if v == "" else v for v in values]


class IntervalSelectConsistencyTestCase(unittest.TestCase):
    def test_there_are_period_selects_to_check(self):
        """The scan is regex-based, so an empty result would pass every
        assertion below without testing anything."""
        self.assertGreaterEqual(len(list(_periodSelects())), 6)

    def test_every_period_select_lists_its_options_in_the_same_order(self):
        """Relative order, not an identical list: /profile's two selects pick a
        DEFAULT window, and a default cannot be "Custom Date Range", so they
        legitimately offer seven of the eight. What must not differ is the
        sequence."""
        for template, name, values in _periodSelects():
            with self.subTest(template=template, select=name):
                order = _orderOf(values)
                self.assertEqual(order, [v for v in CANONICAL_ORDER if v in order])

    def test_only_the_default_window_selects_may_drop_an_option(self):
        """So "same relative order" above cannot be satisfied by a select that
        quietly lost a period."""
        for template, name, values in _periodSelects():
            with self.subTest(template=template, select=name):
                expected = 7 if name.startswith("default_") else 8
                self.assertEqual(len(values), expected)

    def test_all_time_leads_every_list(self):
        """The specific drift this was written for, called out on its own so a
        failure says which half moved."""
        for template, name, values in _periodSelects():
            with self.subTest(template=template, select=name):
                self.assertEqual(_orderOf(values)[0], "all time")

    def test_compare_is_the_one_page_that_spells_all_time_as_blank(self):
        """Deliberate, and it took three coordinated pieces to make it work -
        recorded here so the next reader does not "fix" it as this one nearly
        did.

        comparePage calls _getDateRange with default="all time", so a blank
        interval resolves to all-time THERE (everywhere else the default is the
        user's window, and blank means that instead). The route normalizes the
        stored "all time" setting to "" so the option matches, and compare.js
        deliberately keeps `interval` out of its prune list because an ABSENT
        interval would fall back to the saved default. Change any one of those
        and this convention breaks silently."""
        blanks = {t for t, _, values in _periodSelects() if "" in values}

        self.assertEqual(blanks, {"compare.html"})


if __name__ == "__main__":
    unittest.main()
