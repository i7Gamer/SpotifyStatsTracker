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
