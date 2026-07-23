"""Per-user achievement-milestone detection and display.

Detects when a user crosses a lifetime play-count / listen-time threshold,
reaches a listening-streak length, or gets a new all-time #1 artist, and records
each as a row in user_milestones (see Database/queries/milestones.py). The
topbar badge (app.py's _injectMilestoneStatus) and the Profile Milestones
section read those rows back.

First-run seeding: a user's very first detection pass records every
already-achieved milestone as *seen* and stamps users.milestones_baseline_at, so
shipping the feature never floods an existing account with notifications for
milestones it passed long ago. Only milestones crossed after that baseline
notify (seen=0).

Kept free of Flask/template concerns so it stays unit-testable against a plain
Database + Repository, mirroring services/genre_gate.py and services/taste_match.py.
"""
import datetime
import json
import logging
import time
from zoneinfo import ZoneInfo

from Database.utils import convertToDatetime, getTimezone

logger = logging.getLogger(__name__)

MILESTONE_KIND_PLAYS = "plays"
MILESTONE_KIND_LISTEN_TIME = "listen_time"
MILESTONE_KIND_STREAK = "streak"
MILESTONE_KIND_TOP_ARTIST = "top_artist"

# Ascending thresholds. Detection records every threshold at/below the current
# value; only ones crossed after a user's baseline notify (seen=0).
MILESTONE_PLAYS_THRESHOLDS = (1000, 5000, 10000, 25000, 50000, 100000, 250000, 500000, 1000000)
MILESTONE_LISTEN_HOURS_THRESHOLDS = (100, 250, 500, 1000, 2500, 5000, 10000)
MILESTONE_STREAK_DAY_THRESHOLDS = (7, 30, 100, 365, 1000)

MS_PER_HOUR = 1000 * 60 * 60


def _detectThresholdMilestones(repo, username, kind, thresholds, currentValue, achievedAt, seen) -> int:
    """Record every not-yet-recorded threshold in `thresholds` (ascending) that
    currentValue has reached. Returns how many rows were newly recorded."""
    recorded = 0
    for threshold in thresholds:
        if currentValue < threshold:
            break  # ascending list - nothing further can be reached
        if not repo.hasThresholdMilestone(username, kind, threshold):
            repo.recordMilestone(username, kind, threshold, None, achievedAt, seen)
            recorded += 1
    return recorded


def _detectTopArtistMilestone(repo, db, username, achievedAt, seen) -> int:
    """Record a top_artist milestone when the all-time #1 artist differs from
    the last one recorded (or none has been recorded yet). Returns 1 if a row
    was recorded, else 0."""
    topArtists = db.getTopArtists(startDate=None, endDate=None, by="plays", limit=1)
    if not topArtists:
        return 0
    top = topArtists[0]
    artistId = top.get("id")
    artistName = top.get("name")
    if not artistId or not artistName:
        return 0

    latest = repo.getLatestMilestone(username, MILESTONE_KIND_TOP_ARTIST)
    if latest is not None:
        try:
            prev = json.loads(latest["detail"]) if latest.get("detail") else {}
        except (ValueError, TypeError):
            prev = {}
        if prev.get("id") == artistId:
            return 0  # unchanged #1 - nothing to record

    repo.recordMilestone(
        username, MILESTONE_KIND_TOP_ARTIST, 0,
        json.dumps({"id": artistId, "name": artistName}), achievedAt, seen)
    return 1


