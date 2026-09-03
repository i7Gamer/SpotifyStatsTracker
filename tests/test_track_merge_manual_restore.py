# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Turning the merge toggle off restores a manual merge the matcher re-homed.

unmergeAllIsrcMerges promises "undo what the matcher did, not forget what
anyone decided", and the toggle calls itself a lossless undo. Both were false
for one shape: the matcher's carry-along re-points every dependent of a member
and rewrote its audit row with no decided_by filter, so a hand-made verdict
came out pointing at the matcher's head - and because track_id is the audit
table's PRIMARY KEY, the release the admin actually chose was overwritten in
place and gone. The surviving row read "A -> C, manual-merge, decided_by=admin":
a decision attributed to someone who never made it.

track_merge_decisions.carried_canonical_id records where the carry-along MOVED
a manual verdict, leaving canonical_id saying where the person PUT it. The
revert re-points those back.

The clearing rule is load-bearing rather than tidiness: a stale carried value
would restore a track to a target the person has since moved away from, and
with two manual merges in play that builds a two-hop chain no reader resolves.
So every manual write path clears it - see TestAReDecisionDropsTheCarry.

2026-09-03 review, M2.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase

ISRC_A = "USRC12345678"
REMASTER = "A" * 22   #< the old head: plays elected it, the title rule demotes it
PLAIN = "B" * 22      #< the release the matcher promotes
HAND = "D" * 22       #< carries no ISRC: hand-merged onto REMASTER, invisible to the planner
OTHER = "E" * 22


class ManualRestoreTestCase(DatabaseTestCase):
    def _db(self):
        return self._makeDb({}, [])

    def _track(self, db, trackId, isrc=None, name="Song", createdAt=1000.0, plays=0):
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES ('alb1', 'A', '')")
            conn.execute("INSERT OR IGNORE INTO users (username, created_at) VALUES ('alice', 0)")
            conn.execute("INSERT INTO tracks (id, name, url, album_id, isrc, created_at) "
                         "VALUES (?, ?, '', 'alb1', ?, ?)", (trackId, name, isrc, createdAt))
            for i in range(plays):
                conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                             "VALUES ('alice', ?, ?, 200000)", (trackId, 1e9 + i))
        return trackId

    def _mergedUnderThePlaysOnlyRule(self, db, headId, *memberIds):
        """A group as an earlier run left it: plays elected the head."""
        conn = db.repo._conn()
        with conn:
            for memberId in memberIds:
                conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?", (headId, memberId))
                conn.execute(
                    "INSERT INTO track_merge_decisions (track_id, canonical_id, reason,"
                    " evidence, decided_at, decided_by) VALUES (?, ?, 'isrc', ?, 1000.0, NULL)",
                    (memberId, headId, ISRC_A))

    def _canonical(self, db, trackId):
        return db.repo._conn().execute(
            "SELECT canonical_id FROM tracks WHERE id=?", (trackId,)).fetchone()[0]

    def _decision(self, db, trackId):
        row = db.repo._conn().execute(
            "SELECT canonical_id, carried_canonical_id, reason, decided_by "
            "FROM track_merge_decisions WHERE track_id=?", (trackId,)).fetchone()
        return dict(row) if row else None

    def _assertNoChains(self, db):
        for (trackId,) in db.repo._conn().execute(
                "SELECT id FROM tracks WHERE canonical_id IS NOT NULL"):
            target = self._canonical(db, trackId)
            self.assertIsNone(self._canonical(db, target),
                              f"{trackId} points at {target}, which points on again")

    def _reHomedHandMerge(self, db):
        """The setup this whole file is about: a hand-merged track riding along
        when the title rule moves its group's head."""
        self._track(db, REMASTER, isrc=ISRC_A, name="Song - 2005 Remaster", plays=70)
        self._track(db, PLAIN, isrc=ISRC_A, name="Song", plays=13)
        self._track(db, HAND, name="Song - Mono", plays=1)
        self._mergedUnderThePlaysOnlyRule(db, REMASTER, PLAIN)
        db.repo.mergeTrackManually(HAND, REMASTER, decidedBy="timorzipa")
        db.repo.mergeTracksByIsrc()


class TestTheCarryIsRecordedNotOverwritten(ManualRestoreTestCase):
    def test_the_audit_row_still_names_the_release_the_person_chose(self):
        db = self._db()

        self._reHomedHandMerge(db)

        decision = self._decision(db, HAND)
        self.assertEqual(decision["canonical_id"], REMASTER)     #< where a person put it
        self.assertEqual(decision["carried_canonical_id"], PLAIN)  #< where the matcher moved it
        self.assertEqual(decision["reason"], "manual-merge")
        self.assertEqual(decision["decided_by"], "timorzipa")

    def test_the_pointer_itself_still_moves(self):
        """The no-chain invariant is not negotiable: readers resolve exactly
        one hop, so the track must point at the group's live head even while
        the audit row remembers somewhere else."""
        db = self._db()

        self._reHomedHandMerge(db)

        self.assertEqual(self._canonical(db, HAND), PLAIN)
        self._assertNoChains(db)

    def test_a_matcher_made_dependent_carries_nothing(self):
        """Negative control: only a person's verdict has anything to restore."""
        db = self._db()
        self._track(db, REMASTER, isrc=ISRC_A, name="Song - 2005 Remaster", plays=70)
        self._track(db, PLAIN, isrc=ISRC_A, name="Song", plays=13)
        self._track(db, OTHER, isrc=ISRC_A, name="Song (Deluxe Edition)", plays=5)
        self._mergedUnderThePlaysOnlyRule(db, REMASTER, PLAIN, OTHER)

        db.repo.mergeTracksByIsrc()

        decision = self._decision(db, OTHER)
        self.assertEqual(decision["canonical_id"], PLAIN)
        self.assertIsNone(decision["carried_canonical_id"])


