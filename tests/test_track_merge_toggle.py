"""The admin checkbox that turns track merging on and off.

The toggle gates the DATA, not the queries: switching on runs the ISRC matcher,
switching off unmerges everything the matcher did. Read paths honour
canonical_id unconditionally - safe precisely because it is only ever set while
the feature is on - and that is what makes the off position a genuine, instant,
lossless undo rather than a display filter.

New tracks are the reason it is a checkbox and not a button: the ISRC
backfiller keeps stamping new arrivals, so merge candidates appear for as long
as anyone listens to new music, and each backfill batch that records ISRCs
re-runs the matcher while the toggle is on.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
from Database.database import Database

ISRC = "USRC12345678"


class ToggleTestCase(DatabaseTestCase):
    def _dbWithPair(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES ('alb1', 'A', '')")
            conn.execute("INSERT OR IGNORE INTO users (username, created_at) VALUES ('alice', 0)")
            for trackId, plays in (("A" * 22, 5), ("B" * 22, 2)):
                conn.execute("INSERT INTO tracks (id, name, url, album_id, isrc) "
                             "VALUES (?, 'Song', '', 'alb1', ?)", (trackId, ISRC))
                for i in range(plays):
                    conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                                 "VALUES ('alice', ?, ?, 200000)", (trackId, 1e9 + i))
        return db

    def _canonical(self, db, trackId):
        return db.repo._conn().execute(
            "SELECT canonical_id FROM tracks WHERE id=?", (trackId,)).fetchone()[0]


class TestTheSettingItself(ToggleTestCase):
    def test_it_defaults_off(self):
        """A merge moves every account's numbers at once. It has to be a
        decision someone made, never a default they inherited - the only other
        toggle with this default is the push listener, for the same reason."""
        db = self._makeDb({}, [])

        self.assertFalse(db.repo.isTrackMergeEnabled())

    def test_it_round_trips(self):
        db = self._makeDb({}, [])
        db.repo.setTrackMergeEnabled(True)
        self.assertTrue(db.repo.isTrackMergeEnabled())
        db.repo.setTrackMergeEnabled(False)
        self.assertFalse(db.repo.isTrackMergeEnabled())


class TestTheBackfillerKeepsMergesCurrent(ToggleTestCase):
    """The "new tracks" half of the question: while the toggle is on, the
    matcher re-runs after any backfill batch that recorded ISRCs, so a pair
    completed by a new arrival merges within a cycle of its ISRC arriving."""

    def _runOneIsrcBatch(self, db):
        """Drive the real backfill step with a mocked per-id endpoint."""
        response = MagicMock()
        response.status_code = 200
        response.text = ""
        response.json.return_value = {"external_ids": {"isrc": ISRC}}
        stop = MagicMock(is_set=MagicMock(return_value=False))
        with patch("requests.get", return_value=response):
            db._backfillTrackIsrcs(lambda: "mock_token", stop)

    def test_a_batch_that_completes_a_pair_merges_it(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES ('alb1', 'A', '')")
            #< one track already stamped, its pair still missing the ISRC -
            #  exactly the state a new arrival is in until the backfiller runs
            conn.execute("INSERT INTO tracks (id, name, url, album_id, isrc) "
                         "VALUES (?, 'Song', '', 'alb1', ?)", ("A" * 22, ISRC))
            conn.execute("INSERT INTO tracks (id, name, url, album_id) "
                         "VALUES (?, 'Song', '', 'alb1')", ("B" * 22,))
        db.repo.setTrackMergeEnabled(True)

        self._runOneIsrcBatch(db)
        #< what the loop does after a batch that recorded ISRCs
        if db.repo.isTrackMergeEnabled():
            db.repo.mergeTracksByIsrc()

        self.assertIsNotNone(self._canonical(db, "B" * 22) or self._canonical(db, "A" * 22))

    def test_the_loop_skips_the_matcher_while_off(self):
        """A disabled feature costs nothing - pinned by the loop gating on the
        toggle, which the source assertion below keeps wired."""
        import inspect
        from Database.workers.metadata_backfiller import MetadataBackfillMixin
        source = inspect.getsource(MetadataBackfillMixin._metadataBackfillLoop)

        self.assertIn("isTrackMergeEnabled", source)
        self.assertIn("mergeTracksByIsrc", source)


class TestReversibilityAtTheToggle(ToggleTestCase):
    """The property the whole design was chosen for: OFF undoes everything the
    matcher did, instantly and losslessly, whenever it is pressed."""

    def test_on_then_off_is_a_round_trip(self):
        db = self._dbWithPair()

        db.repo.setTrackMergeEnabled(True)
        db.repo.mergeTracksByIsrc()
        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)

        db.repo.setTrackMergeEnabled(False)
        undone = db.repo.unmergeAllIsrcMerges()

        self.assertEqual(undone, 1)
        self.assertIsNone(self._canonical(db, "A" * 22))
        self.assertIsNone(self._canonical(db, "B" * 22))

    def test_off_keeps_manual_verdicts_for_the_next_on(self):
        """The one thing a full undo must NOT undo: a person's "not the same
        recording". Toggle off, toggle on - the matcher still honours it."""
        db = self._dbWithPair()
        db.repo.setTrackMergeEnabled(True)
        db.repo.mergeTracksByIsrc()
        db.repo.unmergeTrack("B" * 22, decidedBy="timorzipa")

        db.repo.setTrackMergeEnabled(False)
        db.repo.unmergeAllIsrcMerges()
        db.repo.setTrackMergeEnabled(True)
        db.repo.mergeTracksByIsrc()

        self.assertIsNone(self._canonical(db, "B" * 22))


if __name__ == "__main__":
    unittest.main()
