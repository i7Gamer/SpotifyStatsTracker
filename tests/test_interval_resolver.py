# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""DateRangeMixin._resolveIntervalParam: the shared ?interval= resolver five
route call sites used to spell out by hand (CORE-8, 2026-09-02 review). Two
defaults - `absentDefault` for a missing param, `emptyDefault` for a present
but empty one - because the Top Songs/Artists/Albums pages need them to
differ (an absent param takes the account's own default_top_list_window, an
empty one has always meant All Time - see tests/test_top_list_default_window.py)
while dashboardIndex/historyPage/chartsPage and routes/genres.py's genresPage
all pass the same value for both, collapsing the distinction away.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import makeApp


class TestResolveIntervalParam(unittest.TestCase):
    def _resolve(self, dash, query, absentDefault, emptyDefault, customStart=None, customEnd=None):
        with dash.app.test_request_context(f"/{query}"):
            return dash._resolveIntervalParam(absentDefault, emptyDefault, customStart, customEnd)

    def test_an_absent_param_takes_the_absent_default(self):
        dash = makeApp()
        self.assertEqual(self._resolve(dash, "", "week", "all time"), "week")

    def test_a_present_empty_param_takes_the_empty_default(self):
        dash = makeApp()
        self.assertEqual(self._resolve(dash, "?interval=", "week", "all time"), "all time")

    def test_equal_defaults_collapse_absent_and_empty_to_one_value(self):
        """The shape every non-Top page uses: passing the same value for both
        arguments makes the absent/empty distinction invisible."""
        dash = makeApp()
        self.assertEqual(self._resolve(dash, "", "day", "day"), "day")
        self.assertEqual(self._resolve(dash, "?interval=", "day", "day"), "day")

    def test_a_recognized_value_wins_over_either_default(self):
        dash = makeApp()
        self.assertEqual(self._resolve(dash, "?interval=year", "week", "all time"), "year")

    def test_junk_falls_back_to_the_absent_default_not_the_empty_one(self):
        """A hand-edited or stale URL must not get a WIDER view than the
        account configured - see test_top_list_default_window.py's identical
        rule at the route level."""
        dash = makeApp()
        self.assertEqual(self._resolve(dash, "?interval=bogus", "week", "all time"), "week")

    def test_custom_with_both_dates_is_honoured(self):
        dash = makeApp()
        self.assertEqual(
            self._resolve(dash, "?interval=custom", "week", "all time",
                          customStart="2020-01-01", customEnd="2020-01-02"),
            "custom")

    def test_custom_missing_a_date_falls_back_to_the_empty_default(self):
        """An incomplete custom range reads as "nothing was actually
        specified" - the same bucket a present-but-empty ?interval= falls
        into - not as the account's usual (absent-default) window."""
        dash = makeApp()
        self.assertEqual(
            self._resolve(dash, "?interval=custom", "week", "all time", customStart="2020-01-01"),
            "all time")
        self.assertEqual(
            self._resolve(dash, "?interval=custom", "week", "all time"),
            "all time")

    def test_the_result_is_never_the_empty_string(self):
        """Every caller drops falsy query values from the links it builds
        (PaginationMixin._buildPageUrl, _topListShell's listArgs) - an ""
        surviving out of here would vanish from those links and the request
        that followed would re-resolve the absent default, disagreeing with
        what the filter card had just shown as selected."""
        dash = makeApp()
        for query in ("", "?interval="):
            with self.subTest(query=query):
                self.assertNotEqual(self._resolve(dash, query, "week", "all time"), "")


if __name__ == "__main__":
    unittest.main()
