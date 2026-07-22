import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.utils import msToString


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
