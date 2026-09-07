# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""services/listening_behavior.py - pure bucketing and ratio maths for the
Charts page's Listening Behavior card. No DB, no Flask: Database.database's
getListeningBehavior/getBehavioralCounts are covered separately in
tests/test_database_stats.py (real SQL rows piped through here) and
tests/test_charts_htmx.py / tests/test_charts_genres.py (the route)."""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.listening_behavior import (
    bucketReasonEnd, bucketPlatform, buildListeningBehavior,
    TOP_REASONS, TOP_COUNTRIES, UNKNOWN_LABEL, REASON_END_UNEXPECTED_EXIT_LABEL,
)


def _emptyRaw(total=0):
    return {
        "total": total,
        "shuffle": {"known": 0, "on": 0},
        "offline": {"known": 0, "on": 0},
        "incognito": {"known": 0, "on": 0},
        "reasonEnd": [],
        "platforms": [],
        "countries": [],
    }


class TestBucketReasonEnd(unittest.TestCase):
    def test_null_becomes_unknown(self):
        self.assertEqual(bucketReasonEnd(None), UNKNOWN_LABEL)

    def test_known_code_uses_its_label(self):
        self.assertEqual(bucketReasonEnd("fwdbtn"), "Skipped forward")
        self.assertEqual(bucketReasonEnd("trackdone"), "Track finished")

    def test_unexpected_exit_family_folds_into_one_bucket(self):
        self.assertEqual(bucketReasonEnd("unexpected-exit"), REASON_END_UNEXPECTED_EXIT_LABEL)
        self.assertEqual(bucketReasonEnd("unexpected-exit-while-loading"),
                         REASON_END_UNEXPECTED_EXIT_LABEL)

    def test_unrecognised_code_falls_back_to_title_cased_raw(self):
        self.assertEqual(bucketReasonEnd("some_new_code"), "Some New Code")
        self.assertEqual(bucketReasonEnd("another-new-code"), "Another New Code")


class TestBucketPlatform(unittest.TestCase):
    def test_none_is_other(self):
        self.assertEqual(bucketPlatform(None), "Other")

    def test_empty_string_is_other(self):
        self.assertEqual(bucketPlatform(""), "Other")

    def test_unrecognised_string_is_other(self):
        self.assertEqual(bucketPlatform("some_future_platform_string"), "Other")

    def test_android(self):
        self.assertEqual(bucketPlatform("Android OS 13 API 33 (samsung, SM-G991B)"), "Android")

    def test_ios_by_ios_substring(self):
        self.assertEqual(bucketPlatform("iOS 17.1 (iPhone14,2)"), "iOS")

    def test_ios_by_iphone_substring_without_the_word_ios(self):
        self.assertEqual(bucketPlatform("iPhone14,2"), "iOS")

    def test_ipad(self):
        self.assertEqual(bucketPlatform("iPad13,1"), "iOS")

    def test_windows(self):
        self.assertEqual(bucketPlatform("Windows 10 (10.0.19045; x64)"), "Windows")

    def test_macos_by_osx_substring(self):
        self.assertEqual(bucketPlatform("osx"), "macOS")

    def test_macos_by_macos_substring(self):
        self.assertEqual(bucketPlatform("macOS 14.1"), "macOS")

    def test_linux(self):
        self.assertEqual(bucketPlatform("linux"), "Linux")

    def test_web_player(self):
        self.assertEqual(bucketPlatform("web_player"), "Web player")

    def test_case_insensitive(self):
        self.assertEqual(bucketPlatform("ANDROID OS 13"), "Android")

    def test_precedence_a_compound_string_naming_both_an_os_and_the_player_buckets_to_the_os(self):
        """A real-world-shaped string that carries both hints - the OS checks
        run before the web-player check, so this must not read as "Web
        player"."""
        self.assertEqual(bucketPlatform("Windows 10 (10.0.19045; web_player)"), "Windows")


