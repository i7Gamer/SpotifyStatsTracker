"""Phase 4: the ISRC matcher that fills tracks.canonical_id.

An ISRC identifies a RECORDING, so two tracks carrying the same one are the
same performance released twice - a single and an album, or an album and a
compilation. That is a fact rather than an inference, which is why this tier
merges automatically where a title-similarity tier would have to ask.

Most of this file is about what the matcher must refuse to do. Merging is the
easy half; not merging the wrong things, not undoing a person's decision, and
not moving the goalposts on a re-run are the halves that cost data.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase

ISRC_A = "USRC12345678"
ISRC_B = "GBAYE0000001"


class TrackMergeTestCase(DatabaseTestCase):
    def _db(self):
        return self._makeDb({}, [])

    def _track(self, db, trackId, isrc=None, name="Song", createdAt=1000.0, plays=0, user="alice"):
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES ('alb1', 'Album 1', '')")
            conn.execute("INSERT OR IGNORE INTO users (username, created_at) VALUES (?, 0)", (user,))
            conn.execute("INSERT INTO tracks (id, name, url, album_id, isrc, created_at) "
                         "VALUES (?, ?, '', 'alb1', ?, ?)", (trackId, name, isrc, createdAt))
            for i in range(plays):
                conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                             "VALUES (?, ?, ?, 200000)", (user, trackId, 1e9 + i))
        return trackId

    def _canonical(self, db, trackId):
        return db.repo._conn().execute(
            "SELECT canonical_id FROM tracks WHERE id=?", (trackId,)).fetchone()[0]

    def _decision(self, db, trackId):
        row = db.repo._conn().execute(
            "SELECT canonical_id, reason, evidence, decided_by FROM track_merge_decisions "
            "WHERE track_id=?", (trackId,)).fetchone()
        return dict(row) if row else None


class TestIsrcMerging(TrackMergeTestCase):
    def test_two_tracks_sharing_an_isrc_merge(self):
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, plays=10)
        self._track(db, "B" * 22, isrc=ISRC_A, plays=2)

        summary = db.repo.mergeTracksByIsrc()

        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)
        self.assertIsNone(self._canonical(db, "A" * 22))   #< the canonical points at nothing
        self.assertEqual(summary["merged"], 1)

    def test_the_most_played_track_becomes_canonical(self):
        """Whichever entry people actually use is the one their links and their
        detail page should land on."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, plays=2)
        self._track(db, "B" * 22, isrc=ISRC_A, plays=40)

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._canonical(db, "A" * 22), "B" * 22)

    def test_plays_are_counted_across_every_user(self):
        """The merge is global - sameness is a property of the recording, not of
        one listener - so the election has to be too."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, plays=5, user="alice")
        self._track(db, "B" * 22, isrc=ISRC_A, plays=3, user="alice")
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO users (username, created_at) VALUES ('bob', 0)")
            for i in range(9):
                conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                             "VALUES ('bob', ?, ?, 200000)", ("B" * 22, 2e9 + i))

        db.repo.mergeTracksByIsrc()

        #< 5 for alice against 3+9 for bob's side
        self.assertEqual(self._canonical(db, "A" * 22), "B" * 22)

    def test_a_three_way_group_all_points_at_one_canonical(self):
        """Never a chain: a merged track's canonical must itself be canonical,
        or every reader has to walk a linked list of unknown depth."""
        db = self._db()
        for trackId, plays in (("A" * 22, 1), ("B" * 22, 50), ("C" * 22, 3)):
            self._track(db, trackId, isrc=ISRC_A, plays=plays)

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._canonical(db, "A" * 22), "B" * 22)
        self.assertEqual(self._canonical(db, "C" * 22), "B" * 22)
        self.assertIsNone(self._canonical(db, "B" * 22))

    def test_separate_isrcs_stay_separate(self):
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A)
        self._track(db, "B" * 22, isrc=ISRC_B)

        db.repo.mergeTracksByIsrc()

        self.assertIsNone(self._canonical(db, "A" * 22))
        self.assertIsNone(self._canonical(db, "B" * 22))

    def test_tracks_without_an_isrc_are_left_alone(self):
        """Most of the catalog, while the backfill drains against a quota. An
        absent ISRC is "not asked yet", never "no match"."""
        db = self._db()
        self._track(db, "A" * 22, isrc=None, name="Same Name")
        self._track(db, "B" * 22, isrc="", name="Same Name")

        summary = db.repo.mergeTracksByIsrc()

        self.assertIsNone(self._canonical(db, "A" * 22))
        self.assertIsNone(self._canonical(db, "B" * 22))
        self.assertEqual(summary["merged"], 0)


class TestTheMatcherDoesNotOverreach(TrackMergeTestCase):
    def test_a_manual_decision_is_never_overwritten(self):
        """A person looked at this pair and said no. An automatic pass that
        re-merges them makes the review queue pointless and the disagreement
        invisible."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, plays=10)
        self._track(db, "B" * 22, isrc=ISRC_A, plays=2)
        conn = db.repo._conn()
        with conn:
            conn.execute(
                "INSERT INTO track_merge_decisions "
                "(track_id, canonical_id, reason, evidence, decided_at, decided_by) "
                "VALUES (?, NULL, 'manual-split', 'not the same recording', 1.0, 'timorzipa')",
                ("B" * 22,))

        db.repo.mergeTracksByIsrc()

        self.assertIsNone(self._canonical(db, "B" * 22))
        self.assertEqual(self._decision(db, "B" * 22)["decided_by"], "timorzipa")

    def test_an_existing_canonical_is_sticky(self):
        """Re-electing on every pass would move the canonical - and every link
        to it - as play counts drift. The first verdict stands until a person
        changes it."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, plays=10)
        self._track(db, "B" * 22, isrc=ISRC_A, plays=2)
        db.repo.mergeTracksByIsrc()
        conn = db.repo._conn()
        with conn:   #< B overtakes A
            for i in range(100):
                conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                             "VALUES ('alice', ?, ?, 200000)", ("B" * 22, 3e9 + i))

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)

    def test_two_canonicals_for_one_recording_are_left_alone(self):
        """The branch that actually guarantees no chains: if the group cannot
        agree where it points, the matcher declines rather than picking.

        Reachable only by a hand edit or a merge that predates this, but it is
        what makes "a canonical never points anywhere itself" true - a member
        pointing outside the group puts a second value in play, and this is the
        branch that catches it. Untested until mutation testing showed the
        belt-and-braces line I had written instead could never run."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, plays=10)
        self._track(db, "B" * 22, isrc=ISRC_A, plays=5)
        self._track(db, "C" * 22, isrc=ISRC_A, plays=1)
        conn = db.repo._conn()
        with conn:   #< two members pointing at different places
            conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?", ("A" * 22, "B" * 22))
            conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?", ("B" * 22, "C" * 22))

        summary = db.repo.mergeTracksByIsrc()

        self.assertEqual(summary["merged"], 0)
        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)
        self.assertEqual(self._canonical(db, "C" * 22), "B" * 22)

    def test_a_canonical_never_points_anywhere_itself(self):
        """Stated as the property readers depend on, rather than as a step: a
        merged track's target must be a genuine endpoint."""
        db = self._db()
        for trackId, plays in (("A" * 22, 1), ("B" * 22, 50), ("C" * 22, 3)):
            self._track(db, trackId, isrc=ISRC_A, plays=plays)

        db.repo.mergeTracksByIsrc()

        conn = db.repo._conn()
        for trackId, in conn.execute("SELECT id FROM tracks WHERE canonical_id IS NOT NULL"):
            target = self._canonical(db, trackId)
            self.assertIsNone(self._canonical(db, target),
                              f"{trackId} points at {target}, which points on again")

    def test_a_member_with_its_own_dependents_brings_them_along(self):
        """An admin merged A into C by hand (a remaster - no ISRC of its own);
        the backfill then learns C and D share a master and elects D. C moves,
        so A must move WITH it: a canonical that itself points somewhere is a
        chain, and every reader resolves exactly one hop
        (resolveCanonicalTrackId, _canonicalTrackJoin, _expandToMergeGroups) -
        left behind, A falls out of the group and its song renders twice.
        Same move mergeTrackManually makes for the mirror case."""
        db = self._db()
        self._track(db, "A" * 22, plays=1)               #< the hand-merged remaster
        self._track(db, "C" * 22, isrc=ISRC_A, plays=8)
        self._track(db, "D" * 22, isrc=ISRC_A, plays=25)
        db.repo.mergeTrackManually("A" * 22, "C" * 22, decidedBy="timorzipa")

        summary = db.repo.mergeTracksByIsrc()

        self.assertEqual(self._canonical(db, "A" * 22), "D" * 22)
        self.assertEqual(self._canonical(db, "C" * 22), "D" * 22)
        self.assertIsNone(self._canonical(db, "D" * 22))
        #< the audit row moved with it and stays the person's own verdict
        decision = self._decision(db, "A" * 22)
        self.assertEqual(decision["canonical_id"], "D" * 22)
        self.assertEqual(decision["reason"], "manual-merge")
        self.assertEqual(decision["decided_by"], "timorzipa")
        #< A was already merged - re-homed, not newly collapsed - so the
        #  summary (and the admin message built from it) counts only C, which
        #  also keeps the preview's number equal to the run's
        self.assertEqual(summary["merged"], 1)

    def test_running_it_again_changes_nothing(self):
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, plays=10)
        self._track(db, "B" * 22, isrc=ISRC_A, plays=2)

        first = db.repo.mergeTracksByIsrc()
        second = db.repo.mergeTracksByIsrc()

        self.assertEqual(first["merged"], 1)
        self.assertEqual(second["merged"], 0)
        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)

    def test_a_track_never_merges_into_itself(self):
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A)

        db.repo.mergeTracksByIsrc()

        self.assertIsNone(self._canonical(db, "A" * 22))


