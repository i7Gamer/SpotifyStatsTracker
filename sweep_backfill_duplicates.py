# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One-off sweep for historical pause-stretched backfill duplicates.

The live dedup layers (backfill announce, insert guard, reconciler) recognise a
Web API backfill row as a copy of a listener recording via the listener row's
created_at - the observed end of the play, pauses included, since the listener
inserts its row at the track-change moment (see
Database.BACKFILL_END_TIME_MATCH_TOLERANCE_SECONDS). Rows double-recorded
BEFORE that fix landed are out of the reconciler's reach - it only spans the
current API page's window - so this script applies the same pairing rule to the
whole plays table once:

    delete a web_api_backfill play when a same-user same-track LISTENER play
    exists whose created_at sits within the tolerance of the backfill row's
    played_at (both rows real plays, is_skip=0 - the reconciler's own filter).

A second pairing covers what that one structurally cannot reach. The Web API
reports no ms_played, so a backfill row stamps the track's whole duration and is
is_skip=0 by construction - meaning a listen the listener classified as a SKIP
matched no arm of the insert guard at all and came back as a full play
(2026-08-14: a 3.6s skip re-added as 220s, 15s away). Those rows pair
played_at-to-played_at, on a much tighter tolerance, since a skip's created_at is
when the user skipped away rather than an end the feed still owes us:

    delete a web_api_backfill play when a same-user same-track SKIP exists
    whose played_at sits within the skip tolerance of the backfill row's
    played_at (see Database.BACKFILL_SKIP_MATCH_TOLERANCE_SECONDS for why that
    stays tight - at 95s+ a genuine replay the listener missed is the likelier
    reading, and deleting one is unrecoverable).

Dry-run by default: prints what it would delete and exits. Pass --apply to
actually delete (one transaction, committed at the end).

Usage:
    python sweep_backfill_duplicates.py --db path/to/spotify_stats.db [--apply]
"""
import argparse
import datetime
import sqlite3
import sys

from Database.database import Database

#< how long a competing writer (the live app) is given before "database is
#  locked" aborts the sweep - the sweep's own work is milliseconds
BUSY_TIMEOUT_MS = 5000

#< how many ids one DELETE binds. Each is a bound parameter, and SQLite's
#  SQLITE_MAX_VARIABLE_NUMBER (32766 here) is a COMPILE-TIME maximum that
#  sqlite3_limit clamps to and cannot raise - so one statement over every
#  duplicate raises "too many SQL variables" on a library with enough of them,
#  after the report has already told the operator how many there are. Well
#  under the ceiling; a one-off sweep gains nothing from being close to it.
DELETE_CHUNK_SIZE = 500

#< plays.time_played is milliseconds; the skip report prints seconds
MS_PER_SECOND = 1000

#< matches the reconciler's pairing rule exactly (see workers/listener.py
#  _isSameListen): backfill played_at (Spotify's end-time reading) against the
#  listener row's created_at (its observed end). The created_reason filters are
#  what getPlaysWithSourceInRange's CASE and the mixed-sources rule enforce in
#  the live path: only listener rows anchor, only backfill rows are deleted.
#
#  One row per duplicate via ROW_NUMBER rather than GROUP BY: the reported
#  start and end are what the operator reads to decide whether --apply is safe,
#  and two independent MIN() aggregates took each end of that pair from
#  whichever listener row minimised it separately - so a copy pairing with
#  several listener rows was described by a start and an end that never
#  belonged to the same listen. The pick is the CLOSEST pairing (l.id breaks a
#  tie, so the report is stable across runs). Only the reported columns change;
#  WHERE decides which backfill rows are duplicates, and it is untouched.
_FIND_SQL = """
SELECT play_id, username, track_id, played_at, listener_played_at, listener_created_at
FROM (
    SELECT b.id AS play_id, b.username, b.track_id, b.played_at,
           l.played_at AS listener_played_at,
           l.created_at AS listener_created_at,
           ROW_NUMBER() OVER (PARTITION BY b.id
                              ORDER BY ABS(b.played_at - l.created_at), l.id) AS pairRank
    FROM plays b
    JOIN plays l
      ON l.username = b.username AND l.track_id = b.track_id
    WHERE b.created_reason LIKE 'web_api_backfill_play%'
      AND l.created_reason LIKE 'listener_play%'
      AND b.is_skip = 0 AND l.is_skip = 0
      AND l.created_at IS NOT NULL
      AND ABS(b.played_at - l.created_at) <= ?
)
WHERE pairRank = 1
ORDER BY username, played_at
"""


#< the insert guard's skip arm (see Database.BACKFILL_SKIP_MATCH_TOLERANCE_SECONDS)
#  applied to the whole table. played_at on BOTH sides: a skip's created_at is
#  when the user skipped away, so it anchors nothing. No created_reason filter
#  on the skip either - that filter exists on the end-time pairing because only
#  a listener row's created_at MEANS a play end, and played_at needs no such
#  vouching. Same ROW_NUMBER pick as above, so a copy sitting between two skips
#  is one deletion described by the closest pairing.
_FIND_SKIP_SQL = """
SELECT play_id, username, track_id, played_at, skip_played_at, skip_ms_played, skip_reason
FROM (
    SELECT b.id AS play_id, b.username, b.track_id, b.played_at,
           s.played_at AS skip_played_at,
           s.time_played AS skip_ms_played,
           s.created_reason AS skip_reason,
           ROW_NUMBER() OVER (PARTITION BY b.id
                              ORDER BY ABS(b.played_at - s.played_at), s.id) AS pairRank
    FROM plays b
    JOIN plays s
      ON s.username = b.username AND s.track_id = b.track_id
    WHERE b.created_reason LIKE 'web_api_backfill_play%'
      AND b.is_skip = 0 AND s.is_skip = 1
      AND ABS(b.played_at - s.played_at) <= ?
)
WHERE pairRank = 1
ORDER BY username, played_at
"""


def findBackfillEndTimeDuplicates(conn: sqlite3.Connection, toleranceSeconds: float) -> list[sqlite3.Row]:
    """All web_api_backfill plays provably double-recording a listener play,
    one row per duplicate (a copy pairing with several listener rows is still
    one deletion)."""
    return conn.execute(_FIND_SQL, (toleranceSeconds,)).fetchall()


def findBackfillSkipDuplicates(conn: sqlite3.Connection, toleranceSeconds: float) -> list[sqlite3.Row]:
    """All web_api_backfill plays re-recording a listen already captured as a
    skip, one row per duplicate. Reports the skip's own time_played beside it:
    that number is what tells the operator this was a 3-second sample rather
    than a listen the feed had reason to report."""
    return conn.execute(_FIND_SKIP_SQL, (toleranceSeconds,)).fetchall()


def deleteBackfillDuplicates(conn: sqlite3.Connection, playIds: list[int]) -> int:
    """Delete the given plays rows by id; returns how many rows went. The
    caller owns the transaction (commit/rollback), so a chunk that fails rolls
    the whole sweep back with it - the batching below is about SQLite's
    parameter ceiling (see DELETE_CHUNK_SIZE), not about partial progress."""
    deleted = 0
    for start in range(0, len(playIds), DELETE_CHUNK_SIZE):
        chunk = playIds[start:start + DELETE_CHUNK_SIZE]
        placeholders = ",".join("?" for _ in chunk)
        deleted += conn.execute(
            f"DELETE FROM plays WHERE id IN ({placeholders})", chunk).rowcount
    return deleted


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="path to spotify_stats.db")
    parser.add_argument("--apply", action="store_true",
                        help="actually delete (default: dry-run report only)")
    parser.add_argument("--tolerance", type=float,
                        default=Database.BACKFILL_END_TIME_MATCH_TOLERANCE_SECONDS,
                        help="max |backfill played_at - listener created_at| in seconds "
                             "(default: the live pairing tolerance, %(default)ss)")
    parser.add_argument("--skip-tolerance", type=float,
                        default=Database.BACKFILL_SKIP_MATCH_TOLERANCE_SECONDS,
                        help="max |backfill played_at - skip played_at| in seconds "
                             "(default: the live pairing tolerance, %(default)ss)")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    try:
        endDuplicates = findBackfillEndTimeDuplicates(conn, args.tolerance)
        skipDuplicates = findBackfillSkipDuplicates(conn, args.skip_tolerance)

        for row in endDuplicates:
            print(f"{row['username']}: track={row['track_id']} "
                  f"backfill played_at={_iso(row['played_at'])} "
                  f"listener start={_iso(row['listener_played_at'])} "
                  f"listener end={_iso(row['listener_created_at'])}")
        for row in skipDuplicates:
            print(f"{row['username']}: track={row['track_id']} "
                  f"backfill played_at={_iso(row['played_at'])} "
                  f"skip at={_iso(row['skip_played_at'])} "
                  f"skip played={(row['skip_ms_played'] or 0) / MS_PER_SECOND:.1f}s "
                  f"[{row['skip_reason']}]")

        # One row can satisfy both pairings (a copy near a real play's end AND
        # near a skip's start). Counting it twice would print a total the
        # operator cannot reconcile with the lines above it, and hand the same
        # id to DELETE twice.
        playIds: list[int] = []
        seen: set[int] = set()
        byUser: dict[str, int] = {}
        for row in (*endDuplicates, *skipDuplicates):
            if row["play_id"] in seen:
                continue
            seen.add(row["play_id"])
            playIds.append(row["play_id"])
            byUser[row["username"]] = byUser.get(row["username"], 0) + 1

        for username, count in sorted(byUser.items()):
            print(f"{username}: {count} duplicate(s)")
        print(f"total: {len(playIds)} duplicate backfill play(s)")

        if not args.apply:
            print("dry run - nothing deleted (pass --apply to delete)")
            return 0

        deleted = deleteBackfillDuplicates(conn, playIds)
        conn.commit()
        print(f"deleted: {deleted}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
