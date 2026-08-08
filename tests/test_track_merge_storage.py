"""Storage-level guarantees for the track merge (tracks.canonical_id and
track_merge_decisions, added in 1.49.0).

Nothing reads canonical_id yet. These exist because the moment something does,
the ways it can be silently undone are already in place - and one of them has
happened here before, to the column right next to it.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest

CANONICAL_ID = "3xMBguKPth2j8YPuhmJHSO"
MERGED_ID = "6YcwCi4Guhw3TEfnSH9ROX"


class TestCanonicalIdSurvivesIngest(DatabaseTestCase):
    """A merge must outlive the next play of the track it merged.

    This is the isrc bug waiting to happen again. upsertTrack assigned
    `isrc=excluded.isrc` unconditionally while every live ingest path sends "",
    so a backfilled ISRC survived only until that track played again - hours,
    for anything in rotation - and the column measured 0 out of 24,850 rows on a
    real library without anyone noticing.

    canonical_id is safe today only because upsertTrack's column list happens
    not to mention it. That is one pattern-matched line away from being untrue,
    and the failure is silent and slow: merges quietly come apart as the merged
    tracks get played, which looks like the matcher never ran."""

    def _play(self, trackId, name="Song"):
        """A track arriving the way every live ingest path delivers one."""
        return normalizeTrackForTest({"id": trackId, "name": name, "artists": []})

    def test_a_merged_track_playing_again_stays_merged(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        db.repo.upsertTrack(self._play(CANONICAL_ID))
        db.repo.upsertTrack(self._play(MERGED_ID))
        db.repo.commit()
        with conn:
            conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?", (CANONICAL_ID, MERGED_ID))

        #< the merged track plays again, through a path that knows nothing about
        #  merging and supplies no canonical_id
        db.repo.upsertTrack(self._play(MERGED_ID))
        db.repo.commit()

        self.assertEqual(
            conn.execute("SELECT canonical_id FROM tracks WHERE id=?", (MERGED_ID,)).fetchone()[0],
            CANONICAL_ID)

    def test_a_canonical_track_playing_again_stays_canonical(self):
        """The other side of the same edge: the track being merged INTO is
        played at least as often, and must not acquire a canonical_id of its
        own - a row pointing at itself, or at anything, would make the merge a
        chain rather than a fact."""
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        db.repo.upsertTrack(self._play(CANONICAL_ID))
        db.repo.commit()

        db.repo.upsertTrack(self._play(CANONICAL_ID))
        db.repo.commit()

        self.assertIsNone(
            conn.execute("SELECT canonical_id FROM tracks WHERE id=?", (CANONICAL_ID,)).fetchone()[0])

    def test_a_new_track_starts_unmerged(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()

        db.repo.upsertTrack(self._play(CANONICAL_ID))
        db.repo.commit()

        self.assertIsNone(
            conn.execute("SELECT canonical_id FROM tracks WHERE id=?", (CANONICAL_ID,)).fetchone()[0])


class TestMergeDecisionsOutliveIngest(DatabaseTestCase):
    """The audit trail has to survive the same thing, for a sharper reason: a
    decision row is the only record of a MANUAL verdict, and losing one silently
    re-opens a question a person already answered."""

    def test_a_decision_survives_the_track_playing_again(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        db.repo.upsertTrack(self._playable(CANONICAL_ID))
        db.repo.upsertTrack(self._playable(MERGED_ID))
        db.repo.commit()
        with conn:
            conn.execute(
                "INSERT INTO track_merge_decisions "
                "(track_id, canonical_id, reason, evidence, decided_at, decided_by) "
                "VALUES (?, NULL, 'manual-split', 'different recordings', 1786000000.0, 'timorzipa')",
                (MERGED_ID,))

        db.repo.upsertTrack(self._playable(MERGED_ID))
        db.repo.commit()

        row = conn.execute(
            "SELECT reason, decided_by FROM track_merge_decisions WHERE track_id=?",
            (MERGED_ID,)).fetchone()
        self.assertEqual(row["reason"], "manual-split")
        self.assertEqual(row["decided_by"], "timorzipa")

    def _playable(self, trackId):
        return normalizeTrackForTest({"id": trackId, "name": "Song", "artists": []})


if __name__ == "__main__":
    unittest.main()
