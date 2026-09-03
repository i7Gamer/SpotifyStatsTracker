# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Subtitle text for the dashboard's four trend cards (Obsession,
Rediscovery, Fresh Find, Forgotten Favorite).

Pure string formatting (no DB, no Flask) - mirrors services/time_buckets.py
and services/listening_calendar.py. Database.getDashboardTrends fetches the
raw per-kind row (Repository.getDashboardTrendsRaw), hydrates it with track
metadata, and calls one formatter here per populated card to fill in
song['trend_subtitle']."""
from Database.utils import GAP_DAYS_PER_MONTH, SECONDS_PER_DAY


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' if count != 1 else ''}"


def obsessionSubtitle(item: dict) -> str:
    """"N play(s) in the past week"."""
    return f"{_plural(item['recent_count'], 'play')} in the past week"


def rediscoverySubtitle(item: dict, now_ts: float) -> str:
    """"N play(s) this week · unplayed for D days"."""
    days_ago = _daysAgo(item["max_old_played_at"], now_ts, floor=1)
    return f"{_plural(item['recent_count'], 'play')} this week · unplayed for {days_ago} days"


def freshFindSubtitle(item: dict, now_ts: float) -> str:
    """"N play(s) · first heard today/D day(s) ago".

    Floored at 0, not at 1 like the two cards either side. Their floor is
    unreachable - both require a 30+ day gap - and this one's window is 14
    days against a two-play bar, so "found it this morning" is the ordinary
    case rather than the edge. With their floor it read "first heard 1 day
    ago" for a track whose first play was four hours old."""
    days_ago = _daysAgo(item["first_played_at"], now_ts, floor=0)
    heard = "today" if days_ago == 0 else f"{days_ago} day{'s' if days_ago != 1 else ''} ago"
    return f"{_plural(item['play_count'], 'play')} · first heard {heard}"


def forgottenSubtitle(item: dict, now_ts: float) -> str:
    """"N full plays all-time · last played M month(s) ago"."""
    days_ago = _daysAgo(item["last_played_at"], now_ts, floor=1)
    months_ago = max(1, days_ago // GAP_DAYS_PER_MONTH)
    return (f"{item['total_plays']} full plays all-time · "
            f"last played {_plural(months_ago, 'month')} ago")


def _daysAgo(pastTs: float | None, now_ts: float, floor: int) -> int:
    """Whole days between `pastTs` and `now_ts`, floored at `floor` (never
    negative even when `pastTs` is somehow after `now_ts` - a clock
    correction, say - "-1 days ago" reads worse than "today"/"N days ago"
    with N clamped up to `floor`). 0 when `pastTs` is falsy/unknown."""
    if not pastTs:
        return 0
    return max(floor, int((now_ts - pastTs) // SECONDS_PER_DAY))
