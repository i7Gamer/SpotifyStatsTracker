import contextlib
import datetime
import time
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


class TestSystemLocalTimezoneTracksDst(unittest.TestCase):
    """With TZ unset, the app default used to be
    `datetime.now().astimezone().tzinfo` - which yields a FIXED offset for
    whatever the offset happened to be at import, frozen for the process's
    lifetime. Every day boundary, year boundary, streak day and calendar cell
    for a user with no profile timezone was therefore an hour out from the next
    DST transition until someone restarted the app. The live instance runs on
    Windows with TZ unset, so this was the live path.

    Simulated rather than relying on the host's own zone: CI runs on UTC, which
    has no DST, so a real-clock test would pass while proving nothing. `time`'s
    standard/DST offsets and its per-instant isdst answer are exactly what the
    replacement consults.
    """

    WINTER = datetime.datetime(2026, 1, 15, 12, 0)
    SUMMER = datetime.datetime(2026, 7, 15, 12, 0)

    def _zoneObservingDst(self):
        """A CET/CEST-like zone: UTC+1 standard, UTC+2 in summer."""
        realLocaltime = time.localtime

        def localtime(stamp=None):
            parts = realLocaltime(stamp)
            isDst = 1 if 3 < parts.tm_mon < 11 else 0
            return time.struct_time(tuple(parts)[:8] + (isDst,))

        return [
            patch.object(time, "timezone", -3600),    #< seconds WEST of UTC, so UTC+1
            patch.object(time, "altzone", -7200),     #< UTC+2 while DST is in effect
            patch.object(time, "daylight", 1),
            patch.object(time, "localtime", localtime),
        ]

    def _offsets(self):
        zone = utilsModule._SystemLocalTimezone()
        return zone.utcoffset(self.WINTER), zone.utcoffset(self.SUMMER)

    def test_the_offset_follows_the_season(self):
        with contextlib.ExitStack() as stack:
            for patcher in self._zoneObservingDst():
                stack.enter_context(patcher)

            winter, summer = self._offsets()

        self.assertEqual(winter, datetime.timedelta(hours=1))
        self.assertEqual(summer, datetime.timedelta(hours=2))

    def test_it_is_not_a_fixed_offset(self):
        """The whole defect in one assertion."""
        with contextlib.ExitStack() as stack:
            for patcher in self._zoneObservingDst():
                stack.enter_context(patcher)

            winter, summer = self._offsets()

        self.assertNotEqual(winter, summer)

    def test_dst_reports_the_extra_hour_only_in_summer(self):
        with contextlib.ExitStack() as stack:
            for patcher in self._zoneObservingDst():
                stack.enter_context(patcher)
            zone = utilsModule._SystemLocalTimezone()

            self.assertEqual(zone.dst(self.WINTER), datetime.timedelta(0))
            self.assertEqual(zone.dst(self.SUMMER), datetime.timedelta(hours=1))

    def test_a_zone_without_dst_reports_one_offset_all_year(self):
        with patch.object(time, "timezone", 0), patch.object(time, "altzone", 0), \
             patch.object(time, "daylight", 0):
            winter, summer = self._offsets()

        self.assertEqual(winter, datetime.timedelta(0))
        self.assertEqual(summer, datetime.timedelta(0))

    def test_an_out_of_range_date_degrades_instead_of_raising(self):
        """time.mktime rejects dates outside the platform's range, and this
        object is handed everything the app converts - including the epoch
        fallbacks and year-1 sentinels."""
        zone = utilsModule._SystemLocalTimezone()

        for extreme in (datetime.datetime(1, 1, 1), datetime.datetime(9999, 12, 31)):
            with self.subTest(extreme=extreme):
                self.assertIsInstance(zone.utcoffset(extreme), datetime.timedelta)

    def test_it_round_trips_a_timestamp(self):
        """Used as a real tzinfo: fromtimestamp then .timestamp() must return
        the value it started from, or every stored played_at shifts."""
        zone = utilsModule._SystemLocalTimezone()
        stamp = 1784694565.0

        restored = datetime.datetime.fromtimestamp(stamp, tz=zone).timestamp()

        self.assertEqual(restored, stamp)


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


