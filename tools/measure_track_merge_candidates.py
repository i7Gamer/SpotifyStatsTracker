# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2 of the track-merge work: does ISRC alone decide it?

Read-only. Point it at a COPY of the database (never the live file - see the
WAL note below) once the ISRC backfill has drained:

    python tools/measure_track_merge_candidates.py path/to/spotify_stats.db

It answers five questions, and the answers decide how much of the merge feature
needs building:

  1. What does ISRC find on its own?
  2. Where do ISRC and title-matching AGREE?      -> title matching confirmed
  3. Where do they DISAGREE?                      -> the measured error rate
  4. What does ISRC find that titles MISS?        -> what ISRC is worth
  5. What do titles find that ISRC MISSES?        -> what a second tier would add

Read (3) carefully in both directions. Identical ISRC means the same recording,
but the same recording can carry DIFFERENT ISRCs across labels, territories and
re-releases - so "different ISRCs" is evidence against a title match, not proof.
A disagreement is a case for review, not an automatic rejection.

The rule being measured is the one that was chosen deliberately: merge tracks
with the same title, primary artist and duration; never merge across a suffix
that marks a different recording (remaster, live, remix, acoustic...); fold
mono/stereo onto the base title. See TITLE_VARIANTS below.
"""
import collections
import re
import sqlite3
import sys
from pathlib import Path

#< the pair must agree within this to be the same recording at all. Same value
#  the earlier measurements used, so the numbers stay comparable.
DURATION_TOLERANCE_MS = 3000

# Suffixes that mark a DIFFERENT recording. Never merge across one of these -
# a remaster is a new master, a live take is a different performance.
DISTINGUISHING = re.compile(
    r"\b(remaster(ed)?|remix|live|acoustic|instrumental|demo|edit|extended|"
    r"anniversary|re-?recorded|taylor'?s version|single version|album version|"
    r"radio (edit|version)|session|unplugged|karaoke|cover|reprise|\d{4} version)\b", re.I)
# Suffixes that are the same recording presented differently.
NON_DISTINGUISHING = re.compile(r"\b(mono|stereo)\b", re.I)
TITLE_VARIANTS = re.compile(r"\s*[-–(\[]\s*([^)\]]*?)\s*[)\]]?\s*$")


def titleKey(name: str) -> str:
    """The title a merge groups by.

    A remaster and a mono variant are both just a suffix on the same base
    title, and they want opposite outcomes - so this classifies the suffix
    instead of stripping it. An UNRECOGNISED suffix stays part of the title,
    which is the safe default: it keeps a variant nobody has taught this about
    from being merged into the original."""
    name = (name or "").strip()
    match = TITLE_VARIANTS.search(name)
    if not match:
        return name.lower()
    tail = match.group(1)
    if NON_DISTINGUISHING.search(tail) and not DISTINGUISHING.search(tail):
        return (name[:match.start()].strip() or name).lower()
    return name.lower()


def openReadOnly(dbPath):
    """A connection that CANNOT write to, or create, the file it names.

    This tool documents itself as read-only and was not: a plain connect()
    creates a missing file, so a mistyped path left an empty database behind
    and the run then reported a confident zero of everything about it. mode=ro
    makes both failures loud - a missing file raises rather than appearing, and
    a stray write raises rather than landing on real listening history."""
    return sqlite3.connect(f"file:{Path(dbPath).resolve().as_posix()}?mode=ro", uri=True)


def load(dbPath):
    conn = openReadOnly(dbPath)
    conn.row_factory = sqlite3.Row
    tracks = conn.execute("""
        SELECT t.id, t.name, t.duration_ms, t.isrc,
               (SELECT artist_id FROM track_artists ta WHERE ta.track_id = t.id
                ORDER BY ta.position LIMIT 1) AS artist
        FROM tracks t
    """).fetchall()
    plays = dict(conn.execute("SELECT track_id, COUNT(*) FROM plays GROUP BY track_id"))
    conn.close()
    return tracks, plays


def groupsBy(keyOf, tracks, requireDuration=True):
    """{key: [track, ...]} for keys with more than one track, optionally
    requiring the group to agree on duration."""
    buckets = collections.defaultdict(list)
    for track in tracks:
        key = keyOf(track)
        if key is not None:
            buckets[key].append(track)
    grouped = {}
    for key, items in buckets.items():
        if len(items) < 2:
            continue
        if requireDuration:
            # `or 0` used to fold a NULL into a zero-length track, so one
            # unstamped release dragged the spread past the tolerance and the
            # WHOLE group went - every valid member with it. An unknown
            # duration is not a disagreement; it is an absence of evidence, so
            # it abstains. But a group of NOTHING but unknowns proves nothing
            # either, and this tool exists to measure the rule rather than to
            # be generous about it - so those are still dropped.
            durations = [t["duration_ms"] for t in items
                         if t["duration_ms"] is not None and t["duration_ms"] > 0]
            if len(durations) < 2:
                continue
            if max(durations) - min(durations) > DURATION_TOLERANCE_MS:
                continue
        grouped[key] = items
    return grouped


def pairsOf(groups):
    """Every unordered pair of ids inside each group.

    Comparing whole GROUPS gets questions 4 and 5 wrong whenever the two methods
    carve the same tracks differently. A three-track title group that ISRC sees
    as one pair plus a singleton would land in both "ISRC finds, titles miss"
    (the pair is not a group titles produced) and "titles find, ISRC misses"
    (the triple is not a group ISRC produced) - two findings out of one
    agreement, in the numbers deciding whether a second matcher gets built.

    A pair is the unit the two methods can actually agree or disagree on: "are
    these two the same recording?" is the whole question."""
    pairs = set()
    for items in groups.values():
        ids = sorted(track["id"] for track in items)
        for index, first in enumerate(ids):
            for second in ids[index + 1:]:
                pairs.add((first, second))
    return pairs


