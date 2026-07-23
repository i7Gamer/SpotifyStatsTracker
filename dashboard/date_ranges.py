from __future__ import annotations

from datetime import datetime, timedelta
from Database.utils import convertToDatetime, msToString, now, parseDateString, startOfDay
from config import COMPARE_TREND_WEEK_SPAN_DAYS, COMPARE_TREND_MONTH_SPAN_DAYS


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
        choice wins; anything else (the "Auto" option's empty value, or junk)
        derives day/week/month from the range span so the trend stays
        readable at any range - day buckets across a multi-year span are
        sub-pixel. Same thresholds Compare's trend has always auto-bucketed
        with; callers with an open-ended range (all time, a detail page's
        whole item history) pass play-range-derived dates, and no dates at
        all fall back to day."""
        if groupByParam in ("day", "week", "month"):
            return groupByParam
        spanDays = (endDate - startDate).days if startDate and endDate else 0
        if spanDays > COMPARE_TREND_MONTH_SPAN_DAYS:
            return "month"
        if spanDays > COMPARE_TREND_WEEK_SPAN_DAYS:
            return "week"
        return "day"

    def _getDateRange(self, interval: str = None, customStart: str = None, customEnd: str = None, default="day", tz=None):
            """Get start and end dates based on interval or custom dates.

            Returns a half-open local interval [startDate, endDate).
            """
            nowLocal = now(tz=tz)
            startDate = None

            futureBuffer = timedelta(days=1) 

            endDate = nowLocal + futureBuffer   #< bypass any timezone issues

            if customStart and customEnd:
                try:
                    startLocal = parseDateString(customStart, tz=tz)
                    endLocal = parseDateString(customEnd, tz=tz)
                    if startLocal is None or endLocal is None:
                        raise ValueError("Invalid custom date")

                    startDate = startLocal
                    endDate = endLocal + timedelta(days=1)
                except ValueError:
                    pass
            if interval == "":
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

    def _getIntervalLabel(self, interval: str = None, customStart: str = None, customEnd: str = None):
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
            return f"Custom range: {customStart} to {customEnd}"

        return labels.get(interval or "day", "Yesterday")

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