class TestTheHeadMovesToTheNormalVersion(TrackMergeTestCase):
    """Rule 2's one exception: a head beaten on TITLE by its own group steps
    down.

    Sticky exists because play counts drift, and re-electing on them every
    pass would walk the canonical - and every link to it - around forever. A
    title does not drift: a release either carries a version marker or it does
    not, and the answer is the same on every pass. So the title half of the
    election can be applied to a group that already has a head, once, and it
    settles - while plays stay exactly as sticky as they were.

    What it fixes: every group merged before the title rule existed is headed
    by whatever was played most, so the song's page can read "- 2005
    Remaster" with the original sitting underneath it as a member."""

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

    def test_a_group_headed_by_a_remaster_moves_to_the_plain_release(self):
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, name="Psycho Killer - 2005 Remaster", plays=70)
        self._track(db, "B" * 22, isrc=ISRC_A, name="Psycho Killer", plays=13)
        self._mergedUnderThePlaysOnlyRule(db, "A" * 22, "B" * 22)

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._canonical(db, "A" * 22), "B" * 22)
        self.assertIsNone(self._canonical(db, "B" * 22))

    def test_the_promoted_release_keeps_no_trace_of_having_been_a_member(self):
        """A canonical carries no decision row - it was not merged, it is what
        the others were merged into - so the row from when it WAS a member has
        to go, or the audit says it points at a track that now points at it."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, name="Song - 2005 Remaster", plays=70)
        self._track(db, "B" * 22, isrc=ISRC_A, name="Song", plays=13)
        self._mergedUnderThePlaysOnlyRule(db, "A" * 22, "B" * 22)

        db.repo.mergeTracksByIsrc()

        self.assertIsNone(self._decision(db, "B" * 22))
        #< and the release that stepped down is now an ordinary matcher merge
        decision = self._decision(db, "A" * 22)
        self.assertEqual(decision["canonical_id"], "B" * 22)
        self.assertEqual(decision["reason"], "isrc")
        self.assertIsNone(decision["decided_by"])

    def test_the_old_heads_own_members_come_with_it(self):
        """The head moving is the same carry-along a member moving already
        needs: left behind, they point at a track that points on again."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, name="Song - 2005 Remaster", plays=70)
        self._track(db, "B" * 22, isrc=ISRC_A, name="Song", plays=13)
        self._track(db, "C" * 22, isrc=ISRC_A, name="Song (Deluxe Edition)", plays=5)
        self._mergedUnderThePlaysOnlyRule(db, "A" * 22, "B" * 22, "C" * 22)

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._canonical(db, "C" * 22), "B" * 22)
        self.assertEqual(self._decision(db, "C" * 22)["canonical_id"], "B" * 22)
        for trackId, in db.repo._conn().execute(
                "SELECT id FROM tracks WHERE canonical_id IS NOT NULL"):
            target = self._canonical(db, trackId)
            self.assertIsNone(self._canonical(db, target),
                              f"{trackId} points at {target}, which points on again")

    def test_a_hand_merged_dependent_of_the_old_head_comes_along_too(self):
        """It carries no ISRC, so the planner cannot see it at all - the same
        blind spot a member's own dependents already have."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, name="Song - 2005 Remaster", plays=70)
        self._track(db, "B" * 22, isrc=ISRC_A, name="Song", plays=13)
        self._track(db, "D" * 22, name="Song - Mono", plays=1)
        self._mergedUnderThePlaysOnlyRule(db, "A" * 22, "B" * 22)
        db.repo.mergeTrackManually("D" * 22, "A" * 22, decidedBy="timorzipa")

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._canonical(db, "D" * 22), "B" * 22)
        #< moved, but still the person's own verdict
        decision = self._decision(db, "D" * 22)
        self.assertEqual(decision["canonical_id"], "B" * 22)
        self.assertEqual(decision["reason"], "manual-merge")
        self.assertEqual(decision["decided_by"], "timorzipa")

    def test_the_move_settles_after_one_run(self):
        """A title is the same on every pass, which is the whole licence for
        touching a head at all - so the second run has nothing left to do."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, name="Song - 2005 Remaster", plays=70)
        self._track(db, "B" * 22, isrc=ISRC_A, name="Song", plays=13)
        self._track(db, "C" * 22, isrc=ISRC_A, name="Song (Deluxe Edition)", plays=5)
        self._mergedUnderThePlaysOnlyRule(db, "A" * 22, "B" * 22, "C" * 22)

        first = db.repo.mergeTracksByIsrc()
        second = db.repo.mergeTracksByIsrc()

        #< only the head itself was newly pointed; C was carried, not collapsed
        self.assertEqual(first["merged"], 1)
        self.assertEqual(second["merged"], 0)
        self.assertEqual(self._canonical(db, "A" * 22), "B" * 22)

    def test_plays_alone_still_never_move_a_head(self):
        """The sticky rule is intact where it was written for. The member out-
        plays the head 100 to 1 and carries a marker; nothing moves."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, name="Song", plays=1)
        self._track(db, "B" * 22, isrc=ISRC_A, name="Song - 2005 Remaster", plays=100)
        self._mergedUnderThePlaysOnlyRule(db, "A" * 22, "B" * 22)

        summary = db.repo.mergeTracksByIsrc()

        self.assertEqual(summary["merged"], 0)
        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)

    def test_two_equally_plain_releases_leave_the_head_where_it_is(self):
        """The title rule is silent here, and silence must not be read as a
        reason to move: this is exactly the drift rule 2 forbids."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, name="Song", plays=1)
        self._track(db, "B" * 22, isrc=ISRC_A, name="Song", plays=100)
        self._mergedUnderThePlaysOnlyRule(db, "A" * 22, "B" * 22)

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)

    def test_a_head_a_person_pinned_is_left_where_they_put_it(self):
        """Rule 1 outranks this: a pinned track takes no part, and promoting
        one of its members would move the pinned track onto something a person
        said it is not the same as."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, name="Song - 2005 Remaster", plays=70)
        self._track(db, "B" * 22, isrc=ISRC_A, name="Song", plays=13)
        self._track(db, "C" * 22, isrc=ISRC_A, name="Song (Deluxe Edition)", plays=5)
        self._mergedUnderThePlaysOnlyRule(db, "A" * 22, "B" * 22, "C" * 22)
        db.repo.unmergeTrack("A" * 22, decidedBy="timorzipa")   #< pins the head

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)
        self.assertEqual(self._canonical(db, "C" * 22), "A" * 22)

    def test_the_preview_says_the_move_is_coming(self):
        """The admin page's number has to be the run's number, moves
        included - looking first is what makes the checkbox a decision."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, name="Song - 2005 Remaster", plays=70)
        self._track(db, "B" * 22, isrc=ISRC_A, name="Song", plays=13)
        self._mergedUnderThePlaysOnlyRule(db, "A" * 22, "B" * 22)

        preview = db.repo.previewMergeTracksByIsrc()

        self.assertEqual(preview["groups"][0]["canonical"]["trackId"], "B" * 22)
        self.assertEqual([m["trackId"] for m in preview["groups"][0]["members"]], ["A" * 22])
        self.assertEqual(preview["merged"], db.repo.mergeTracksByIsrc()["merged"])

    def test_the_move_is_undone_by_the_toggle_like_any_other_merge(self):
        """Nothing it writes is destructive, so the off edge still returns the
        group to three separate songs."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, name="Song - 2005 Remaster", plays=70)
        self._track(db, "B" * 22, isrc=ISRC_A, name="Song", plays=13)
        self._track(db, "C" * 22, isrc=ISRC_A, name="Song (Deluxe Edition)", plays=5)
        self._mergedUnderThePlaysOnlyRule(db, "A" * 22, "B" * 22, "C" * 22)
        db.repo.mergeTracksByIsrc()

        db.repo.unmergeAllIsrcMerges()

        for trackId in ("A" * 22, "B" * 22, "C" * 22):
            self.assertIsNone(self._canonical(db, trackId), trackId)
            self.assertIsNone(self._decision(db, trackId), trackId)


class TestTheDecisionTrail(TrackMergeTestCase):
    def test_every_merge_records_why(self):
        """Reversible by design: the ISRC that justified the merge is kept, so
        it can be explained and re-checked long after the catalog row it came
        from has changed underneath it."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, plays=10)
        self._track(db, "B" * 22, isrc=ISRC_A, plays=2)

        db.repo.mergeTracksByIsrc()

        decision = self._decision(db, "B" * 22)
        self.assertEqual(decision["canonical_id"], "A" * 22)
        self.assertEqual(decision["reason"], "isrc")
        self.assertEqual(decision["evidence"], ISRC_A)
        self.assertIsNone(decision["decided_by"])   #< the matcher, not a person

    def test_the_canonical_gets_no_decision_row(self):
        """It was not merged; it is what the others were merged INTO."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, plays=10)
        self._track(db, "B" * 22, isrc=ISRC_A, plays=2)

        db.repo.mergeTracksByIsrc()

        self.assertIsNone(self._decision(db, "A" * 22))


if __name__ == "__main__":
    unittest.main()


class TestEveryMergeCanBeUndone(TrackMergeTestCase):
    """The guarantee the whole feature rests on: nothing here destroys
    information, so any merge can be taken back.

    A merge writes canonical_id (a pointer, reversible) and a decision row
    (additive). It never touches a play, never rewrites a track, never deletes
    anything. That is what makes "undo" a matter of clearing a column rather
    than restoring a backup - and it is a property to keep, not a coincidence."""

    def test_a_merge_touches_nothing_that_cannot_be_put_back(self):
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, plays=10)
        self._track(db, "B" * 22, isrc=ISRC_A, plays=2)
        conn = db.repo._conn()
        before = {
            "plays": conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0],
            "tracks": conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0],
            "names": sorted(r[0] for r in conn.execute("SELECT name FROM tracks")),
        }

        db.repo.mergeTracksByIsrc()

        self.assertEqual(conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0], before["plays"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0], before["tracks"])
        self.assertEqual(sorted(r[0] for r in conn.execute("SELECT name FROM tracks")), before["names"])

    def test_unmerging_one_track_puts_it_back_and_keeps_it_back(self):
        """Both halves matter. Clearing canonical_id alone would last exactly
        until the next matcher run re-merged it, which is not an undo."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, plays=10)
        self._track(db, "B" * 22, isrc=ISRC_A, plays=2)
        db.repo.mergeTracksByIsrc()

        db.repo.unmergeTrack("B" * 22, decidedBy="timorzipa")
        db.repo.mergeTracksByIsrc()   #< the matcher runs again and must not undo the undo

        self.assertIsNone(self._canonical(db, "B" * 22))
        self.assertEqual(self._decision(db, "B" * 22)["decided_by"], "timorzipa")

    def test_unmerging_an_unknown_track_is_refused(self):
        """The same ValueError its siblings raise (dismissMergeCandidate,
        mergeTrackManually), so the route can answer 400. Without the check
        the decision INSERT hits the tracks(id) FOREIGN KEY instead and the
        split button 500s."""
        db = self._db()
        with self.assertRaises(ValueError):
            db.repo.unmergeTrack("Z" * 22, decidedBy="timorzipa")

    def test_undoing_the_whole_run_clears_every_automatic_merge(self):
        db = self._db()
        for trackId, plays in (("A" * 22, 9), ("B" * 22, 2), ("C" * 22, 1)):
            self._track(db, trackId, isrc=ISRC_A, plays=plays)
        db.repo.mergeTracksByIsrc()

        undone = db.repo.unmergeAllIsrcMerges()

        self.assertEqual(undone, 2)
        for trackId in ("A" * 22, "B" * 22, "C" * 22):
            self.assertIsNone(self._canonical(db, trackId))
            self.assertIsNone(self._decision(db, trackId))

    def test_undoing_the_run_leaves_a_persons_decisions_alone(self):
        """A full revert is "undo what the matcher did", not "forget what anyone
        decided" - those verdicts are the expensive kind to recreate."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, plays=9)
        self._track(db, "B" * 22, isrc=ISRC_A, plays=2)
        db.repo.mergeTracksByIsrc()
        db.repo.unmergeTrack("B" * 22, decidedBy="timorzipa")

        db.repo.unmergeAllIsrcMerges()

        self.assertEqual(self._decision(db, "B" * 22)["decided_by"], "timorzipa")

    def test_a_full_revert_is_repeatable_and_re_mergeable(self):
        """Undo, then redo: the matcher is the only thing that decides, so a
        revert cannot be a one-way door either."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, plays=9)
        self._track(db, "B" * 22, isrc=ISRC_A, plays=2)
        db.repo.mergeTracksByIsrc()
        db.repo.unmergeAllIsrcMerges()

        self.assertEqual(db.repo.unmergeAllIsrcMerges(), 0)   #< idempotent
        self.assertEqual(db.repo.mergeTracksByIsrc()["merged"], 1)
        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)