def playsMoved(groups, plays):
    """Plays that would move onto a canonical track - everything but the
    largest member of each group, which is the one they merge into."""
    total = 0
    for items in groups.values():
        counts = sorted((plays.get(t["id"], 0) for t in items), reverse=True)
        total += sum(counts[1:])
    return total


def main(dbPath):
    tracks, plays = load(dbPath)
    withIsrc = [t for t in tracks if (t["isrc"] or "").strip()]
    coverage = 100 * len(withIsrc) / len(tracks) if tracks else 0
    print(f"catalog: {len(tracks)} tracks, {len(withIsrc)} with an ISRC ({coverage:.1f}%)")
    if coverage < 95:
        print("  !! the backfill has not drained - every number below is a floor, not an answer")
    print()

    isrcGroups = groupsBy(
        lambda t: (t["isrc"] or "").strip() or None, tracks, requireDuration=False)
    titleGroups = groupsBy(
        lambda t: (titleKey(t["name"]), t["artist"]) if t["artist"] else None, tracks)

    print(f"1. ISRC alone           {len(isrcGroups):>4} groups, "
          f"{sum(len(v) - 1 for v in isrcGroups.values()):>4} ids folded, "
          f"{playsMoved(isrcGroups, plays):>5} plays moved")
    print(f"   title+artist+length  {len(titleGroups):>4} groups, "
          f"{sum(len(v) - 1 for v in titleGroups.values()):>4} ids folded, "
          f"{playsMoved(titleGroups, plays):>5} plays moved")
    print()

    #< only groups whose members ALL carry an ISRC can be judged; the rest are
    #  unknown rather than disagreements, and counting them as either is how a
    #  half-drained catalog produces a confident wrong answer
    agree = disagree = unjudgeable = 0
    disagreements = []
    for items in titleGroups.values():
        isrcs = {(t["isrc"] or "").strip() for t in items}
        if "" in isrcs:
            unjudgeable += 1
        elif len(isrcs) == 1:
            agree += 1
        else:
            disagree += 1
            disagreements.append(items)

    print(f"2. ISRC agrees with the title match     {agree:>4}")
    print(f"3. ISRC disagrees                       {disagree:>4}"
          f"   <- review these, do not merge them blind")
    print(f"   not yet judgeable (missing ISRCs)    {unjudgeable:>4}")
    print()

    titlePairs = pairsOf(titleGroups)
    isrcPairs = pairsOf(isrcGroups)
    onlyIsrc = isrcPairs - titlePairs
    onlyTitle = titlePairs - isrcPairs
    print(f"4. ISRC finds, titles miss              {len(onlyIsrc):>4} pairs"
          f"   <- what ISRC is worth over title matching")
    print(f"5. titles find, ISRC misses             {len(onlyTitle):>4} pairs"
          f"   <- what a second tier would still add")
    print()

    if disagreements:
        print("A few disagreements, for eyeballing:")
        for items in sorted(disagreements, key=lambda g: -sum(plays.get(t["id"], 0) for t in g))[:8]:
            names = " | ".join(f'{t["name"]!r} ({plays.get(t["id"], 0)}p)' for t in items)
            print(f"  {names}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
