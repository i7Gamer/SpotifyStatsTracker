# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from flask import request
from datetime import datetime, timedelta
from Database.utils import convertToDatetime, msToString, now, parseDateString, startOfDay
from config import (
    COMPARE_TREND_WEEK_SPAN_DAYS, COMPARE_TREND_MONTH_SPAN_DAYS,
    CUSTOM_RANGE_MIN_YEAR, CUSTOM_RANGE_MAX_YEAR, MAX_TREND_BUCKETS,
)

# The interval values that make sense as a STORED default (a profile
# preference - routes/auth.py's save_preferences validates
# default_dashboard_window/default_top_list_window against this), which is
# _getValidInterval's per-REQUEST whitelist minus the two spellings that only
# make sense on one request at a time: "" (meaning "not specified" - a stored
# default cannot itself be "not specified") and "custom" (a stored date range
# would go stale the moment "today" moves past it). _getValidInterval extends
# this set with those two for its own whitelist, so a newly added interval
# only has to be added here - the traps memory's "drive it off the interval
# constant" rule.
SETTABLE_INTERVALS = frozenset({"today", "day", "week", "month", "year", "5years", "all time"})

# Approximate days per bucket, for _resolveGroupBy's explicit-choice guard.
# An ESTIMATE only - month uses its 28-day floor so the guard can never
# underestimate how many buckets a choice implies.
GROUP_BY_APPROX_BUCKET_DAYS = {"day": 1, "week": 7, "month": 28}

# The intervals a page renders bucketed by HOUR, which makes the Trend-buckets
# control a no-op that hides. The server-side twin of htmx-filters.js's
# SINGLE_DAY_INTERVALS, and it exists for the same reason that one does: the
# rule had been spelled out separately on /charts and /genres, and two
# spellings of one rule is how they stop mirroring. One home per side - this
# for the first paint, SINGLE_DAY_INTERVALS for every filter change after it.
SINGLE_DAY_INTERVALS = ("today", "day")


def isSingleDayInterval(interval) -> bool:
    """Whether `interval` is bucketed by hour, so the Trend-buckets control is
    meaningless for it. Feeds the templates' own hide rule - see
    SINGLE_DAY_INTERVALS."""
    return interval in SINGLE_DAY_INTERVALS


