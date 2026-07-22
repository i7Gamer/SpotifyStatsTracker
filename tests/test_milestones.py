"""Achievement-milestone detection, seeding, and display.

Detection is exercised against a real temp Repository (so the SQL and the
seed/notify bookkeeping are under test) but a lightweight fake Database, so a
"12,000 plays" milestone doesn't require inserting 12,000 play rows.
"""
import os
import sys
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import Repository
from services.milestones import (
    detectMilestones, formatMilestone,
    MILESTONE_KIND_PLAYS, MILESTONE_KIND_LISTEN_TIME, MILESTONE_KIND_STREAK, MILESTONE_KIND_TOP_ARTIST,
    _MS_PER_HOUR,
)


class _FakeDb:
    """Stand-in for a Database returning controlled stat values, so detection
    can be tested without inserting thousands of play rows."""

    def __init__(self, plays=0, ms=0, streakDays=0, topArtist=None):
        self._plays = plays
        self._ms = ms
        self._streakDays = streakDays
        self._topArtist = topArtist

    def getPlayTotals(self, start, end):
        return (self._plays, self._ms)

    def getCurrentStreak(self):
        return {"days": self._streakDays, "activeToday": True}

    def getTopArtists(self, startDate=None, endDate=None, by="plays", limit=None):
        return [self._topArtist] if self._topArtist else []


class _RepoTestCase(unittest.TestCase):
    USER = "alice"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "milestones.db")
        self.addCleanup(self.repo.connectionManager.close)
        self.repo.upsertUser(self.USER, f"{self.USER}@example.com", createdAt=100.0)


class TestMilestoneRepo(_RepoTestCase):
    def test_record_unseen_count_and_mark_seen(self):
        self.repo.recordMilestone(self.USER, MILESTONE_KIND_PLAYS, 1000, None, 1.0, seen=False)
        self.repo.recordMilestone(self.USER, MILESTONE_KIND_STREAK, 7, None, 2.0, seen=True)
        self.assertEqual(self.repo.getUnseenMilestoneCount(self.USER), 1)

        self.repo.markMilestonesSeen(self.USER)
        self.assertEqual(self.repo.getUnseenMilestoneCount(self.USER), 0)

    def test_has_threshold_milestone(self):
        self.assertFalse(self.repo.hasThresholdMilestone(self.USER, MILESTONE_KIND_PLAYS, 1000))
        self.repo.recordMilestone(self.USER, MILESTONE_KIND_PLAYS, 1000, None, 1.0, seen=False)
        self.assertTrue(self.repo.hasThresholdMilestone(self.USER, MILESTONE_KIND_PLAYS, 1000))
        self.assertFalse(self.repo.hasThresholdMilestone(self.USER, MILESTONE_KIND_PLAYS, 5000))

    def test_get_milestones_newest_first(self):
        self.repo.recordMilestone(self.USER, MILESTONE_KIND_PLAYS, 1000, None, 10.0, seen=True)
        self.repo.recordMilestone(self.USER, MILESTONE_KIND_PLAYS, 5000, None, 20.0, seen=True)
        rows = self.repo.getMilestonesForUser(self.USER)
        self.assertEqual([r["threshold"] for r in rows], [5000, 1000])

    def test_baseline_get_set(self):
        self.assertIsNone(self.repo.getMilestoneBaselineAt(self.USER))
        self.repo.setMilestoneBaselineAt(self.USER, 1234.5)
        self.assertEqual(self.repo.getMilestoneBaselineAt(self.USER), 1234.5)

    def test_get_latest_milestone(self):
        self.assertIsNone(self.repo.getLatestMilestone(self.USER, MILESTONE_KIND_TOP_ARTIST))
        self.repo.recordMilestone(self.USER, MILESTONE_KIND_TOP_ARTIST, 0,
                                  json.dumps({"id": "a1", "name": "A"}), 1.0, seen=True)
        self.repo.recordMilestone(self.USER, MILESTONE_KIND_TOP_ARTIST, 0,
                                  json.dumps({"id": "a2", "name": "B"}), 2.0, seen=False)
        latest = self.repo.getLatestMilestone(self.USER, MILESTONE_KIND_TOP_ARTIST)
        self.assertEqual(json.loads(latest["detail"])["id"], "a2")


