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


class TestTimeToIntUTC(unittest.TestCase):
    """timeToIntUTC must treat a naive (no offset marker) date/time string as
    UTC, unlike timeToInt which localizes it to the app's configured TZ -
    Spotify's Account-export "endTime" field is documented as UTC but carries
    no timezone marker on the wire."""

    def test_naive_string_is_interpreted_as_utc_not_local_tz(self):
        with patch.object(utilsModule, "tz", datetime.timezone(datetime.timedelta(hours=-8))):  #< e.g. America/Los_Angeles
            result = utilsModule.timeToIntUTC("2023-07-08 12:00:00")
        expected = int(datetime.datetime(2023, 7, 8, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual(result, expected)

    def test_differs_from_timeToInt_when_local_tz_is_not_utc(self):
        with patch.object(utilsModule, "tz", datetime.timezone(datetime.timedelta(hours=-8))):
            utcResult = utilsModule.timeToIntUTC("2023-07-08 12:00:00")
            localResult = utilsModule.timeToInt("2023-07-08 12:00:00")
        self.assertNotEqual(utcResult, localResult)
        # "12:00:00" read as UTC-8 local time is a later UTC instant (further
        # from the UTC-8 zone's earlier clock) than the same wall-clock string
        # read directly as UTC.
        self.assertEqual(localResult - utcResult, 8 * 3600)

    def test_string_with_explicit_offset_is_respected_not_overridden(self):
        with patch.object(utilsModule, "tz", datetime.timezone(datetime.timedelta(hours=-8))):
            result = utilsModule.timeToIntUTC("2023-07-08T12:00:00+02:00")
        expected = int(datetime.datetime(2023, 7, 8, 10, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual(result, expected)

    def test_z_suffix_is_treated_as_utc(self):
        result = utilsModule.timeToIntUTC("2023-07-08T12:00:00Z")
        expected = int(datetime.datetime(2023, 7, 8, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual(result, expected)

    def test_falls_back_to_timeToInt_for_unparseable_input(self):
        self.assertEqual(utilsModule.timeToIntUTC("not-a-date"), utilsModule.timeToInt("not-a-date"))

    def test_falls_back_to_timeToInt_for_numeric_timestamp(self):
        self.assertEqual(utilsModule.timeToIntUTC(1234567890), utilsModule.timeToInt(1234567890))


if __name__ == "__main__":
    unittest.main()
