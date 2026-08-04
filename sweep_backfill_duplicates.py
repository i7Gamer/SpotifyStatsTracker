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

#< matches the reconciler's pairing rule exactly (see workers/listener.py
#  _isSameListen): backfill played_at (Spotify's end-time reading) against the
#  listener row's created_at (its observed end). The created_reason filters are
#  what getPlaysWithSourceInRange's CASE and the mixed-sources rule enforce in
#  the live path: only listener rows anchor, only backfill rows are deleted.
_FIND_SQL = """
SELECT b.id AS play_id, b.username, b.track_id, b.played_at,
       MIN(l.played_at) AS listener_played_at,
       MIN(l.created_at) AS listener_created_at
FROM plays b
JOIN plays l
  ON l.username = b.username AND l.track_id = b.track_id
WHERE b.created_reason LIKE 'web_api_backfill_play%'
  AND l.created_reason LIKE 'listener_play%'
  AND b.is_skip = 0 AND l.is_skip = 0
  AND l.created_at IS NOT NULL
  AND ABS(b.played_at - l.created_at) <= ?
GROUP BY b.id
ORDER BY b.username, b.played_at
"""


def findBackfillEndTimeDuplicates(conn: sqlite3.Connection, toleranceSeconds: float) -> list[sqlite3.Row]:
    """All web_api_backfill plays provably double-recording a listener play,
    one row per duplicate (a copy pairing with several listener rows is still
    one deletion)."""
    return conn.execute(_FIND_SQL, (toleranceSeconds,)).fetchall()


def deleteBackfillDuplicates(conn: sqlite3.Connection, playIds: list[int]) -> int:
    """Delete the given plays rows by id; returns how many rows went. The
    caller owns the transaction (commit/rollback)."""
    if not playIds:
        return 0
    placeholders = ",".join("?" for _ in playIds)
    cur = conn.execute(f"DELETE FROM plays WHERE id IN ({placeholders})", playIds)
    return cur.rowcount


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
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    try:
        duplicates = findBackfillEndTimeDuplicates(conn, args.tolerance)

        byUser: dict[str, int] = {}
        for row in duplicates:
            byUser[row["username"]] = byUser.get(row["username"], 0) + 1
            print(f"{row['username']}: track={row['track_id']} "
                  f"backfill played_at={_iso(row['played_at'])} "
                  f"listener start={_iso(row['listener_played_at'])} "
                  f"listener end={_iso(row['listener_created_at'])}")

        for username, count in sorted(byUser.items()):
            print(f"{username}: {count} duplicate(s)")
        print(f"total: {len(duplicates)} duplicate backfill play(s)")

        if not args.apply:
            print("dry run - nothing deleted (pass --apply to delete)")
            return 0

        deleted = deleteBackfillDuplicates(conn, [row["play_id"] for row in duplicates])
        conn.commit()
        print(f"deleted: {deleted}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