class TestTheOffEdgeRestoresIt(ManualRestoreTestCase):
    def test_the_hand_merge_goes_back_to_the_release_the_person_picked(self):
        db = self._db()
        self._reHomedHandMerge(db)

        db.repo.unmergeAllIsrcMerges()

        self.assertEqual(self._canonical(db, HAND), REMASTER)
        self.assertIsNone(self._canonical(db, REMASTER))   #< the matcher's own work is gone
        self._assertNoChains(db)

    def test_the_verdict_keeps_its_author_and_drops_the_carry(self):
        db = self._db()
        self._reHomedHandMerge(db)

        db.repo.unmergeAllIsrcMerges()

        decision = self._decision(db, HAND)
        self.assertEqual(decision["canonical_id"], REMASTER)
        self.assertIsNone(decision["carried_canonical_id"])
        self.assertEqual(decision["decided_by"], "timorzipa")

    def test_off_then_on_then_off_lands_in_the_same_place(self):
        """The toggle is a real undo only if it is stable across cycles."""
        db = self._db()
        self._reHomedHandMerge(db)

        db.repo.unmergeAllIsrcMerges()
        db.repo.mergeTracksByIsrc()
        self.assertEqual(self._canonical(db, HAND), PLAIN)

        db.repo.unmergeAllIsrcMerges()

        self.assertEqual(self._canonical(db, HAND), REMASTER)
        self.assertIsNone(self._decision(db, HAND)["carried_canonical_id"])
        self._assertNoChains(db)

    def test_a_revert_with_nothing_carried_behaves_exactly_as_before(self):
        db = self._db()
        self._track(db, REMASTER, isrc=ISRC_A, name="Song", plays=70)
        self._track(db, PLAIN, isrc=ISRC_A, name="Song", plays=13)
        db.repo.mergeTracksByIsrc()

        undone = db.repo.unmergeAllIsrcMerges()

        self.assertEqual(undone, 1)
        self.assertIsNone(self._canonical(db, PLAIN))
        self.assertIsNone(self._canonical(db, REMASTER))


class TestAReDecisionDropsTheCarry(ManualRestoreTestCase):
    """A person re-deciding replaces the verdict outright: what they chose
    before is no longer what they chose. Leaving the carry behind would let a
    later revert drag the track back to an abandoned target - and, with the
    intermediate release still merged, build the two-hop chain no reader
    resolves."""

    def test_a_later_manual_merge_replaces_the_verdict_outright(self):
        db = self._db()
        self._reHomedHandMerge(db)
        self._track(db, OTHER, name="Song - Alternate", plays=2)

        db.repo.mergeTrackManually(HAND, OTHER, decidedBy="timorzipa")

        decision = self._decision(db, HAND)
        self.assertEqual(decision["canonical_id"], OTHER)
        self.assertIsNone(decision["carried_canonical_id"])

    def test_a_revert_after_a_re_decision_leaves_it_where_the_person_last_put_it(self):
        db = self._db()
        self._reHomedHandMerge(db)
        self._track(db, OTHER, name="Song - Alternate", plays=2)
        db.repo.mergeTrackManually(HAND, OTHER, decidedBy="timorzipa")

        db.repo.unmergeAllIsrcMerges()

        self.assertEqual(self._canonical(db, HAND), OTHER)
        self._assertNoChains(db)

    def test_a_manual_split_drops_the_carry_too(self):
        db = self._db()
        self._reHomedHandMerge(db)

        db.repo.unmergeTrack(HAND, decidedBy="timorzipa")
        db.repo.unmergeAllIsrcMerges()

        #< taken out by hand and it stays out: the revert must not resurrect it
        self.assertIsNone(self._canonical(db, HAND))
        decision = self._decision(db, HAND)
        self.assertEqual(decision["reason"], "manual-split")
        self.assertIsNone(decision["carried_canonical_id"])

    def test_two_hand_merges_in_one_group_still_revert_without_a_chain(self):
        """The shape the clearing rule exists for, driven end to end."""
        db = self._db()
        self._reHomedHandMerge(db)
        self._track(db, OTHER, name="Song - Alternate", plays=2)
        db.repo.mergeTrackManually(OTHER, HAND, decidedBy="timorzipa")

        db.repo.unmergeAllIsrcMerges()

        self._assertNoChains(db)


if __name__ == "__main__":
    unittest.main()
