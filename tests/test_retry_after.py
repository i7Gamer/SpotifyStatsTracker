"""The Retry-After header, parsed in one place (Database/rate_limit.py).

RFC 9110 gives the header two forms - delta-seconds and an HTTP-date - and the
copy in metadata_backfiller understood only the first, so an HTTP-date fell
through to the hour-long default stand-down. That default is safe, which is
exactly why the gap could sit there: honouring a 5-second date reads as a
45-minute outage, and nothing looks broken.

Bounded in both directions, which is the part that matters more than the
parsing: a missing or unparseable header is not "retry immediately", and an
absurd one is not "stop for a week".
"""
import datetime
import email.utils
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.rate_limit import retryAfterSeconds

DEFAULT = 60.0
MAXIMUM = 3600.0


class _Response:
    """Just the attribute the parser reads, so no HTTP stack is involved."""

    def __init__(self, headers=None):
        if headers is not None:
            self.headers = headers


def _parse(headerValue):
    headers = {} if headerValue is None else {"Retry-After": headerValue}
    return retryAfterSeconds(_Response(headers), default=DEFAULT, maximum=MAXIMUM)


def _httpDate(secondsFromNow):
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=secondsFromNow)
    return email.utils.format_datetime(when, usegmt=True)


class TestDeltaSeconds(unittest.TestCase):
    def test_a_plain_number_is_honoured(self):
        self.assertEqual(_parse("42"), 42.0)

    def test_a_fractional_value_is_honoured(self):
        self.assertEqual(_parse("1.5"), 1.5)

    def test_surrounding_whitespace_does_not_defeat_it(self):
        self.assertEqual(_parse("  42  "), 42.0)

    def test_zero_means_zero_rather_than_the_default(self):
        """"Retry now" is a real answer, and rounding it up to the default
        would turn a server saying "go ahead" into a minute of dead time."""
        self.assertEqual(_parse("0"), 0.0)


class TestHttpDate(unittest.TestCase):
    """The half that was missing."""

    def test_a_date_in_the_future_becomes_the_seconds_until_it(self):
        parsed = _parse(_httpDate(120))

        #< a second of slack: the clock moves between formatting and parsing
        self.assertAlmostEqual(parsed, 120, delta=2)

    def test_a_date_in_the_past_is_zero_not_negative(self):
        """A negative wait would be applied as an instant expiry at best, and
        as a backwards window at worst."""
        self.assertEqual(_parse(_httpDate(-300)), 0.0)

    def test_a_date_beyond_the_cap_is_capped(self):
        self.assertEqual(_parse(_httpDate(MAXIMUM * 10)), MAXIMUM)

    def test_a_date_without_a_zone_is_still_read(self):
        """RFC 9110 says an HTTP-date is always GMT; some servers omit the
        marker anyway, and a naive datetime must not raise on comparison."""
        when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=90)
        parsed = retryAfterSeconds(
            _Response({"Retry-After": when.strftime("%a, %d %b %Y %H:%M:%S")}),
            default=DEFAULT, maximum=MAXIMUM)

        self.assertAlmostEqual(parsed, 90, delta=2)

    def test_the_wait_is_measured_against_the_response_date_header(self):
        """Retry-After and Date come from the same server clock, so their
        difference is the wait regardless of what the local clock says.
        Pinned with stamps far in the past: measured against the local clock
        instead, this date reads as long gone, the wait collapses to 0, and
        the retry lands inside the very window the header announced - which
        is what a host a few seconds behind the server saw."""
        base = datetime.datetime(2020, 5, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)
        resp = _Response({
            "Retry-After": email.utils.format_datetime(
                base + datetime.timedelta(seconds=30), usegmt=True),
            "Date": email.utils.format_datetime(base, usegmt=True),
        })

        self.assertEqual(retryAfterSeconds(resp, default=DEFAULT, maximum=MAXIMUM), 30.0)

    def test_an_unparseable_date_header_falls_back_to_the_local_clock(self):
        resp = _Response({"Retry-After": _httpDate(120), "Date": "not a date"})

        self.assertAlmostEqual(
            retryAfterSeconds(resp, default=DEFAULT, maximum=MAXIMUM), 120, delta=2)

    def test_a_date_before_the_response_date_is_still_zero(self):
        """The genuinely-past case keeps its documented answer under the
        server-clock reference too."""
        base = datetime.datetime(2020, 5, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)
        resp = _Response({
            "Retry-After": email.utils.format_datetime(
                base - datetime.timedelta(seconds=300), usegmt=True),
            "Date": email.utils.format_datetime(base, usegmt=True),
        })

        self.assertEqual(retryAfterSeconds(resp, default=DEFAULT, maximum=MAXIMUM), 0.0)


class TestBounds(unittest.TestCase):
    def test_a_missing_header_falls_back_to_the_default(self):
        self.assertEqual(_parse(None), DEFAULT)

    def test_a_response_with_no_headers_at_all_falls_back(self):
        self.assertEqual(
            retryAfterSeconds(_Response(), default=DEFAULT, maximum=MAXIMUM), DEFAULT)

    def test_none_instead_of_a_response_falls_back(self):
        self.assertEqual(retryAfterSeconds(None, default=DEFAULT, maximum=MAXIMUM), DEFAULT)

    def test_unparseable_text_falls_back_to_the_default(self):
        for junk in ("soon", "", "  ", "NaN", "1,5"):
            with self.subTest(junk=junk):
                self.assertEqual(_parse(junk), DEFAULT)

    def test_an_absurd_value_is_capped(self):
        self.assertEqual(_parse("999999"), MAXIMUM)

    def test_a_negative_value_is_floored_at_zero(self):
        self.assertEqual(_parse("-5"), 0.0)

    def test_infinity_is_capped_rather_than_parked_forever(self):
        """float() accepts "inf", and an infinite backoff never reopens."""
        for value in ("inf", "Infinity", "-inf"):
            with self.subTest(value=value):
                self.assertLessEqual(_parse(value), MAXIMUM)

    def test_a_nan_is_not_smuggled_through_the_bounds(self):
        """min/max propagate NaN silently, and a NaN deadline compares False
        against everything - the window would never be considered over."""
        parsed = _parse("nan")

        self.assertEqual(parsed, DEFAULT)


if __name__ == "__main__":
    unittest.main()
