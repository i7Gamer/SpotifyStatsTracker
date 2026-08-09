"""Do the affected Wrapped years recalculate CORRECTLY after merges and splits?

Invalidation alone was already pinned (test_track_merge_toggle). These cover
the other two halves of the question: the rebuilt snapshot carries merged
numbers, and nothing can sneak a stale snapshot back into the cache after the
invalidation ran - which a torn in-flight recalculation otherwise could,
permanently, because a merge changes neither of the freshness signals the
worker compares.
"""
import json
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase

SINGLE = "A" * 22
ALBUM_CUT = "B" * 22
ISRC = "USRC12345678"


class WrappedMergeTestCase(DatabaseTestCase):
    def _dbWithPair(self):
        """The same recording twice, all plays in the current year so a Wrapped
        year exists to rebuild."""
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        nowTs = time.time()
        with conn:
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES ('alb', 'A', '')")
            for trackId, plays in ((SINGLE, 3), (ALBUM_CUT, 9)):
                conn.execute("INSERT INTO tracks (id, name, url, album_id, isrc, duration_ms) "
                             "VALUES (?, 'Shared Song', '', 'alb', ?, 200000)", (trackId, ISRC))
                for i in range(plays):
                    conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                                 "VALUES (?, ?, ?, 200000)",
                                 (db.user, trackId, nowTs - 3600 * (i + 1)))
        return db

    def _cachedTopSongs(self, db, year):
        row = db.repo.getCachedWrapped(db.user, year)
        return json.loads(row["top_songs"]) if row else None


class TestTheRebuildIsMerged(WrappedMergeTestCase):
    def test_after_a_merge_the_year_rebuilds_with_one_song(self):
        import datetime
        db = self._dbWithPair()
        year = datetime.datetime.now(tz=db.tz).year
        db.recalculateWrappedForYear(year)   #< the pre-merge snapshot exists
        self.assertEqual(len(self._cachedTopSongs(db, year)), 2)

        db.repo.mergeTracksByIsrc()          #< invalidates every cached year
        self.assertIsNone(self._cachedTopSongs(db, year))
        db.recalculateWrappedForYear(year)   #< what the worker / the view does next

        topSongs = self._cachedTopSongs(db, year)
        self.assertEqual(len(topSongs), 1)
        self.assertEqual(topSongs[0]["id"], ALBUM_CUT)
        self.assertEqual(topSongs[0]["plays"], 12)

    def test_after_a_split_the_year_rebuilds_apart_again(self):
        import datetime
        db = self._dbWithPair()
        year = datetime.datetime.now(tz=db.tz).year
        db.repo.mergeTracksByIsrc()
        db.recalculateWrappedForYear(year)
        self.assertEqual(len(self._cachedTopSongs(db, year)), 1)

        db.repo.unmergeTrack(SINGLE, decidedBy="timorzipa")
        self.assertIsNone(self._cachedTopSongs(db, year))
        db.recalculateWrappedForYear(year)

        topSongs = self._cachedTopSongs(db, year)
        self.assertEqual(len(topSongs), 2)
        self.assertEqual({s["plays"] for s in topSongs}, {3, 9})


class TestATornSnapshotCannotOutliveTheInvalidation(WrappedMergeTestCase):
    """The race: a recalculation is seconds of queries under a per-year lock
    the merge does not take. A merge committing mid-computation deletes the
    caches - and the in-flight computation then SAVES, after the delete, a
    snapshot whose reads straddle the merge. Its max_played_at and play count
    match current values (a merge changes neither), so the staleness check
    never flags it again: a permanently stale past year.

    Closed with a generation stamp checked inside the save's own transaction,
    so the write serializes against the merge: whichever commits second sees
    the other."""

    def test_a_merge_during_the_recalculation_discards_the_save(self):
        import datetime
        db = self._dbWithPair()
        year = datetime.datetime.now(tz=db.tz).year

        realTopSongs = type(db).getTopSongs

        def mergeMidway(self_, *args, **kwargs):
            #< the merge lands while the year is being computed
            db.repo.mergeTracksByIsrc()
            return realTopSongs(self_, *args, **kwargs)

        with patch.object(type(db), "getTopSongs", mergeMidway):
            db.recalculateWrappedForYear(year)

        #< the torn snapshot must NOT have been cached; the next (clean) pass
        #  rebuilds it merged
        self.assertIsNone(db.repo.getCachedWrapped(db.user, year))
        db.recalculateWrappedForYear(year)
        self.assertEqual(len(json.loads(
            db.repo.getCachedWrapped(db.user, year)["top_songs"])), 1)

    def test_a_clean_recalculation_still_saves(self):
        """The guard must not eat the ordinary case."""
        import datetime
        db = self._dbWithPair()
        year = datetime.datetime.now(tz=db.tz).year

        db.recalculateWrappedForYear(year)

        self.assertIsNotNone(db.repo.getCachedWrapped(db.user, year))

    def test_the_stamp_moves_on_every_invalidation(self):
        db = self._dbWithPair()
        before = db.repo.getWrappedInvalidationGeneration()

        db.repo.deleteAllWrapped()

        self.assertGreater(db.repo.getWrappedInvalidationGeneration(), before)


if __name__ == "__main__":
    unittest.main()