def detectMilestones(db, repo, username, changeCache=None, markSeen=False) -> int:
    """Detect and record any newly-reached milestones for `username`, returning
    how many rows were recorded this pass.

    On the user's first pass (no milestones_baseline_at yet) everything already
    achieved is recorded as seen and the baseline is stamped; afterwards new
    crossings are recorded unseen (seen=0) so the topbar badge surfaces them.
    `markSeen=True` records this pass's crossings as already seen instead -
    the import-backfill case (see _detectMilestonesSafely in app.py): crossings
    surfaced by imported history are past achievements, so they get the same
    no-notification contract as first-pass seeding rather than flooding the
    badge.

    `changeCache` is an optional mutable {username: (totalPlays, totalMs)} dict
    (the periodic background loop passes a per-process one). When supplied, a
    non-seeding pass whose play totals equal the last pass's short-circuits
    before the heavier streak + top-artist queries: every milestone kind derives
    from the plays table, so unchanged (count, listen-time) totals mean nothing
    can have crossed. getPlayTotals is a single indexed scan and doubles as that
    change signal, so it always runs; the join-and-group getTopArtists query and
    the streak scan are what the guard saves on the common idle cycle. Omitting
    the cache keeps the old always-run behavior (used by the unit tests)."""
    now = time.time()
    baseline = repo.getMilestoneBaselineAt(username)
    seed = baseline is None   #< first-ever pass: seed already-achieved milestones silently
    seen = seed or markSeen

    totalPlays, totalMs = db.getPlayTotals(None, None)
    # Idle-cycle short-circuit (see docstring). Never on the seeding pass - that
    # must record the already-achieved backlog once, even against a stale cache.
    if changeCache is not None and not seed and changeCache.get(username) == (totalPlays, totalMs):
        return 0

    totalHours = (totalMs or 0) // MS_PER_HOUR
    streak = db.getCurrentStreak()
    streakDays = streak.get("days", 0) if isinstance(streak, dict) else 0

    recorded = 0
    recorded += _detectThresholdMilestones(
        repo, username, MILESTONE_KIND_PLAYS, MILESTONE_PLAYS_THRESHOLDS, totalPlays, now, seen)
    recorded += _detectThresholdMilestones(
        repo, username, MILESTONE_KIND_LISTEN_TIME, MILESTONE_LISTEN_HOURS_THRESHOLDS, totalHours, now, seen)
    recorded += _detectThresholdMilestones(
        repo, username, MILESTONE_KIND_STREAK, MILESTONE_STREAK_DAY_THRESHOLDS, streakDays, now, seen)
    recorded += _detectTopArtistMilestone(repo, db, username, now, seen)

    if seed:
        repo.setMilestoneBaselineAt(username, now)
    if changeCache is not None:
        changeCache[username] = (totalPlays, totalMs)
    return recorded


def resolveUserTimezone(repo, username):
    """The tz `username`'s local day boundaries use - users.timezone when set
    and valid, else the app default - mirroring Database.refreshSettings so
    recalculated streak dates agree with what the streak features show."""
    try:
        tzName = repo.getUserSettings(username).get("timezone")
        if tzName:
            return ZoneInfo(tzName)
    except Exception:
        pass
    return getTimezone()


def _dayFirstPlayTimestamps(repo, username, tz) -> dict:
    """{"%Y-%m-%d" local date: earliest 15-minute bucket start that day}
    across the user's whole history - the same bucket->local-date mapping
    _getPlayDateSet/getListeningCalendar use, so streak dates recalculated
    from it agree with the streak features."""
    dayFirst: dict = {}
    for row in repo.getBucketedPlayTotals(username):
        dateStr = convertToDatetime(row["bucketStartTs"], tz=tz).strftime("%Y-%m-%d")
        if dateStr not in dayFirst or row["bucketStartTs"] < dayFirst[dateStr]:
            dayFirst[dateStr] = row["bucketStartTs"]
    return dayFirst


def computeStreakAchievedTimestamps(dayFirstTs, thresholds) -> dict:
    """{threshold: timestamp of the FIRST day ever that a consecutive-day run
    reached it}, from a {"%Y-%m-%d" local date: first play timestamp that day}
    map. Thresholds no run ever reached are absent. Runs grow one day at a
    time, so each threshold lands exactly on some run's threshold-th day; only
    the earliest occurrence is kept (seeding recorded only the then-current
    run, which may not have been the first to get there)."""
    wanted = set(thresholds)
    achieved: dict = {}
    if not wanted or not dayFirstTs:
        return achieved
    previousDay = None
    runLength = 0
    for day in sorted(datetime.date.fromisoformat(d) for d in dayFirstTs):
        runLength = runLength + 1 if previousDay is not None and (day - previousDay).days == 1 else 1
        previousDay = day
        if runLength in wanted and runLength not in achieved:
            achieved[runLength] = dayFirstTs[day.isoformat()]
    return achieved


