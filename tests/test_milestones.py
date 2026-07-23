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
    nextMilestoneProgress, buildNextMilestones,
    MILESTONE_KIND_PLAYS, MILESTONE_KIND_LISTEN_TIME, MILESTONE_KIND_STREAK, MILESTONE_KIND_TOP_ARTIST,
    MILESTONE_PLAYS_THRESHOLDS, MILESTONE_STREAK_DAY_THRESHOLDS,
    MS_PER_HOUR,
)


class _FakeDb:
    """Stand-in for a Database returning controlled stat values, so detection
    can be tested without inserting thousands of play rows."""

    def __init__(self, plays=0, ms=0, streakDays=0, topArtist=None):
        self._plays = plays
        self._ms = ms
        self._streakDays = streakDays
        self._topArtist = topArtist
        # Call counters so tests can assert the idle-cycle short-circuit skips
        # the heavier streak/top-artist queries (see detectMilestones).
        self.playTotalsCalls = 0
        self.streakCalls = 0
        self.topArtistCalls = 0

    def getPlayTotals(self, start, end):
        self.playTotalsCalls += 1
        return (self._plays, self._ms)

    def getCurrentStreak(self):
        self.streakCalls += 1
        return {"days": self._streakDays, "activeToday": True}

    def getTopArtists(self, startDate=None, endDate=None, by="plays", limit=None):
        self.topArtistCalls += 1
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
        db = _FakeDb(plays=12000, ms=300 * MS_PER_HOUR, streakDays=40,
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


class TestDetectMilestonesMarkSeen(_RepoTestCase):
    """markSeen records a pass's crossings as already seen - the post-import
    backfill case (see _detectMilestonesSafely in app.py): crossings surfaced
    by imported history are past achievements and get the same
    no-notification contract as first-pass seeding."""

    def test_marked_threshold_crossings_do_not_notify(self):
        detectMilestones(_FakeDb(plays=0), self.repo, self.USER)   #< baseline, nothing achieved

        recorded = detectMilestones(_FakeDb(plays=1500), self.repo, self.USER, markSeen=True)

        self.assertEqual(recorded, 1)
        self.assertEqual(self.repo.getUnseenMilestoneCount(self.USER), 0)
        self.assertTrue(self.repo.hasThresholdMilestone(self.USER, MILESTONE_KIND_PLAYS, 1000))

    def test_marked_top_artist_change_does_not_notify(self):
        detectMilestones(_FakeDb(topArtist={"id": "a1", "name": "A"}), self.repo, self.USER)

        detectMilestones(_FakeDb(topArtist={"id": "a2", "name": "B"}), self.repo, self.USER, markSeen=True)

        self.assertEqual(self.repo.getUnseenMilestoneCount(self.USER), 0)
        latest = self.repo.getLatestMilestone(self.USER, MILESTONE_KIND_TOP_ARTIST)
        self.assertEqual(json.loads(latest["detail"])["id"], "a2")   #< still recorded

    def test_later_organic_crossing_still_notifies(self):
        detectMilestones(_FakeDb(plays=0), self.repo, self.USER)
        detectMilestones(_FakeDb(plays=1500), self.repo, self.USER, markSeen=True)

        detectMilestones(_FakeDb(plays=5500), self.repo, self.USER)   #< back to the default

        self.assertEqual(self.repo.getUnseenMilestoneCount(self.USER), 1)


class TestDetectMilestonesChangeCache(_RepoTestCase):
    """The optional changeCache lets the periodic background pass skip the
    heavier streak + top-artist queries on cycles where the user's play totals
    are unchanged - every milestone kind derives from the plays table, so
    unchanged (count, listen-time) totals mean nothing can have crossed."""

    def test_unchanged_totals_skip_heavy_queries_on_next_pass(self):
        cache: dict = {}
        db = _FakeDb(plays=1500, ms=5 * MS_PER_HOUR, streakDays=10,
                     topArtist={"id": "a1", "name": "A"})

        detectMilestones(db, self.repo, self.USER, changeCache=cache)   #< first pass runs fully
        self.assertEqual((db.streakCalls, db.topArtistCalls), (1, 1))

        recorded = detectMilestones(db, self.repo, self.USER, changeCache=cache)

        self.assertEqual(recorded, 0)
        self.assertEqual(db.playTotalsCalls, 2)          #< the change signal still runs each pass
        self.assertEqual((db.streakCalls, db.topArtistCalls), (1, 1))   #< but the heavy pair did not

    def test_changed_totals_run_full_detection_and_notify(self):
        cache: dict = {}
        detectMilestones(_FakeDb(plays=0), self.repo, self.USER, changeCache=cache)   #< baseline, nothing achieved

        db = _FakeDb(plays=1500, streakDays=0)
        recorded = detectMilestones(db, self.repo, self.USER, changeCache=cache)

        self.assertEqual(recorded, 1)
        self.assertEqual((db.streakCalls, db.topArtistCalls), (1, 1))   #< heavy queries ran this pass
        self.assertEqual(self.repo.getUnseenMilestoneCount(self.USER), 1)

    def test_first_pass_never_short_circuits_even_with_prefilled_cache(self):
        # A stale cache entry (e.g. from a previous process) must not suppress
        # the one-time baseline seeding pass.
        cache = {self.USER: (1500, 0)}
        db = _FakeDb(plays=1500, ms=0, streakDays=0)

        detectMilestones(db, self.repo, self.USER, changeCache=cache)

        self.assertEqual((db.streakCalls, db.topArtistCalls), (1, 1))
        self.assertIsNotNone(self.repo.getMilestoneBaselineAt(self.USER))
        self.assertTrue(self.repo.hasThresholdMilestone(self.USER, MILESTONE_KIND_PLAYS, 1000))

    def test_without_cache_every_pass_runs_full(self):
        # Backward compatibility: callers that pass no cache keep the old
        # always-run behavior.
        db = _FakeDb(plays=1500)
        detectMilestones(db, self.repo, self.USER)
        detectMilestones(db, self.repo, self.USER)
        self.assertEqual((db.streakCalls, db.topArtistCalls), (2, 2))

    def test_cache_updates_when_totals_change_so_new_plateau_short_circuits(self):
        cache: dict = {}
        detectMilestones(_FakeDb(plays=0), self.repo, self.USER, changeCache=cache)

        detectMilestones(_FakeDb(plays=1500), self.repo, self.USER, changeCache=cache)   #< totals moved
        db = _FakeDb(plays=1500)
        recorded = detectMilestones(db, self.repo, self.USER, changeCache=cache)         #< now steady again

        self.assertEqual(recorded, 0)
        self.assertEqual((db.streakCalls, db.topArtistCalls), (0, 0))


class TestNextMilestoneProgress(unittest.TestCase):
    def test_returns_next_unreached_threshold(self):
        prog = nextMilestoneProgress(820, MILESTONE_PLAYS_THRESHOLDS)
        self.assertEqual(prog["target"], 1000)
        self.assertEqual(prog["current"], 820)
        self.assertEqual(prog["remaining"], 180)
        self.assertEqual(prog["percent"], 82)

    def test_skips_already_reached_thresholds(self):
        # 6000 plays: 1000 and 5000 are done, next is 10000.
        prog = nextMilestoneProgress(6000, MILESTONE_PLAYS_THRESHOLDS)
        self.assertEqual(prog["target"], 10000)
        self.assertEqual(prog["remaining"], 4000)

    def test_exactly_on_a_threshold_targets_the_next_one(self):
        # Reaching a threshold means it's done - progress points at the one after.
        prog = nextMilestoneProgress(1000, MILESTONE_PLAYS_THRESHOLDS)
        self.assertEqual(prog["target"], 5000)

    def test_none_when_every_threshold_reached(self):
        self.assertIsNone(nextMilestoneProgress(2_000_000, MILESTONE_PLAYS_THRESHOLDS))

    def test_zero_value_targets_first_threshold(self):
        prog = nextMilestoneProgress(0, MILESTONE_STREAK_DAY_THRESHOLDS)
        self.assertEqual(prog["target"], 7)
        self.assertEqual(prog["percent"], 0)


class TestBuildNextMilestones(unittest.TestCase):
    def test_one_entry_per_kind_in_order(self):
        items = buildNextMilestones(totalPlays=820, totalHours=87, streakDays=5)
        self.assertEqual([i["kind"] for i in items],
                         [MILESTONE_KIND_PLAYS, MILESTONE_KIND_LISTEN_TIME, MILESTONE_KIND_STREAK])
        plays = items[0]
        self.assertEqual((plays["current"], plays["target"], plays["remaining"]), (820, 1000, 180))
        self.assertTrue(plays["icon"] and plays["label"])

    def test_maxed_kind_is_omitted(self):
        # Streak past its final threshold drops out; plays/hours still climbing.
        items = buildNextMilestones(totalPlays=100, totalHours=1, streakDays=99999)
        kinds = [i["kind"] for i in items]
        self.assertIn(MILESTONE_KIND_PLAYS, kinds)
        self.assertNotIn(MILESTONE_KIND_STREAK, kinds)

    def test_all_maxed_returns_empty(self):
        self.assertEqual(buildNextMilestones(9_000_000, 999_999, 999_999), [])


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
