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


if __name__ == "__main__":
    unittest.main()