def computeTopArtistTakeover(bucketRows) -> tuple | None:
    """(artistId, bucket timestamp of their LAST takeover) for the final
    all-time #1 in `bucketRows` (getBucketedArtistPlayCounts output, already
    bucket-ordered), or None with no plays. The lead only changes hands on a
    strictly greater play count - a tie never displaces the sitting leader -
    evaluated after each 15-minute bucket's increments, so the takeover
    moment is bucket-precise (plenty for a date display)."""
    counts: dict = {}
    leader = None
    leaderSince = None
    index = 0
    total = len(bucketRows)
    while index < total:
        bucketTs = bucketRows[index]["bucketStartTs"]
        while index < total and bucketRows[index]["bucketStartTs"] == bucketTs:
            row = bucketRows[index]
            counts[row["artistId"]] = counts.get(row["artistId"], 0) + row["plays"]
            index += 1
        challengers = [a for a, c in counts.items() if leader is None or c > counts[leader]]
        if challengers:
            newLeader = max(challengers, key=lambda a: (counts[a], a))
            if newLeader != leader:
                leader, leaderSince = newLeader, bucketTs
    if leader is None:
        return None
    return leader, leaderSince


def _topArtistTakeoverTs(repo, username, row) -> float | None:
    """The recalculated date for a top_artist row: when its artist last took
    the all-time #1 spot per the play data - or None (leave the row alone)
    when the detail is unreadable or the data's leader isn't that artist
    (tie ordering or same-name artist merges can make getTopArtists disagree
    with this id-based scan; no safe date to claim then)."""
    try:
        detail = json.loads(row["detail"]) if row.get("detail") else {}
    except (ValueError, TypeError):
        return None
    artistId = detail.get("id")
    if not artistId:
        return None
    takeover = computeTopArtistTakeover(repo.getBucketedArtistPlayCounts(username))
    if takeover is None or takeover[0] != artistId:
        return None
    return takeover[1]


# _recalculatedAchievedAt sentinel: this row must be left exactly as it is -
# distinct from None, which means "the current data does not support this
# milestone at all" (deletable under removeUnsupported).
_KEEP_ROW = object()


def _recalculatedAchievedAt(repo, username, row, streakTs, topArtistRowCount):
    """The data-derived achieved_at for one milestone row, None when the
    current play history can't support the milestone at all (fewer plays/
    hours than the threshold, no such streak run, or no plays whatsoever), or
    _KEEP_ROW when there's no safe answer and the row must stay untouched.

    top_artist is only re-dated for a user's SINGLE (seeded) row: with
    multiple rows (organic #1 changes), moving the latest one earlier could
    re-order getLatestMilestone below an older row, and the next detection
    pass would then re-record the current #1 as a fresh notification. A
    detail the id-based scan disagrees with is ambiguity (tie order,
    same-name artist merges), not lack of support - also _KEEP_ROW. Only a
    completely play-less history makes a top_artist row unsupported."""
    kind = row["kind"]
    if kind == MILESTONE_KIND_PLAYS:
        return repo.getNthPlayTimestamp(username, row["threshold"])
    if kind == MILESTONE_KIND_LISTEN_TIME:
        return repo.getListenTimeCrossingTimestamp(username, row["threshold"] * MS_PER_HOUR)
    if kind == MILESTONE_KIND_STREAK:
        return streakTs.get(row["threshold"])
    if kind == MILESTONE_KIND_TOP_ARTIST:
        if repo.getPlayTimeRange(username) is None:
            return None   #< no plays at all - nobody can be #1 of an empty history
        if topArtistRowCount != 1:
            return _KEEP_ROW
        takeoverTs = _topArtistTakeoverTs(repo, username, row)
        return takeoverTs if takeoverTs is not None else _KEEP_ROW
    return _KEEP_ROW   #< unknown kind: never touch