class TestThePreview(TrackMergeTestCase):
    """What the admin page shows before anything is written."""

    def test_it_describes_what_a_run_would_do(self):
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, plays=9, name="Shared Song")
        self._track(db, "B" * 22, isrc=ISRC_A, plays=2, name="Shared Song")

        preview = db.repo.previewMergeTracksByIsrc()

        self.assertEqual(preview["merged"], 1)
        self.assertEqual(len(preview["groups"]), 1)
        group = preview["groups"][0]
        self.assertEqual(group["canonical"]["trackId"], "A" * 22)
        self.assertEqual([m["trackId"] for m in group["members"]], ["B" * 22])
        self.assertEqual(group["isrc"], ISRC_A)

    def test_the_preview_writes_nothing(self):
        """The whole point: it answers "what would happen" without it happening."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, plays=9)
        self._track(db, "B" * 22, isrc=ISRC_A, plays=2)

        db.repo.previewMergeTracksByIsrc()

        self.assertIsNone(self._canonical(db, "B" * 22))
        self.assertIsNone(self._decision(db, "B" * 22))

    def test_the_preview_agrees_with_the_run_that_follows(self):
        """A preview nobody can trust is worse than none."""
        db = self._db()
        for trackId, plays in (("A" * 22, 9), ("B" * 22, 2), ("C" * 22, 1)):
            self._track(db, trackId, isrc=ISRC_A, plays=plays)

        preview = db.repo.previewMergeTracksByIsrc()
        applied = db.repo.mergeTracksByIsrc()

        self.assertEqual(preview["merged"], applied["merged"])

    def test_a_member_nobody_has_played_still_counts(self):
        """The play tally arrives via an aggregate join over plays; a track
        with no plays at all has no row on that side, and must stay in its
        group at zero rather than vanish. Exactly the state a new arrival is
        in when the backfiller completes its pair."""
        db = self._db()
        self._track(db, "A" * 22, isrc=ISRC_A, plays=4)
        self._track(db, "B" * 22, isrc=ISRC_A, plays=0)

        preview = db.repo.previewMergeTracksByIsrc()

        self.assertEqual(preview["merged"], 1)
        group = preview["groups"][0]
        self.assertEqual(group["canonical"]["trackId"], "A" * 22)
        self.assertEqual(group["members"],
                         [{"trackId": "B" * 22, "name": "Song", "plays": 0}])
