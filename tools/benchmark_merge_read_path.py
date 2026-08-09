# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 5's open question, measured: what does grouping by canonical track cost?

Three designs for the same answer, on a COPY of the live database with a
realistic merge simulated from the title rule (the same 457 groups / 484
redundant ids the measurement tool finds - real distribution, real play counts):

  A  current            GROUP BY track_id, plays scanned alone
  B  join               JOIN tracks, GROUP BY COALESCE(t.canonical_id, t.id)
  C  denormalised       GROUP BY p.canonical_track_id, plays still alone

B is the obvious implementation. C costs a column on plays that the merge has to
maintain. The eleven _trackSetClause call sites exist specifically to keep these
scans off the tracks join, so B is the one that has to be proven, not assumed -
this repo has measured and rejected three plausible indexes on this table.
"""
import sqlite3
import sys
import time
from pathlib import Path

#< beside this file, not an absolute path: the first committed version carried
#  the author's own checkout location and ran nowhere else
sys.path.insert(0, str(Path(__file__).resolve().parent))
from measure_track_merge_candidates import groupsBy, titleKey, load  # noqa: E402

DB = sys.argv[1]
REPEATS = 3


def simulateMerge(dbPath):
    """Populate canonical_id (and a denormalised plays column) from the title
    rule, so the read path is measured against a merge of realistic shape."""
    tracks, _ = load(dbPath)
    groups = groupsBy(lambda t: (titleKey(t["name"]), t["artist"]) if t["artist"] else None, tracks)
    conn = sqlite3.connect(dbPath)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
    with conn:
        if "canonical_id" not in columns:
            conn.execute("ALTER TABLE tracks ADD COLUMN canonical_id TEXT REFERENCES tracks(id)")
        conn.execute("UPDATE tracks SET canonical_id = NULL")
        merged = 0
        for items in groups.values():
            #< the most-played member wins, which is what a real matcher would do
            ids = [t["id"] for t in items]
            keep, rest = ids[0], ids[1:]
            for trackId in rest:
                conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?", (keep, trackId))
                merged += 1
        # Design C's denormalised column, maintained by the merge.
        playColumns = {r[1] for r in conn.execute("PRAGMA table_info(plays)")}
        if "canonical_track_id" not in playColumns:
            conn.execute("ALTER TABLE plays ADD COLUMN canonical_track_id TEXT")
        conn.execute("""UPDATE plays SET canonical_track_id = COALESCE(
                          (SELECT t.canonical_id FROM tracks t WHERE t.id = plays.track_id), track_id)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_user_canonical "
                     "ON plays(username, canonical_track_id)")
    conn.close()
    return merged


def timed(dbPath, sql, params):
    """Best-of-N on ONE warm connection.

    The first version opened a fresh connection per variant and ran them A, B,
    C - so A paid the cold page cache for all three and the join looked 2.4x
    FASTER than doing less work, which is not a thing. Warmed and rotated now:
    the caller runs the trio in both directions and keeps the best of each,
    so no variant can win on cache position."""
    conn = sqlite3.connect(dbPath)
    conn.execute(sql, params).fetchall()          #< warm
    best = None
    for _ in range(REPEATS):
        start = time.perf_counter()
        conn.execute(sql, params).fetchall()
        elapsed = (time.perf_counter() - start) * 1000
        best = elapsed if best is None else min(best, elapsed)
    conn.close()
    return best