class TestDetectMilestones(_RepoTestCase):
    def test_first_pass_seeds_everything_as_seen(self):
        db = _FakeDb(plays=12000, ms=300 * _MS_PER_HOUR, streakDays=40,
                     topArtist={"id": "art1", "name": "Radiohead"})
        recorded = detectMilestones(db, self.repo, self.USER)

        self.assertGreater(recorded, 0)
        # Baseline stamped, and nothing surfaces as a notification.
        self.assertIsNotNone(self.repo.getMilestoneBaselineAt(self.USER))
        self.assertEqual(self.repo.getUnseenMilestoneCount(self.USER), 0)
        # The expected already-achieved thresholds are recorded (and only those).
        self.assertTrue(self.repo.hasThresholdMilestone(self.USER, MILESTONE_KIND_PLAYS, 10000))
        self.assertFalse(self.repo.hasThresholdMilestone(self.USER, MILESTONE_KIND_PLAYS, 25000))
        self.assertTrue(self.repo.hasThresholdMilestone(self.USER, MILESTONE_KIND_LISTEN_TIME, 250))
        self.assertFalse(self.repo.hasThresholdMilestone(self.USER, MILESTONE_KIND_LISTEN_TIME, 500))
        self.assertTrue(self.repo.hasThresholdMilestone(self.USER, MILESTONE_KIND_STREAK, 30))
        self.assertIsNotNone(self.repo.getLatestMilestone(self.USER, MILESTONE_KIND_TOP_ARTIST))

    def test_new_crossing_after_baseline_notifies(self):
        detectMilestones(_FakeDb(plays=0), self.repo, self.USER)   #< first pass: baseline, nothing achieved
        self.assertEqual(self.repo.getUnseenMilestoneCount(self.USER), 0)

        detectMilestones(_FakeDb(plays=1500), self.repo, self.USER)
        self.assertEqual(self.repo.getUnseenMilestoneCount(self.USER), 1)
        self.assertTrue(self.repo.hasThresholdMilestone(self.USER, MILESTONE_KIND_PLAYS, 1000))

    def test_threshold_recorded_only_once(self):
        detectMilestones(_FakeDb(plays=0), self.repo, self.USER)
        detectMilestones(_FakeDb(plays=1500), self.repo, self.USER)
        detectMilestones(_FakeDb(plays=1500), self.repo, self.USER)   #< same value again
        playsRows = [r for r in self.repo.getMilestonesForUser(self.USER) if r["kind"] == MILESTONE_KIND_PLAYS]
        self.assertEqual(len(playsRows), 1)

    def test_top_artist_change_notifies_once(self):
        detectMilestones(_FakeDb(topArtist={"id": "a1", "name": "A"}), self.repo, self.USER)  # seeded seen
        self.assertEqual(self.repo.getUnseenMilestoneCount(self.USER), 0)

        detectMilestones(_FakeDb(topArtist={"id": "a2", "name": "B"}), self.repo, self.USER)
        self.assertEqual(self.repo.getUnseenMilestoneCount(self.USER), 1)

        # Same #1 again -> no new row (still just the seeded + the one change).
        detectMilestones(_FakeDb(topArtist={"id": "a2", "name": "B"}), self.repo, self.USER)
        topRows = [r for r in self.repo.getMilestonesForUser(self.USER) if r["kind"] == MILESTONE_KIND_TOP_ARTIST]
        self.assertEqual(len(topRows), 2)

    def test_no_top_artist_records_nothing(self):
        detectMilestones(_FakeDb(topArtist=None), self.repo, self.USER)
        self.assertIsNone(self.repo.getLatestMilestone(self.USER, MILESTONE_KIND_TOP_ARTIST))


class TestFormatMilestone(unittest.TestCase):
    def test_plays(self):
        out = formatMilestone({"kind": MILESTONE_KIND_PLAYS, "threshold": 10000})
        self.assertEqual(out["label"], "10,000 lifetime plays")
        self.assertNotIn("artistId", out)

    def test_listen_time(self):
        out = formatMilestone({"kind": MILESTONE_KIND_LISTEN_TIME, "threshold": 500})
        self.assertEqual(out["label"], "500 hours listened")

    def test_streak(self):
        out = formatMilestone({"kind": MILESTONE_KIND_STREAK, "threshold": 30})
        self.assertEqual(out["label"], "30-day listening streak")

    def test_top_artist(self):
        out = formatMilestone({"kind": MILESTONE_KIND_TOP_ARTIST, "threshold": 0,
                               "detail": json.dumps({"id": "art1", "name": "Radiohead"})})
        self.assertEqual(out["label"], "New #1 artist: Radiohead")
        self.assertEqual(out["artistId"], "art1")

    def test_top_artist_malformed_detail(self):
        out = formatMilestone({"kind": MILESTONE_KIND_TOP_ARTIST, "threshold": 0, "detail": "not json"})
        self.assertEqual(out["label"], "New #1 artist: Unknown artist")
        self.assertIsNone(out["artistId"])


if __name__ == "__main__":
    unittest.main()