class TestFormatTimeGap(unittest.TestCase):
    """formatTimeGap must convert a delta in seconds into human-readable time-gap strings."""

    def test_seconds_under_one_minute(self):
        self.assertEqual(utilsModule.formatTimeGap(30), "< 1 min later")
        self.assertEqual(utilsModule.formatTimeGap(0), "< 1 min later")

    def test_minutes(self):
        self.assertEqual(utilsModule.formatTimeGap(60), "1 min later")
        self.assertEqual(utilsModule.formatTimeGap(300), "5 mins later")
        self.assertEqual(utilsModule.formatTimeGap(3599), "59 mins later")

    def test_hours(self):
        self.assertEqual(utilsModule.formatTimeGap(3600), "1 hour later")
        self.assertEqual(utilsModule.formatTimeGap(7200), "2 hours later")
        self.assertEqual(utilsModule.formatTimeGap(82800), "23 hours later")

    def test_days(self):
        self.assertEqual(utilsModule.formatTimeGap(86400), "1 day later")
        self.assertEqual(utilsModule.formatTimeGap(86400 * 5), "5 days later")

    def test_months(self):
        self.assertEqual(utilsModule.formatTimeGap(86400 * 30), "1 month later")
        self.assertEqual(utilsModule.formatTimeGap(86400 * 90), "3 months later")

    def test_years(self):
        self.assertEqual(utilsModule.formatTimeGap(86400 * 365), "1 year later")
        self.assertEqual(utilsModule.formatTimeGap(86400 * 365 * 3), "3 years later")


class TestUnparseableInputNeverRaises(unittest.TestCase):
    """timeToInt / convertToDatetime / dateToString / parseDatetime document
    fallbacks (0 / epoch / "1970-01-01" / None) for input they cannot read -
    convertToDatetime's own docstring: "one bad date on one row must not take
    down a whole page".

    The blast radius when this breaks is the importer: json.loads accepts a
    bare NaN, one raising `ts` in the per-entry parse counts as
    droppedMalformed, droppedMalformed is in UNREADABLE_DROP_STAT_KEYS, and
    that aborts a whole overwrite import.

    The out-of-range cases need an explicit zone: year 9999 only overflows
    when converting EAST of UTC, year 1 only WEST, and the suite must fail on
    a UTC CI runner either way. (TZ env is useless here - Windows ignores it.)
    """

    def setUp(self):
        patcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nan_maps_to_the_documented_fallbacks(self):
        for value in ("nan", "NaN", " nan ", float("nan")):
            with self.subTest(value=value):
                self.assertEqual(utilsModule.timeToInt(value), 0)
                self.assertEqual(utilsModule.timeToIntUTC(value), 0)
                self.assertEqual(utilsModule.convertToDatetime(value), utilsModule.epoch())
                self.assertEqual(utilsModule.dateToString(value), "1970-01-01")
        self.assertIsNone(utilsModule.parseDatetime("nan"))

    def test_infinity_maps_to_the_documented_fallbacks(self):
        for value in ("inf", "-inf", float("inf")):
            with self.subTest(value=value):
                self.assertEqual(utilsModule.timeToInt(value), 0)
                self.assertEqual(utilsModule.convertToDatetime(value), utilsModule.epoch())

    def test_out_of_range_aware_iso_east_of_utc(self):
        """Year 9999 with a positive offset pushes past datetime.max in
        toTimezone - the exact OverflowError a Europe/* instance hit."""
        east = datetime.timezone(datetime.timedelta(hours=2))
        value = "9999-12-31T23:59:59Z"
        self.assertIsNone(utilsModule.parseDatetime(value, tz=east))
        self.assertEqual(utilsModule.convertToDatetime(value, tz=east),
                         utilsModule.epoch(tz=east))

    def test_out_of_range_aware_iso_west_of_utc(self):
        """The mirror image: year 1 with a negative offset underflows."""
        west = datetime.timezone(datetime.timedelta(hours=-5))
        value = "0001-01-01T00:00:00+00:00"
        self.assertIsNone(utilsModule.parseDatetime(value, tz=west))
        self.assertEqual(utilsModule.convertToDatetime(value, tz=west),
                         utilsModule.epoch(tz=west))

    def test_the_readable_forms_still_parse(self):
        """Negative control: hardening the failure paths must not have dulled
        the success ones."""
        self.assertEqual(utilsModule.timeToInt(1700000000), 1700000000)
        self.assertEqual(utilsModule.timeToInt("1700000000"), 1700000000)
        self.assertEqual(utilsModule.timeToInt("2024-03-05T12:00:00Z"), 1709640000)
        self.assertEqual(utilsModule.dateToString("2024-03-05T12:00:00Z"), "2024-03-05")
        self.assertEqual(utilsModule.convertToDatetime("0000-00-00"), utilsModule.epoch())


if __name__ == "__main__":
    unittest.main()