class TestBuildListeningBehaviorFlags(unittest.TestCase):
    def test_hasData_false_when_nothing_is_known_even_with_plays_in_range(self):
        """total > 0 but every behavioral column NULL (a live-only stretch) -
        distinct from total == 0 (no plays at all): both must show the hint,
        for different reasons, and this is the single flag that covers both."""
        raw = _emptyRaw(total=50)
        result = buildListeningBehavior(raw)
        self.assertFalse(result["hasData"])

    def test_hasData_false_when_there_are_no_plays_at_all(self):
        result = buildListeningBehavior(_emptyRaw(total=0))
        self.assertFalse(result["hasData"])

    def test_hasData_true_when_one_flag_has_known_data(self):
        raw = _emptyRaw(total=10)
        raw["shuffle"] = {"known": 4, "on": 2}
        result = buildListeningBehavior(raw)
        self.assertTrue(result["hasData"])

    def test_hasData_true_from_reasonEnd_alone(self):
        raw = _emptyRaw(total=10)
        raw["reasonEnd"] = [("fwdbtn", 3), (None, 7)]
        self.assertTrue(buildListeningBehavior(raw)["hasData"])

    def test_flag_pct_is_over_known_not_total(self):
        raw = _emptyRaw(total=1030)
        raw["shuffle"] = {"known": 412, "on": 255}   #< the plan doc's own worked example
        result = buildListeningBehavior(raw)
        flag = result["flags"]["shuffle"]
        self.assertEqual(flag["known"], 412)
        self.assertEqual(flag["total"], 1030)
        self.assertEqual(flag["pct"], round(255 / 412 * 100, 1))

    def test_flag_knownPct_is_known_over_total_the_cards_second_number(self):
        """The tile's parenthetical ("...(40% of 1,030)") is known/total, a
        different ratio from `pct` (on/known)."""
        raw = _emptyRaw(total=1030)
        raw["shuffle"] = {"known": 412, "on": 255}
        flag = buildListeningBehavior(raw)["flags"]["shuffle"]
        self.assertEqual(flag["knownPct"], round(412 / 1030 * 100, 1))

    def test_flag_pct_is_none_when_known_is_zero_even_though_other_flags_have_data(self):
        """Per-flag divide-by-zero: one flag can be fully NULL while a
        sibling flag on the very same rows has data (e.g. an older export
        missing `incognito` but carrying `shuffle`)."""
        raw = _emptyRaw(total=100)
        raw["shuffle"] = {"known": 100, "on": 40}
        raw["incognito"] = {"known": 0, "on": 0}
        result = buildListeningBehavior(raw)
        self.assertIsNotNone(result["flags"]["shuffle"]["pct"])
        self.assertIsNone(result["flags"]["incognito"]["pct"])
        #< known == 0 is still a well-defined 0% of total, unlike on/known
        self.assertEqual(result["flags"]["incognito"]["knownPct"], 0.0)

    def test_a_non_dict_input_fails_loudly_instead_of_rendering_no_data(self):
        """A bare MagicMock() (a route test that forgot to stub
        getListeningBehavior) or any other non-dict is a caller bug. It must
        raise rather than quietly degrade to the no-data hint - that hint
        would mask a facade returning the wrong shape in production. The three
        bare-MagicMock charts test bases stub the method explicitly instead."""
        with self.assertRaises(TypeError):
            buildListeningBehavior(MagicMock())

    def test_every_flag_name_is_present_even_when_raw_omits_it(self):
        """A route stub or a genuinely old row shape might not carry every
        key - buildListeningBehavior must not KeyError."""
        result = buildListeningBehavior({"total": 0})
        for name in ("shuffle", "offline", "incognito"):
            with self.subTest(flag=name):
                self.assertEqual(result["flags"][name],
                                 {"on": 0, "known": 0, "total": 0, "pct": None, "knownPct": None})


