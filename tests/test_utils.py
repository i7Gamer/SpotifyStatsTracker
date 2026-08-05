import sys
import os
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.utils import msToString, flaskDebugEnabled


class TestFlaskDebugEnabled(unittest.TestCase):
    """The verbose-diagnostics gate, shared by six modules.

    It used to be six separate spellings - two identical private copies, four
    inline `os.environ.get(...) in TRUTHY_DEBUG_VALUES` reads, and one bare
    `if os.environ.get("FLASK_DEBUG")` in Database/database.py that tested the
    STRING for truthiness rather than for a truthy value. That last one is what
    these tests are really for: "0" is a non-empty string, so setting
    FLASK_DEBUG=0 to turn diagnostics off switched that one log on.

    Read per call, not cached at import - every caller's tests drive it with
    patch.dict around the code under test."""

    def _enabled(self, value):
        with patch.dict(os.environ, {"FLASK_DEBUG": value}):
            return flaskDebugEnabled()

    def test_the_on_values(self):
        self.assertTrue(self._enabled("1"))
        self.assertTrue(self._enabled("true"))

    def test_case_does_not_matter(self):
        self.assertTrue(self._enabled("TRUE"))
        self.assertTrue(self._enabled("True"))

    def test_zero_is_off_even_though_it_is_a_non_empty_string(self):
        """The bug the shared helper removes: a bare truthiness test on the
        env var read "0" as on."""
        self.assertFalse(self._enabled("0"))

    def test_other_falsy_spellings_are_off(self):
        for value in ("false", "no", "off", "", "  "):
            with self.subTest(value=value):
                self.assertFalse(self._enabled(value))

    def test_unset_is_off(self):
        env = {k: v for k, v in os.environ.items() if k != "FLASK_DEBUG"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(flaskDebugEnabled())

    def test_it_is_read_per_call_rather_than_frozen_at_import(self):
        """Caching it would silently ignore every caller's patch.dict."""
        self.assertTrue(self._enabled("1"))
        self.assertFalse(self._enabled("0"))
        self.assertTrue(self._enabled("1"))


class TestMsToString(unittest.TestCase):
    def test_zero_renders_as_0s_not_0ms(self):
        self.assertEqual(msToString(0), "0s")

    def test_none_renders_as_0s(self):
        self.assertEqual(msToString(None), "0s")

    def test_negative_renders_as_0s(self):
        self.assertEqual(msToString(-5), "0s")

    def test_seconds_only(self):
        self.assertEqual(msToString(5000), "5s")

    def test_minutes_and_seconds(self):
        self.assertEqual(msToString(65000), "1m 5s")

    def test_hours_minutes_seconds(self):
        self.assertEqual(msToString(3725000), "1h 2m 5s")

    def test_hide_seconds_above_threshold_drops_seconds(self):
        # 12h 3m 41s with a 10h threshold -> seconds dropped.
        twelveHours = (12 * 3600 + 3 * 60 + 41) * 1000
        self.assertEqual(msToString(twelveHours, hideSecondsAboveHours=10), "12h 3m")

    def test_hide_seconds_below_threshold_keeps_seconds(self):
        # 9h 59m 59s is under the 10h threshold -> seconds kept.
        under = (9 * 3600 + 59 * 60 + 59) * 1000
        self.assertEqual(msToString(under, hideSecondsAboveHours=10), "9h 59m 59s")

    def test_hide_seconds_at_exact_threshold_drops_seconds(self):
        # Exactly 10h counts as "at least 10h" -> seconds dropped.
        ten = (10 * 3600 + 5) * 1000
        self.assertEqual(msToString(ten, hideSecondsAboveHours=10), "10h 0m")

    def test_threshold_none_is_unchanged_behavior(self):
        self.assertEqual(msToString(3725000, hideSecondsAboveHours=None), "1h 2m 5s")


if __name__ == "__main__":
    unittest.main()