def recalculateMilestoneDates(repo, username, tz, removeUnsupported=False) -> int:
    """Rewrite `username`'s milestone achieved_at values to what the plays
    table says they really were, returning how many rows changed (dates
    rewritten + rows removed). The 1.34.0 seeding pass stamped every
    already-achieved milestone with the seeding moment itself; organically
    recorded rows also carry "when the background pass noticed" (up to a poll
    interval late, or import time for imported history) rather than the
    actual crossing - the data-derived date is the truthful one for every
    threshold kind, so all of them are recomputed.

    Rows the current data can't support at all (see _recalculatedAchievedAt)
    keep their existing date by default - but with `removeUnsupported=True`
    they are deleted instead. That mode belongs to the settled pass after an
    import (see _detectMilestonesSafely in app.py), where an overwrite import
    may have rewritten history to something smaller and the remaining rows
    would be claims the data visibly contradicts. Organic passes must keep
    the default: a tightened skip threshold also shrinks totals, and deleting
    for that would re-notify every affected milestone once it's re-crossed
    (or the threshold is loosened again). seen flags are never touched, so
    nothing re-notifies. Idempotent - a second run changes nothing."""
    rows = repo.getMilestonesForUser(username)
    if not rows:
        return 0

    streakThresholds = {r["threshold"] for r in rows if r["kind"] == MILESTONE_KIND_STREAK}
    streakTs = computeStreakAchievedTimestamps(
        _dayFirstPlayTimestamps(repo, username, tz), streakThresholds) if streakThresholds else {}
    topArtistRowCount = sum(1 for r in rows if r["kind"] == MILESTONE_KIND_TOP_ARTIST)

    changed = 0
    for row in rows:
        achievedAt = _recalculatedAchievedAt(repo, username, row, streakTs, topArtistRowCount)
        if achievedAt is _KEEP_ROW:
            continue
        if achievedAt is None:
            if removeUnsupported:
                repo.deleteMilestone(row["id"])
                changed += 1
            continue
        if achievedAt != row["achieved_at"]:
            repo.updateMilestoneAchievedAt(row["id"], achievedAt)
            changed += 1
    return changed


def nextMilestoneProgress(currentValue, thresholds) -> dict | None:
    """`{current, target, remaining, percent}` for the next not-yet-reached
    threshold above `currentValue`, or None once every threshold is reached.
    `thresholds` is ascending (see the MILESTONE_*_THRESHOLDS tuples); reaching
    a threshold exactly counts as done, so progress points at the one after.
    percent is an int 0..100 for a progress bar."""
    for threshold in thresholds:
        if currentValue < threshold:
            percent = int(currentValue / threshold * 100) if threshold else 0
            return {"current": currentValue, "target": threshold,
                    "remaining": threshold - currentValue, "percent": percent}
    return None


def buildNextMilestones(totalPlays, totalHours, streakDays) -> list:
    """The next play-count, listen-time, and streak milestone the user is
    working toward, for the dashboard's "Next milestones" panel. Each entry is
    `{kind, icon, label, current, target, remaining, percent}`; a kind whose
    every threshold is already reached is omitted. Same thresholds detection
    uses, so a bar filling to 100% lines up with the achievement being recorded
    (see detectMilestones)."""
    specs = (
        (MILESTONE_KIND_PLAYS, "🎧", "lifetime plays", totalPlays, MILESTONE_PLAYS_THRESHOLDS),
        (MILESTONE_KIND_LISTEN_TIME, "⏱️", "hours listened", totalHours, MILESTONE_LISTEN_HOURS_THRESHOLDS),
        (MILESTONE_KIND_STREAK, "🔥", "day streak", streakDays, MILESTONE_STREAK_DAY_THRESHOLDS),
    )
    out = []
    for kind, icon, label, value, thresholds in specs:
        progress = nextMilestoneProgress(value, thresholds)
        if progress is not None:
            out.append({"kind": kind, "icon": icon, "label": label, **progress})
    return out


def _formatNumber(value) -> str:
    return f"{int(value):,}"


def formatMilestone(row) -> dict:
    """Human-readable {icon, label, artistId?} for one user_milestones row, for
    the Profile Milestones section. artistId is present only on top_artist rows
    so the template can link to the artist page (None if the id is unknown)."""
    kind = row.get("kind")
    threshold = row.get("threshold") or 0
    if kind == MILESTONE_KIND_PLAYS:
        return {"icon": "🎧", "label": f"{_formatNumber(threshold)} lifetime plays"}
    if kind == MILESTONE_KIND_LISTEN_TIME:
        return {"icon": "⏱️", "label": f"{_formatNumber(threshold)} hours listened"}
    if kind == MILESTONE_KIND_STREAK:
        return {"icon": "🔥", "label": f"{_formatNumber(threshold)}-day listening streak"}
    if kind == MILESTONE_KIND_TOP_ARTIST:
        try:
            detail = json.loads(row["detail"]) if row.get("detail") else {}
        except (ValueError, TypeError):
            detail = {}
        name = detail.get("name") or "Unknown artist"
        return {"icon": "👑", "label": f"New #1 artist: {name}", "artistId": detail.get("id")}
    return {"icon": "⭐", "label": "Milestone reached"}
