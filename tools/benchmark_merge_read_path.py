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
import tempfile
import time
from pathlib import Path

#< beside this file, not an absolute path: the first committed version carried
#  the author's own checkout location and ran nowhere else
sys.path.insert(0, str(Path(__file__).resolve().parent))
from measure_track_merge_candidates import groupsBy, titleKey, load  # noqa: E402

REPEATS = 3
#< what the verdict column prints when the baseline timed at exactly zero. Not
#  reachable on a real clock, but it is the last line of the run and a
#  ZeroDivisionError there would throw away every measurement above it.
SPEEDUP_UNAVAILABLE = "n/a"
#< the synthetic rows the write-cost section inserts, kept far past any real
#  played_at so the cleanup DELETE can name them exactly
SYNTHETIC_PLAYED_AT_BASE = 2e9
SYNTHETIC_PLAY_COUNT = 2000


def speedup(baselineMs, variantMs):
    """"2.4x" - or SPEEDUP_UNAVAILABLE rather than a ZeroDivisionError."""
    if not baselineMs:
        return SPEEDUP_UNAVAILABLE
    return f"{variantMs / baselineMs:.1f}x"


def workingCopy(dbPath, intoDir):
    """A throwaway FILE copy of the database, which is the only thing the rest
    of this script is allowed to touch.

    simulateMerge below nulls every canonical_id and rewrites the column from a
    title rule, adds a column to `plays`, and inserts SYNTHETIC_PLAY_COUNT
    rows. Against the path someone actually types that is not a measurement, it
    is an unrequested merge on real listening history - and in this repo the
    dev checkout's Database/Data IS the live data. A Ctrl+C used to leave the
    synthetic plays behind too.

    A file rather than :memory:. The whole point of this benchmark is which
    query plan reads a disk-backed database faster; an in-memory copy has no
    page cache to miss and would measure something else entirely. backup()
    rather than a file copy: it takes a consistent snapshot including anything
    still sitting in the WAL."""
    source = sqlite3.connect(f"file:{Path(dbPath).resolve().as_posix()}?mode=ro", uri=True)
    try:
        copyPath = Path(intoDir) / "benchmark-copy.db"
        destination = sqlite3.connect(copyPath)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return copyPath


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


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(f"usage: {Path(__file__).name} path/to/spotify_stats.db", file=sys.stderr)
        return 2
    try:
        #< every mutation below lands here, never on what the operator named
        with tempfile.TemporaryDirectory(prefix="merge-benchmark-") as workDir:
            return _run(workingCopy(argv[0], workDir))
    except sqlite3.OperationalError as e:
        print(f"could not open {argv[0]}: {e}", file=sys.stderr)
        return 1


def _run(DB):
    merged = simulateMerge(DB)
    conn = sqlite3.connect(DB)
    try:
        busiest = conn.execute("SELECT username FROM plays GROUP BY username "
                               "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        if busiest is None:
            print("no plays in this database - nothing to measure", file=sys.stderr)
            return 1
        user = busiest[0]
        plays = conn.execute("SELECT COUNT(*) FROM plays WHERE username=?", (user,)).fetchone()[0]
    finally:
        conn.close()
    yearAgo = time.time() - 365 * 24 * 3600
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
        verdict = f"join {speedup(ta, tb)}, denorm {speedup(ta, tc)}"
        print(f"  {label:34} {ta:9.1f}ms {tb:9.1f}ms {tc:9.1f}ms   {verdict}")

    # C's write cost is the per-play INSERT, not a recomputeSkipFlags-shaped
    # rewrite. The first version of this measured the latter - and measured
    # nothing, because `UPDATE plays SET is_skip = is_skip` touches neither the
    # new column nor its index, so SQLite maintains neither.
    conn = sqlite3.connect(DB)
    try:
        trackId = conn.execute("SELECT track_id FROM plays LIMIT 1").fetchone()[0]
    finally:
        conn.close()   #< the ad-hoc connection here used to be left open
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
        try:
            with conn:
                for offset in range(SYNTHETIC_PLAY_COUNT):
                    row = (user, trackId, SYNTHETIC_PLAYED_AT_BASE + offset) + ((trackId,) if withColumn else ())
                    conn.execute(f"INSERT OR IGNORE INTO plays ({columns}) VALUES ({values})", row)
            print(f"  {SYNTHETIC_PLAY_COUNT} inserts {label:30} "
                  f"{(time.perf_counter() - start) * 1000:7.0f}ms")
        finally:
            #< in a finally so a Ctrl+C mid-run doesn't leave them behind. The
            #  copy is thrown away either way now, but this section is the one
            #  a future reader is most likely to lift out of here.
            with conn:
                conn.execute("DELETE FROM plays WHERE played_at >= ? AND played_at < ?",
                             (SYNTHETIC_PLAYED_AT_BASE,
                              SYNTHETIC_PLAYED_AT_BASE + SYNTHETIC_PLAY_COUNT))
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
