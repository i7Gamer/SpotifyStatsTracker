"""_getChangeText() turns a (currentValue, previousValue) pair into the
dashboard's "X% more/less than the previous period" label. A truly unchanged
value used to round to "0.0% less than the previous period" in red (the
change > 0 branch is False at exactly zero, so it fell into the "less"/
change-negative case) even though nothing changed.
"""
import unittest
from unittest.mock import patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp
from _app_factory import makeApp as _makeApp

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'



class TestGetChangeText(unittest.TestCase):
    def test_identical_nonzero_values_report_no_change_not_less(self):
        dash = _makeApp()
        text, cssClass = dash._getChangeText(100, 100)
        self.assertEqual(text, "No change from the previous period")
        self.assertEqual(cssClass, "")

    def test_a_change_that_rounds_to_zero_percent_also_reports_no_change(self):
        """A tiny relative change (e.g. 1000 -> 1000.04) rounds to 0.0% and must
        not be reported as "less than the previous period" in red."""
        dash = _makeApp()
        text, cssClass = dash._getChangeText(1000.04, 1000)
        self.assertEqual(text, "No change from the previous period")
        self.assertEqual(cssClass, "")

    def test_increase_is_reported_positive(self):
        dash = _makeApp()
        text, cssClass = dash._getChangeText(150, 100)
        self.assertIn("more", text)
        self.assertEqual(cssClass, "change-positive")

    def test_decrease_is_reported_negative(self):
        dash = _makeApp()
        text, cssClass = dash._getChangeText(50, 100)
        self.assertIn("less", text)
        self.assertEqual(cssClass, "change-negative")

    def test_zero_previous_and_zero_current_reports_nothing(self):
        dash = _makeApp()
        text, cssClass = dash._getChangeText(0, 0)
        self.assertIsNone(text)
        self.assertEqual(cssClass, "")

    def test_zero_previous_with_nonzero_current_is_new(self):
        dash = _makeApp()
        text, cssClass = dash._getChangeText(5, 0)
        self.assertEqual(text, "New this period")
        self.assertEqual(cssClass, "change-positive")


if __name__ == "__main__":
    unittest.main()
