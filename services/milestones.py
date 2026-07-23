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
import json
import logging
import time

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

_MS_PER_HOUR = 1000 * 60 * 60


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


def detectMilestones(db, repo, username, changeCache=None) -> int:
    """Detect and record any newly-reached milestones for `username`, returning
    how many rows were recorded this pass.

    On the user's first pass (no milestones_baseline_at yet) everything already
    achieved is recorded as seen and the baseline is stamped; afterwards new
    crossings are recorded unseen (seen=0) so the topbar badge surfaces them.

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
    seen = seed

    totalPlays, totalMs = db.getPlayTotals(None, None)
    # Idle-cycle short-circuit (see docstring). Never on the seeding pass - that
    # must record the already-achieved backlog once, even against a stale cache.
    if changeCache is not None and not seed and changeCache.get(username) == (totalPlays, totalMs):
        return 0

    totalHours = (totalMs or 0) // _MS_PER_HOUR
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