class DateRangeMixin:
    """Interval/date-range resolution, interval labels, and time-series/heatmap text embedding."""

    def _getValidInterval(self, interval, default="day"):
        """Validate interval parameter, falling back to default for unrecognized values."""
        valid_intervals = SETTABLE_INTERVALS | {"", "custom"}
        return interval if interval in valid_intervals else default

    def _getValidGroupBy(self, groupBy, default="day"):
        """Validate groupBy parameter, falling back to default for unrecognized values."""
        return groupBy if groupBy in ("day", "week", "month") else default

    def _resolveIntervalParam(self, absentDefault, emptyDefault, customStart=None, customEnd=None):
        """The current request's `?interval=`, resolved with the query's TWO
        different defaults - because an ABSENT param and a PRESENT-BUT-EMPTY
        one have never meant the same thing on every page.

        On the Dashboard, Charts, Genres and History pages the two defaults
        are equal (the page's own default window - the account's
        default_dashboard_window for the first three, the hardcoded "all
        time" for History), so this distinction is invisible there and both
        arguments are simply the same value.

        On the Top Songs/Artists/Albums pages it is NOT invisible: an ABSENT
        ?interval= means "no window was ever chosen for this view", so it
        takes the account's own default_top_list_window - a career ranking's
        separate setting from the dashboard's (see
        tests/test_top_list_default_window.py). A PRESENT BUT EMPTY
        ?interval= has always meant All Time instead - it is what every link
        these pages built before that per-user setting existed, and what the
        old `<option value="">All Time</option>` submitted. Left as `""` it
        would pass _getValidInterval intact (`""` is itself a valid
        interval), select All Time in the filter card, and then be DROPPED
        from every link built from it - see PaginationMixin._buildPageUrl and
        _topListShell's listArgs, which both drop falsy query values - so the
        request that link led to would re-resolve default_top_list_window:
        the card would say All Time over a list actually scoped to something
        else. This method's return value must therefore never BE `""` -
        every caller that means All Time spells it "all time" (see
        TOP_LIST_DEFAULT_WINDOW), the one spelling that survives into a URL.

        A present, non-empty, UNRECOGNIZED value (a stale or hand-edited URL)
        falls back to `absentDefault`, never to `emptyDefault` - junk must
        not hand the account a WIDER view than it configured
        (test_junk_falls_back_to_the_stored_window_not_to_all_time).

        `customStart`/`customEnd`, when given, gate "custom" the same way
        _getDateRange does: "custom" with either date missing falls back to
        `emptyDefault` - an incomplete custom range reads as "nothing was
        actually specified", the same bucket a present-but-empty ?interval=
        falls into, rather than as the account's usual window. Without this
        guard the filter card's <select> could show "Custom" selected over
        data that _getDateRange has already fallen back to All Time for
        (`/top-songs?interval=custom` used to do exactly this)."""
        raw = request.args.get("interval")
        if raw is None:
            interval = absentDefault
        elif raw == "":
            interval = emptyDefault
        else:
            interval = self._getValidInterval(raw, default=absentDefault)
        if interval == "custom" and not (customStart and customEnd):
            interval = emptyDefault
        return interval

    def _resolveGroupBy(self, groupByParam, startDate=None, endDate=None):
        """The trend-bucket size for a time-series chart: an explicit valid
        choice wins - unless it would slice the span into more than
        MAX_TREND_BUCKETS buckets (see its comment in config.py; the cap sits
        far beyond any real listening history, so only a hand-edited
        centuries-long custom range ever trips it) - and anything else (the
        "Auto" option's empty value, junk, or an over-cap explicit choice)
        derives day/week/month from the range span so the trend stays
        readable at any range - day buckets across a multi-year span are
        sub-pixel. Same thresholds Compare's trend has always auto-bucketed
        with; callers with an open-ended range (all time, a detail page's
        whole item history) pass play-range-derived dates, and no dates at
        all fall back to day."""
        spanDays = (endDate - startDate).days if startDate and endDate else 0
        if (groupByParam in GROUP_BY_APPROX_BUCKET_DAYS
                and spanDays <= MAX_TREND_BUCKETS * GROUP_BY_APPROX_BUCKET_DAYS[groupByParam]):
            return groupByParam
        if spanDays > COMPARE_TREND_MONTH_SPAN_DAYS:
            return "month"
        if spanDays > COMPARE_TREND_WEEK_SPAN_DAYS:
            return "week"
        return "day"

    def _playRangeSpanDates(self, username, tz, trackId=None, artistId=None, albumId=None):
        """(start, end) datetimes spanning the user's (or one item's) whole
        play history, or (None, None) with no plays - the span an open-ended
        range's auto trend-bucket resolution derives from (see
        _resolveGroupBy). Reads the shared repo like Compare's identical
        all-time pinning does; used by /charts and /genres for "All Time" and
        by the detail pages for an item's whole history."""
        playRange = self.repo.getPlayTimeRange(username, trackId=trackId,
                                               artistId=artistId, albumId=albumId)
        if not playRange:
            return None, None
        return convertToDatetime(playRange[0], tz=tz), convertToDatetime(playRange[1], tz=tz)

    def _getDateRange(self, interval: str = None, customStart: str = None, customEnd: str = None, default="day", tz=None):
            """Get start and end dates based on interval or custom dates.

            Returns a half-open local interval [startDate, endDate).
            """
            # Coerce an unrecognized interval to `default` up front: without this
            # a junk ?interval= (e.g. from a hand-edited URL) fell through to the
            # all-time branch below, silently returning ALL-TIME data - and
            # _getIntervalLabel would then label it "Yesterday". "all time" and
            # the other known values pass through unchanged.
            interval = self._getValidInterval(interval, default)
            nowLocal = now(tz=tz)
            startDate = None

            #< the start of tomorrow, local time - always covers all of today
            #  regardless of what time "now" is, without spilling a whole extra
            #  day (with today's time-of-day) into a future bucket
            endDate = convertToDatetime(startOfDay(nowLocal + timedelta(days=1), tz=tz), tz=tz)

            if interval == "":
                interval = default

            # Custom dates apply ONLY to the custom interval. They used to win
            # unconditionally, so a stale or hand-edited URL carrying both
            # (?interval=week&startDate=2020-01-01&endDate=2020-01-02) rendered
            # 2020's data under a "Last Week" label - the routes only guard the
            # inverse case (custom with no dates).
            if interval == "custom" and customStart and customEnd:
                try:
                    startLocal = self._parseCustomRangeDate(customStart, tz=tz)
                    endLocal = self._parseCustomRangeDate(customEnd, tz=tz)
                    if startLocal is None or endLocal is None:
                        raise ValueError("Invalid custom date")
                    if self._customRangeIsInverted(startLocal, endLocal):
                        raise ValueError("Custom range start is after its end")

                    startDate = startLocal
                    endDate = endLocal + timedelta(days=1)
                except ValueError:
                    # Unparseable dates used to fall through to the all-time
                    # branch below, showing the user's ENTIRE history under a
                    # "Custom range: x to y" label. Fall back to the default
                    # interval instead; _getIntervalLabel does the same. An
                    # INVERTED pair (start after end) takes the same exit: the
                    # filter card already refuses it (htmx-filters.js's
                    # RANGE_INVERTED), so only a shared or hand-edited URL
                    # carries one, and honouring it rendered an empty window
                    # under a label claiming the range. Not swapped - the
                    # server and the client should agree the input is invalid,
                    # not disagree about what it meant.
                    interval = default
            if not startDate:
                if interval == "today":
                    startDate = convertToDatetime(startOfDay(nowLocal, tz=tz), tz=tz)
                    endDate = convertToDatetime(startOfDay(nowLocal + timedelta(days=1), tz=tz), tz=tz)

                elif interval == "day":
                    startDate = convertToDatetime(startOfDay(nowLocal - timedelta(days=1), tz=tz), tz=tz)
                    endDate = convertToDatetime(startOfDay(nowLocal, tz=tz), tz=tz)

                elif interval == "week":
                    startDate = nowLocal - timedelta(weeks=1)

                elif interval == "month":
                    startDate = nowLocal - timedelta(days=30)

                elif interval == "year":
                    startDate = nowLocal - timedelta(days=365)

                elif interval == "5years":
                    startDate = nowLocal - timedelta(days=365*5)
                else:
                    startDate = None
                    endDate = None

            return startDate, endDate

    @staticmethod
    def _parseCustomRangeDate(dateText, tz=None):
        """parseDateString, plus the CUSTOM_RANGE_MIN_YEAR..MAX_YEAR sanity
        window (see config.py for why those bounds): a custom date outside it
        is treated exactly like an unparseable one - None - so _getDateRange
        falls back to the default interval and _getIntervalLabel never claims
        a custom range the data isn't actually using. Both MUST resolve
        custom dates through this one helper, or the label and the data can
        disagree about whether a range was accepted."""
        parsed = parseDateString(dateText, tz=tz)
        if parsed is None or not (CUSTOM_RANGE_MIN_YEAR <= parsed.year <= CUSTOM_RANGE_MAX_YEAR):
            return None
        return parsed

    @staticmethod
    def _customRangeIsInverted(startLocal, endLocal) -> bool:
        """Whether a parsed custom pair has its start AFTER its end - the
        second way a custom range is refused, beside _parseCustomRangeDate's
        None. Strictly `>`: start == end is the one-day range the filter card
        submits for a single day. Same rule as htmx-filters.js's
        RANGE_INVERTED check; _getDateRange and _getIntervalLabel both go
        through here so the data and the label cannot disagree."""
        return startLocal > endLocal

    def _getIntervalLabel(self, interval: str = None, customStart: str = None, customEnd: str = None, default: str = "day"):
        # Match _getDateRange: an unrecognized interval takes `default`, so the
        # label can't disagree with the data (junk no longer reads "Yesterday"
        # over some other range's numbers).
        interval = self._getValidInterval(interval, default)
        labels = {
            "all time": "All Time",
            "today": "Today",
            "day": "Yesterday",
            "week": "Last Week",
            "month": "Last Month",
            "year": "Last Year",
            "5years": "Last 5 Years",
        }

        if interval == "custom" and customStart and customEnd:
            # Only claim a custom range if those dates actually parsed, fell
            # inside the sanity window AND are not inverted - _getDateRange
            # falls back to `default` otherwise (same _parseCustomRangeDate and
            # _customRangeIsInverted), and the label must not promise a range
            # the data doesn't cover.
            startLocal = self._parseCustomRangeDate(customStart)
            endLocal = self._parseCustomRangeDate(customEnd)
            if (startLocal is not None and endLocal is not None
                    and not self._customRangeIsInverted(startLocal, endLocal)):
                return f"Custom range: {customStart} to {customEnd}"
            return labels.get(default, "Yesterday")

        # `or default`, not `or "day"`: "" is a valid interval and _getDateRange
        # resolves it to `default`, so mapping it to "day" here labelled all-time
        # (or last-month, or whatever the user's default is) data as "Yesterday".
        return labels.get(interval or default, "Yesterday")

    def _embedTimeSeriesTextElements(self, timeSeries: list, groupBy: str | None = None) -> list:
        """groupBy: when given (Charts page only - see chartsPage()'s call
        site), also stamps rangeStart/rangeEnd onto each bucket so
        static/js/charts.js's click-to-navigate can link a clicked bar to
        the Dashboard scoped to that exact bucket. Omitted elsewhere
        (Wrapped's own time-series chart, detail pages) since those charts
        don't support click-navigation."""
        for bucket in timeSeries:
            bucket["totalTimeListenedText"] = msToString(bucket["totalTimeListened"])
            bucketRange = self._timeSeriesBucketRange(bucket["label"], groupBy)
            if bucketRange is not None:
                bucket["rangeStart"], bucket["rangeEnd"] = bucketRange
        return timeSeries

    @staticmethod
    def _timeSeriesBucketRange(label: str, groupBy: str | None) -> tuple[str, str] | None:
        """The [inclusive start day, inclusive end day] a time-series
        bucket's label represents, as plain "YYYY-MM-DD" strings - matches
        _getDateRange's custom-range contract, which treats its own endDate
        as inclusive (it adds one day itself), so these values round-trip
        straight into a `?interval=custom&startDate=...&endDate=...` link.
        None for a groupBy without a clean calendar-date mapping (e.g. the
        Charts single-day view's hourly buckets - see chartsPage's
        timeSeriesGroupBy) or a label that doesn't parse, so a bucket like
        that is simply left un-clickable rather than linking somewhere
        wrong."""
        if groupBy not in ("day", "week", "month"):
            return None
        try:
            if groupBy == "week":
                start = datetime.strptime(label, "%Y-%m-%d")
                end = start + timedelta(days=6)
            elif groupBy == "month":
                start = datetime.strptime(label, "%Y-%m")
                nextMonth = (datetime(start.year + 1, 1, 1) if start.month == 12
                             else datetime(start.year, start.month + 1, 1))
                end = nextMonth - timedelta(days=1)
            else:
                start = datetime.strptime(label, "%Y-%m-%d")
                end = start
        except ValueError:
            return None
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    def _embedHeatmapTextElements(self, heatmap: list) -> list:
        for row in heatmap:
            for cell in row:
                cell["totalTimeListenedText"] = msToString(cell["totalTimeListened"])
        return heatmap
