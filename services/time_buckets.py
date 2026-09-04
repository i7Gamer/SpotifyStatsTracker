# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local-timezone bucketing for the trend/heatmap/time-series charts.

Pure layout logic (no DB, no Flask) so it unit-tests against plain lists of
pre-aggregated bucket rows - mirroring services/listening_calendar.py, which
took the same shape for the streak calendar (Database.getListeningCalendar
gathers per-day counts and calls buildListeningCalendar; the pattern here is
identical). Database.database's getGenreTrends, getArtistTrend,
getHourOfDayHeatmap, getGenreHourOfDayHeatmap and getListeningTimeSeries each
reduce to one call into here, behind a same-named one-line delegation -
Database instances are still the caller everywhere else in the app, and the
test suite's `patch("Database.database.X")` sites need those names to keep
existing.

Every function here takes the caller's IANA tz explicitly rather than reading
it off a Database instance - the row->local-date mapping is a per-user
question (see Database.utils' own note on the same point), so a helper that
defaulted to the app-global timezone would re-base a play into the wrong
day/week for any user whose profile timezone differs from it."""
import datetime
import logging

from Database.utils import convertToDatetime, dateToString, startOfDay, startOfMonth, startOfWeek

logger = logging.getLogger(__name__)

# Hard ceiling on buildTimeSeries' gap-fill: an unvalidated custom range used
# to emit one zero bucket per day across centuries (~740k dicts, seconds of
# CPU, a >100MB payload). The route layer's own guards (_resolveGroupBy's
# explicit-choice cap, the custom-range year bounds) keep every real request
# far below this; this is the query-layer backstop.
MAX_TIME_SERIES_BUCKETS = 12_000
# The fewest days one bucket of each groupBy can span, so
# `MAX_TIME_SERIES_BUCKETS * minBucketDays` bounds the clamp above in days
# regardless of which bucket size the caller asked for.
TIME_SERIES_MIN_BUCKET_DAYS = {"hour": 1 / 24, "day": 1, "week": 7, "month": 28}


def bucketKey(date: datetime.datetime, groupBy: str, tz) -> str:
    """The chart-bucket label for one already-local `date` - lexically
    sortable at every groupBy, so a sorted union of keys stays chronological
    (getGenreTrends and getArtistTrend both rely on that).

    `date` must already be in `tz`; startOfDay/startOfWeek/dateToString
    default to the app-global TZ, which would re-base the datetime onto the
    server's calendar and shift midnight-adjacent plays into the wrong
    day/week for any user whose profile timezone differs from it - so `tz`
    is threaded through explicitly rather than relied on as a default."""
    if groupBy == "week":
        return dateToString(startOfWeek(date, tz=tz), tz=tz)
    elif groupBy == "hour":
        return date.strftime("%Y-%m-%d %H:00")
    elif groupBy == "month":
        return date.strftime("%Y-%m")
    else:
        return dateToString(startOfDay(date, tz=tz), tz=tz)


def bucketRowsByKey(rows, tz, groupBy: str) -> list[tuple[str, dict]]:
    """[(bucketKey, row), ...] for every row, in the given order.

    Many rows share the same 15-minute `bucketStartTs` (one per genre/artist
    active in it), so the local-timezone conversion + bucket-key mapping is
    memoized per distinct bucketStartTs rather than recomputed per row - on a
    large library this collapsed ~77k getArtistTrend rows to ~21k
    conversions. Shared by getGenreTrends and getArtistTrend, which each do
    their own aggregation on top (a genre's per-bucket totals; an artist's
    plus its top-N/click-through pick), so this stops at the per-row mapping
    rather than folding in either one's aggregation."""
    cache: dict = {}
    result = []
    for row in rows:
        bucketStartTs = row["bucketStartTs"]
        key = cache.get(bucketStartTs)
        if key is None:
            key = bucketKey(convertToDatetime(bucketStartTs, tz=tz), groupBy, tz)
            cache[bucketStartTs] = key
        result.append((key, row))
    return result


def fillHeatmapGrid(rows, tz) -> list[list[dict]]:
    """7x24 grid (rows Monday=0..Sunday=6, columns hour-of-day 0-23) of total
    listening time and play count - the 'when do I listen' heatmap shared
    (byte-identical, before this extraction) by getHourOfDayHeatmap and
    getGenreHourOfDayHeatmap. `rows` are pre-aggregated 15-minute buckets
    (Repository.getBucketedPlayTotals / getGenreBucketedPlayTotals); each
    maps to one local weekday/hour cell."""
    grid = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
    for row in rows:
        date = convertToDatetime(row["bucketStartTs"], tz=tz)
        cell = grid[date.weekday()][date.hour]
        cell["totalTimeListened"] += row["totalTimeListened"]
        cell["plays"] += row["plays"]
    return grid


def buildTimeSeries(rows, tz, groupBy: str = "day",
                     startDate: datetime.datetime | None = None,
                     endDate: datetime.datetime | None = None) -> list[dict]:
    """Total listening time and play count per day/week/hour/month,
    gap-filled with zero-value buckets so a bar chart shows a continuous
    timeline - getListeningTimeSeries' body, minus fetching `rows`
    (Repository.getBucketedPlayTotals, via Database.getPlayBuckets).

    `startDate`/`endDate` bound the gap-fill when both are given; otherwise
    it's bounded by the first/last row's bucket (rows are ordered by bucket
    start, so those bound the same chart buckets the raw plays would).
    Empty `rows` with no explicit range returns []."""
    buckets = {}
    for row in rows:
        date = convertToDatetime(row["bucketStartTs"], tz=tz)
        key = bucketKey(date, groupBy, tz)
        bucket = buckets.setdefault(key, {"label": key, "totalTimeListened": 0, "plays": 0, "skips": 0})
        bucket["totalTimeListened"] += row["totalTimeListened"]
        bucket["plays"] += row["plays"]
        bucket["skips"] += row.get("skips", 0)

    if startDate is not None and endDate is not None:
        rangeStart, rangeEnd = startDate, endDate
    elif rows:
        # rows are ordered by bucket start; the first/last bucket start in
        # local time bounds the same chart buckets the raw plays would.
        rangeStart = convertToDatetime(rows[0]["bucketStartTs"], tz=tz)
        rangeEnd = convertToDatetime(rows[-1]["bucketStartTs"], tz=tz) + datetime.timedelta(seconds=1)
    else:
        return []

    # The aligner walks the same local calendar bucketKey labels by, so it
    # takes the caller's timezone too - otherwise the gap-filled timeline
    # emits server-local bucket labels a play bucket can never match.
    if groupBy == "week":
        align = lambda d: startOfWeek(d, tz=tz)
        advance = lambda d: d + datetime.timedelta(days=7)
        minBucketDays = TIME_SERIES_MIN_BUCKET_DAYS["week"]
    elif groupBy == "hour":
        align = lambda d: d.replace(minute=0, second=0, microsecond=0)
        # A plain `d + timedelta(hours=1)` only touches the naive fields of a
        # zoneinfo-aware datetime - it never asks "is this wall-clock hour
        # real". On the two DST-transition days that walks straight over the
        # local hour that never happened (Europe/Berlin spring-forward:
        # 02:00 skips to 03:00, so the naive walk still labels a bucket
        # "02:00" that no play can ever land in) and, on fall-back, revisits
        # the same wall-clock "02:00" label without ever reaching its second,
        # later instant (fold=1) as a step of its own. Advancing via a UTC
        # round-trip instead steps by a real elapsed hour, so the local wall
        # time that comes out already reflects whatever the clock actually
        # did at that boundary - the phantom hour is never produced, and the
        # doubled one naturally re-lands on the same label (deduped below,
        # rather than emitted as a second bucket) with both hours' rows
        # already merged into it by the `buckets` dict above (keyed the same
        # way, off the row's real bucketStartTs).
        advance = lambda d: (d.astimezone(datetime.timezone.utc) + datetime.timedelta(hours=1)).astimezone(tz)
        minBucketDays = TIME_SERIES_MIN_BUCKET_DAYS["hour"]
    elif groupBy == "month":
        # A fixed timedelta step doesn't work here since months vary in
        # length - advance to the 1st of the next calendar month instead.
        align = lambda d: startOfMonth(d, tz=tz)
        advance = lambda d: d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)
        minBucketDays = TIME_SERIES_MIN_BUCKET_DAYS["month"]
    else:
        align = lambda d: startOfDay(d, tz=tz)
        advance = lambda d: d + datetime.timedelta(days=1)
        minBucketDays = TIME_SERIES_MIN_BUCKET_DAYS["day"]
    cursor = align(rangeStart)

    # Backstop bound on the gap-fill (see MAX_TIME_SERIES_BUCKETS): when the
    # requested range implies more buckets than the cap, the START is
    # clamped up - the newest buckets are what a chart is about - and
    # re-aligned onto the same bucket grid. The route layer's own guards
    # keep every real request far below this; only a caller passing
    # unvalidated dates straight in can trip it.
    try:
        earliestStart = rangeEnd - datetime.timedelta(days=MAX_TIME_SERIES_BUCKETS * minBucketDays)
    except OverflowError:
        earliestStart = None   #< rangeEnd within the cap of datetime.min - the range itself is small
    if earliestStart is not None and cursor < earliestStart:
        logger.warning(
            "Time-series range %s..%s implies more than %d %s buckets - clamping the range start",
            rangeStart, rangeEnd, MAX_TIME_SERIES_BUCKETS, groupBy,
        )
        cursor = align(earliestStart)

    result = []
    while cursor < rangeEnd:
        key = bucketKey(cursor, groupBy, tz)
        # Only "hour" can ever repeat the PREVIOUS label: fall-back's UTC-real
        # advance (above) steps through both instants of the doubled local
        # hour, and both stringify to the same "HH:00" key. Every other
        # groupBy's keys are already strictly increasing, so this is a no-op
        # for them - not a second DST rule to keep in sync with the one above.
        if not result or key != result[-1]["label"]:
            result.append(buckets.get(key, {"label": key, "totalTimeListened": 0, "plays": 0, "skips": 0}))
        cursor = advance(cursor)
    return result
