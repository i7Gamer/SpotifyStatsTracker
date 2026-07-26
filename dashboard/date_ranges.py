from __future__ import annotations

from datetime import datetime, timedelta
from Database.utils import convertToDatetime, msToString, now, parseDateString, startOfDay
from config import (
    COMPARE_TREND_WEEK_SPAN_DAYS, COMPARE_TREND_MONTH_SPAN_DAYS,
    CUSTOM_RANGE_MIN_YEAR, CUSTOM_RANGE_MAX_YEAR, MAX_TREND_BUCKETS,
)

# Approximate days per bucket, for _resolveGroupBy's explicit-choice guard.
# An ESTIMATE only - month uses its 28-day floor so the guard can never
# underestimate how many buckets a choice implies.
GROUP_BY_APPROX_BUCKET_DAYS = {"day": 1, "week": 7, "month": 28}


class DateRangeMixin:
    """Interval/date-range resolution, interval labels, and time-series/heatmap text embedding."""

    def _getValidInterval(self, interval, default="day"):
        """Validate interval parameter, falling back to default for unrecognized values."""
        valid_intervals = {"", "today", "day", "week", "month", "year", "5years", "all time", "custom"}
        return interval if interval in valid_intervals else default

    def _getValidGroupBy(self, groupBy, default="day"):
        """Validate groupBy parameter, falling back to default for unrecognized values."""
        return groupBy if groupBy in ("day", "week", "month") else default

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

                    startDate = startLocal
                    endDate = endLocal + timedelta(days=1)
                except ValueError:
                    # Unparseable dates used to fall through to the all-time
                    # branch below, showing the user's ENTIRE history under a
                    # "Custom range: x to y" label. Fall back to the default
                    # interval instead; _getIntervalLabel does the same.
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
            # Only claim a custom range if those dates actually parsed AND fell
            # inside the sanity window - _getDateRange falls back to `default`
            # otherwise (same _parseCustomRangeDate), and the label must not
            # promise a range the data doesn't cover.
            if (self._parseCustomRangeDate(customStart) is not None
                    and self._parseCustomRangeDate(customEnd) is not None):
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
