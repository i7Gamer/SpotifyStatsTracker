import datetime
import unittest
from unittest.mock import MagicMock, patch
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.utils"), MagicMock):
    del sys.modules["Database.utils"]

import Database.utils as utilsModule


class TestStartOfWeek(unittest.TestCase):
    """startOfWeek must return Monday 00:00 local time for the week containing the
    given datetime (or now() if omitted), mirroring startOfDay's contract."""

    def setUp(self):
        patcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_wednesday_rolls_back_to_monday(self):
        wednesday = datetime.datetime(2026, 7, 8, 15, 30, tzinfo=datetime.timezone.utc)  # Wednesday
        result = utilsModule.startOfWeek(wednesday)
        self.assertEqual(result, datetime.datetime(2026, 7, 6, 0, 0, tzinfo=datetime.timezone.utc))

    def test_monday_stays_on_monday_at_midnight(self):
        monday = datetime.datetime(2026, 7, 6, 23, 59, tzinfo=datetime.timezone.utc)
        result = utilsModule.startOfWeek(monday)
        self.assertEqual(result, datetime.datetime(2026, 7, 6, 0, 0, tzinfo=datetime.timezone.utc))

    def test_sunday_rolls_back_to_previous_monday(self):
        sunday = datetime.datetime(2026, 7, 12, 8, 0, tzinfo=datetime.timezone.utc)
        result = utilsModule.startOfWeek(sunday)
        self.assertEqual(result, datetime.datetime(2026, 7, 6, 0, 0, tzinfo=datetime.timezone.utc))

    def test_naive_datetime_is_localized(self):
        naive = datetime.datetime(2026, 7, 8, 12, 0)
        result = utilsModule.startOfWeek(naive)
        self.assertEqual(result.tzinfo, datetime.timezone.utc)
        self.assertEqual(result, datetime.datetime(2026, 7, 6, 0, 0, tzinfo=datetime.timezone.utc))

    def test_defaults_to_now(self):
        fixedNow = datetime.datetime(2026, 7, 9, 12, 0, tzinfo=datetime.timezone.utc)
        with patch.object(utilsModule, "now", return_value=fixedNow):
            result = utilsModule.startOfWeek()
        self.assertEqual(result, datetime.datetime(2026, 7, 6, 0, 0, tzinfo=datetime.timezone.utc))


if __name__ == "__main__":
    unittest.main()