class TestBuildListeningBehaviorBuckets(unittest.TestCase):
    def test_reasonEnd_rows_are_bucketed_and_summed(self):
        raw = _emptyRaw(total=10)
        raw["reasonEnd"] = [("unexpected-exit", 2), ("unexpected-exit-while-loading", 3), ("fwdbtn", 1)]
        result = buildListeningBehavior(raw)
        self.assertIn((REASON_END_UNEXPECTED_EXIT_LABEL, 5), result["reasonEnd"])
        self.assertIn(("Skipped forward", 1), result["reasonEnd"])

    def test_null_reasonEnd_row_becomes_the_unknown_bucket(self):
        raw = _emptyRaw(total=10)
        raw["reasonEnd"] = [("fwdbtn", 4), (None, 6)]
        result = buildListeningBehavior(raw)
        self.assertIn((UNKNOWN_LABEL, 6), result["reasonEnd"])

    def test_unknownReasonShare_is_over_the_reasonEnd_total_not_the_plays_total(self):
        raw = _emptyRaw(total=1000)   #< most plays are live and never reach reasonEnd at all
        raw["reasonEnd"] = [("fwdbtn", 3), (None, 1)]
        result = buildListeningBehavior(raw)
        self.assertEqual(result["unknownReasonShare"], round(1 / 4 * 100, 1))

    def test_unknownReasonShare_is_none_when_there_are_no_reasonEnd_rows_at_all(self):
        result = buildListeningBehavior(_emptyRaw(total=0))
        self.assertIsNone(result["unknownReasonShare"])

    def test_reasonEnd_top_n_keeps_the_highest_counts_not_just_n_of_them(self):
        """A truncation bug that kept the FIRST N rows (SQL/dict order) rather
        than the highest N would pass a bare len()==N assertion - this checks
        which ones survived."""
        raw = _emptyRaw(total=100)
        # TOP_REASONS + 2 distinct buckets, ranked 1..TOP_REASONS+2 by count,
        # in an order that is NOT already sorted.
        pairs = [(f"reason_{i}", i) for i in range(TOP_REASONS + 2)]
        pairs.reverse()   #< smallest count first, so a "keep the first N" bug is caught
        raw["reasonEnd"] = pairs
        result = buildListeningBehavior(raw)
        self.assertEqual(len(result["reasonEnd"]), TOP_REASONS)
        keptCounts = {count for _, count in result["reasonEnd"]}
        highest = set(range(2, TOP_REASONS + 2))   #< the TOP_REASONS highest of 0..TOP_REASONS+1
        self.assertEqual(keptCounts, highest)

    def test_countries_top_n_keeps_the_highest_counts(self):
        raw = _emptyRaw(total=100)
        pairs = [(f"C{i}", i) for i in range(TOP_COUNTRIES + 2)]
        pairs.reverse()
        raw["countries"] = pairs
        result = buildListeningBehavior(raw)
        self.assertEqual(len(result["countries"]), TOP_COUNTRIES)
        keptCounts = {count for _, count in result["countries"]}
        highest = set(range(2, TOP_COUNTRIES + 2))
        self.assertEqual(keptCounts, highest)

    def test_null_country_becomes_the_unknown_bucket(self):
        raw = _emptyRaw(total=10)
        raw["countries"] = [("US", 4), (None, 6)]
        result = buildListeningBehavior(raw)
        self.assertIn((UNKNOWN_LABEL, 6), result["countries"])

    def test_platforms_are_bucketed_and_summed_across_raw_variants(self):
        raw = _emptyRaw(total=10)
        raw["platforms"] = [("Android OS 13", 3), ("android os 12 (pixel)", 2), (None, 1)]
        result = buildListeningBehavior(raw)
        self.assertIn(("Android", 5), result["platforms"])
        self.assertIn(("Other", 1), result["platforms"])

    def test_platforms_are_not_truncated_to_a_top_n(self):
        """Only ~7 platform buckets ever exist, so there is no TOP_N constant
        for this list - it should carry every bucket present."""
        raw = _emptyRaw(total=10)
        raw["platforms"] = [("android", 1), ("ios", 1), ("windows", 1), ("osx", 1),
                            ("linux", 1), ("web_player", 1), ("unknown_thing", 1)]
        result = buildListeningBehavior(raw)
        self.assertEqual(len(result["platforms"]), 7)


if __name__ == "__main__":
    unittest.main()