def main():
    merged = simulateMerge(DB)
    conn = sqlite3.connect(DB)
    user = conn.execute("SELECT username FROM plays GROUP BY username "
                        "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    plays = conn.execute("SELECT COUNT(*) FROM plays WHERE username=?", (user,)).fetchone()[0]
    yearAgo = time.time() - 365 * 24 * 3600
    conn.close()
    print(f"  {plays} plays for {user}; {merged} tracks merged into a canonical\n")

    cases = {
        "track aggregates, all time": (
            "SELECT track_id, COUNT(*) c, SUM(time_played) t FROM plays "
            "WHERE username=? AND is_skip=0 GROUP BY track_id",
            "SELECT COALESCE(tr.canonical_id, p.track_id) k, COUNT(*) c, SUM(p.time_played) t "
            "FROM plays p JOIN tracks tr ON tr.id=p.track_id "
            "WHERE p.username=? AND p.is_skip=0 GROUP BY k",
            "SELECT canonical_track_id k, COUNT(*) c, SUM(time_played) t FROM plays "
            "WHERE username=? AND is_skip=0 GROUP BY k",
            (user,)),
        "distinct songs count, all time": (
            "SELECT COUNT(*) FROM (SELECT track_id FROM plays WHERE username=? AND is_skip=0 GROUP BY track_id)",
            "SELECT COUNT(*) FROM (SELECT COALESCE(tr.canonical_id, p.track_id) k FROM plays p "
            "JOIN tracks tr ON tr.id=p.track_id WHERE p.username=? AND p.is_skip=0 GROUP BY k)",
            "SELECT COUNT(*) FROM (SELECT canonical_track_id k FROM plays WHERE username=? AND is_skip=0 GROUP BY k)",
            (user,)),
        "top songs, last year": (
            "SELECT track_id, COUNT(*) c FROM plays WHERE username=? AND is_skip=0 AND played_at>? "
            "GROUP BY track_id ORDER BY c DESC LIMIT 50",
            "SELECT COALESCE(tr.canonical_id, p.track_id) k, COUNT(*) c FROM plays p "
            "JOIN tracks tr ON tr.id=p.track_id WHERE p.username=? AND p.is_skip=0 AND p.played_at>? "
            "GROUP BY k ORDER BY c DESC LIMIT 50",
            "SELECT canonical_track_id k, COUNT(*) c FROM plays WHERE username=? AND is_skip=0 AND played_at>? "
            "GROUP BY k ORDER BY c DESC LIMIT 50",
            (user, yearAgo)),
        "per-artist scan (the 1.2s one)": (
            "SELECT p.track_id, COUNT(*) c FROM plays p WHERE p.username=? AND p.is_skip=0 "
            "AND p.track_id IN (SELECT track_id FROM track_artists) GROUP BY p.track_id",
            "SELECT COALESCE(tr.canonical_id, p.track_id) k, COUNT(*) c FROM plays p "
            "JOIN tracks tr ON tr.id=p.track_id WHERE p.username=? AND p.is_skip=0 "
            "AND p.track_id IN (SELECT track_id FROM track_artists) GROUP BY k",
            "SELECT p.canonical_track_id k, COUNT(*) c FROM plays p WHERE p.username=? AND p.is_skip=0 "
            "AND p.track_id IN (SELECT track_id FROM track_artists) GROUP BY k",
            (user,)),
    }

    print(f"  {'query':34} {'A current':>11} {'B join':>11} {'C denorm':>11}   verdict")
    for label, (a, b, c, params) in cases.items():
        #< forwards and backwards, keeping the best of each: order must not decide
        forward = [timed(DB, sql, params) for sql in (a, b, c)]
        backward = list(reversed([timed(DB, sql, params) for sql in (c, b, a)]))
        ta, tb, tc = (min(f, b_) for f, b_ in zip(forward, backward))
        verdict = f"join {tb / ta:.1f}x, denorm {tc / ta:.1f}x"
        print(f"  {label:34} {ta:9.1f}ms {tb:9.1f}ms {tc:9.1f}ms   {verdict}")

    # C's write cost is the per-play INSERT, not a recomputeSkipFlags-shaped
    # rewrite. The first version of this measured the latter - and measured
    # nothing, because `UPDATE plays SET is_skip = is_skip` touches neither the
    # new column nor its index, so SQLite maintains neither.
    trackId = sqlite3.connect(DB).execute("SELECT track_id FROM plays LIMIT 1").fetchone()[0]
    print()
    for label, withColumn in (("without C's column + index", False), ("with C's column + index", True)):
        conn = sqlite3.connect(DB)
        with conn:
            if withColumn:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_user_canonical "
                             "ON plays(username, canonical_track_id)")
            else:
                conn.execute("DROP INDEX IF EXISTS idx_plays_user_canonical")
        columns = "username, track_id, played_at, time_played, is_skip"
        values = "?, ?, ?, 1000, 0"
        if withColumn:
            columns += ", canonical_track_id"
            values += ", ?"
        start = time.perf_counter()
        with conn:
            for offset in range(2000):
                row = (user, trackId, 2e9 + offset) + ((trackId,) if withColumn else ())
                conn.execute(f"INSERT OR IGNORE INTO plays ({columns}) VALUES ({values})", row)
        print(f"  2000 inserts {label:30} {(time.perf_counter() - start) * 1000:7.0f}ms")
        with conn:
            conn.execute("DELETE FROM plays WHERE played_at >= 2e9 AND played_at < 2e9 + 2000")
        conn.close()


if __name__ == "__main__":
    main()
