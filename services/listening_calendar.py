"""Listening streak calendar: a GitHub-style contribution grid of the days a
user listened, each day shaded by its play volume relative to the busiest day
in the window.

Pure layout logic (no DB, no Flask) so it unit-tests against a plain
{date: count} map, mirroring services/milestones.py and services/genre_gate.py.
Database.getListeningCalendar gathers the per-day counts and calls
buildListeningCalendar; the dashboard renders the returned grid
(templates/tracks.html), reinforcing the Listening streak card above it."""
import datetime
import math

CALENDAR_WEEKS = 53                 #< week columns shown (~1 year, GitHub-style)
CALENDAR_INTENSITY_LEVELS = 4       #< non-zero heat buckets (1..4); 0 = no plays
_DAYS_PER_WEEK = 7


def _intensityLevel(count: int, maxCount: int) -> int:
    """0 for no plays, else 1..CALENDAR_INTENSITY_LEVELS by the day's share of
    the busiest day in the window. Relative (not fixed) buckets so the grid
    reads well for light and heavy listeners alike, like GitHub's graph."""
    if count <= 0 or maxCount <= 0:
        return 0
    level = math.ceil(count / maxCount * CALENDAR_INTENSITY_LEVELS)
    return max(1, min(CALENDAR_INTENSITY_LEVELS, level))


def buildListeningCalendar(dayCounts: dict, today: datetime.date,
                           weeks: int = CALENDAR_WEEKS) -> dict:
    """Grid model for the streak calendar.

    `dayCounts` maps "%Y-%m-%d" -> play count (days outside the window or in the
    future are ignored); `today` is the user's local date. Returns::

        {"weeks": [[cell, ...7], ... up to `weeks` columns],   # oldest col first
         "monthLabels": [{"label": "Jul", "weekIndex": 40}, ...],
         "maxCount": int, "activeDays": int, "totalPlays": int}

    Each cell is {"date", "count", "level" (0..4), "today", "future"}. Columns
    are weeks and rows are Mon..Sun (matching the app's listening-clock heatmap);
    the last column is the current week, so days after `today` in it are marked
    future (rendered blank). Levels are assigned in a second pass once the
    window's busiest day is known."""
    lastMonday = today - datetime.timedelta(days=today.weekday())   #< weekday(): Mon=0
    firstMonday = lastMonday - datetime.timedelta(days=(weeks - 1) * _DAYS_PER_WEEK)

    grid = []           #< weeks[col][row] before levels are filled in
    maxCount = 0
    activeDays = 0
    totalPlays = 0
    monthLabels = []
    for col in range(weeks):
        colMonday = firstMonday + datetime.timedelta(days=col * _DAYS_PER_WEEK)
        column = []
        for row in range(_DAYS_PER_WEEK):
            cellDate = colMonday + datetime.timedelta(days=row)
            future = cellDate > today
            dateStr = cellDate.isoformat()
            count = 0 if future else dayCounts.get(dateStr, 0)
            if count > 0:
                activeDays += 1
                totalPlays += count
                maxCount = max(maxCount, count)
            column.append({"date": dateStr, "count": count,
                           "level": 0, "today": cellDate == today, "future": future})
            # One month label per column, at the column whose week contains that
            # month's 1st - the same placement GitHub uses along the top axis.
            if cellDate.day == 1 and (not monthLabels or monthLabels[-1]["weekIndex"] != col):
                monthLabels.append({"label": cellDate.strftime("%b"), "weekIndex": col})
        grid.append(column)

    if maxCount > 0:
        for column in grid:
            for cell in column:
                cell["level"] = _intensityLevel(cell["count"], maxCount)

    return {"weeks": grid, "monthLabels": monthLabels,
            "maxCount": maxCount, "activeDays": activeDays, "totalPlays": totalPlays}
