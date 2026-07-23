"""Recalculating user_milestones.achieved_at from play history.

The 1.34.0 seeding pass stamped every already-achieved milestone with the
migration moment instead of the date the user actually reached it. The
recalculation (services/milestones.py recalculateMilestoneDates + the
MilestoneQueries lookups it drives, applied by migrate1_35_0) derives the real
dates from the plays table: the Nth non-skip play, the cumulative listen-time
crossing, the first-ever consecutive-day run, and the moment the current #1
artist last took the lead.

Exercised against a real temp Repository with real play rows (small thresholds
keep the fixtures tiny - recalculation reads each row's threshold, it doesn't
require the shipped MILESTONE_*_THRESHOLDS values).
"""
import os
import sys
import datetime
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import Repository
from Database.queries._base import PLAY_BUCKET_SECONDS
from services.milestones import (
    recalculateMilestoneDates, computeStreakAchievedTimestamps,
    computeTopArtistTakeover, resolveUserTimezone,
    MILESTONE_KIND_PLAYS, MILESTONE_KIND_LISTEN_TIME, MILESTONE_KIND_STREAK,
    MILESTONE_KIND_TOP_ARTIST, MS_PER_HOUR,
)

DAY_SECONDS = 86400
HALF_HOUR_MS = 30 * 60 * 1000
UTC = datetime.timezone.utc

# Stand-in for the wrong seeded date every fixture milestone row starts with.
SEEDED_AT = 9_999_999_999.0


def _track(trackId, artistIds, albumId):
    """artistIds in credited order (position 0 = primary)."""
    return {
        "id": trackId,
        "name": f"Track {trackId}",
        "url": f"http://example.com/track/{trackId}",
        "artists": [
            {"id": aid, "name": f"Artist {aid}", "url": f"http://example.com/artist/{aid}",
             "imageUrl": "", "imageId": aid}
            for aid in artistIds
        ],
        "album": {
            "id": albumId, "name": f"Album {albumId}", "url": f"http://example.com/album/{albumId}",
            "imageId": albumId, "imageUrl": "", "totalTracks": 10, "releaseDate": 0.0,
        },
        "imageUrl": "", "imageId": albumId, "duration": 200000, "explicit": False,
        "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0.0,
    }


class _RecalcTestCase(unittest.TestCase):
    USER = "alice"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "recalc.db")
        self.addCleanup(self.repo.connectionManager.close)
        self.repo.upsertUser(self.USER, f"{self.USER}@example.com", createdAt=100.0)
        self.repo.upsertTrack(_track("t1", ["a1"], "al1"))
        self.repo.upsertTrack(_track("t2", ["a2"], "al1"))

    def _play(self, playedAt, timePlayed=60000, trackId="t1", isSkip=0):
        self.repo.insertPlay(self.USER, trackId, playedAt, timePlayed, is_skip=isSkip)

    def _milestone(self, kind, threshold, detail=None, seen=True):
        return self.repo.recordMilestone(self.USER, kind, threshold, detail, SEEDED_AT, seen)

    def _achievedAt(self, milestoneId):
        rows = self.repo.getMilestonesForUser(self.USER)
        return next(r["achieved_at"] for r in rows if r["id"] == milestoneId)


class TestNthPlayTimestamp(_RecalcTestCase):
    def setUp(self):
        super().setUp()
        for ts in (100.0, 200.0, 300.0):
            self._play(ts)

    def test_returns_nth_play_in_chronological_order(self):
        self.assertEqual(self.repo.getNthPlayTimestamp(self.USER, 1), 100.0)
        self.assertEqual(self.repo.getNthPlayTimestamp(self.USER, 3), 300.0)

    def test_none_when_fewer_plays_than_n(self):
        self.assertIsNone(self.repo.getNthPlayTimestamp(self.USER, 4))

    def test_none_for_non_positive_n(self):
        self.assertIsNone(self.repo.getNthPlayTimestamp(self.USER, 0))

    def test_skips_are_excluded(self):
        self._play(150.0, isSkip=1)
        self.assertEqual(self.repo.getNthPlayTimestamp(self.USER, 2), 200.0)

    def test_other_users_plays_do_not_count(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.insertPlay("bob", "t1", 50.0, 60000)
        self.assertEqual(self.repo.getNthPlayTimestamp(self.USER, 1), 100.0)


class TestListenTimeCrossingTimestamp(_RecalcTestCase):
    def setUp(self):
        super().setUp()
        for ts in (100.0, 200.0, 300.0):
            self._play(ts, timePlayed=HALF_HOUR_MS)

    def test_crossing_mid_history(self):
        # Cumulative 30/60/90 minutes: one hour is complete exactly at the
        # second play (>= comparison, matching detection's floor-division).
        self.assertEqual(self.repo.getListenTimeCrossingTimestamp(self.USER, MS_PER_HOUR), 200.0)

    def test_tiny_target_crosses_at_first_play(self):
        self.assertEqual(self.repo.getListenTimeCrossingTimestamp(self.USER, 1), 100.0)

    def test_none_when_total_below_target(self):
        self.assertIsNone(self.repo.getListenTimeCrossingTimestamp(self.USER, 2 * MS_PER_HOUR))

    def test_skips_are_excluded(self):
        self._play(150.0, timePlayed=10 * MS_PER_HOUR, isSkip=1)
        self.assertEqual(self.repo.getListenTimeCrossingTimestamp(self.USER, MS_PER_HOUR), 200.0)


class TestMilestoneRowHelpers(_RecalcTestCase):
    def test_update_achieved_at_preserves_seen(self):
        milestoneId = self.repo.recordMilestone(self.USER, MILESTONE_KIND_PLAYS, 1000, None, SEEDED_AT, seen=False)
        self.repo.updateMilestoneAchievedAt(milestoneId, 1234.5)

        row = next(r for r in self.repo.getMilestonesForUser(self.USER) if r["id"] == milestoneId)
        self.assertEqual(row["achieved_at"], 1234.5)
        self.assertEqual(row["seen"], 0)

    def test_milestone_usernames_distinct_and_sorted(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.upsertUser("carol", "carol@example.com")   #< no milestones - must not appear
        self._milestone(MILESTONE_KIND_PLAYS, 1000)
        self._milestone(MILESTONE_KIND_PLAYS, 5000)
        self.repo.recordMilestone("bob", MILESTONE_KIND_PLAYS, 1000, None, 1.0, seen=True)
        self.assertEqual(self.repo.getMilestoneUsernames(), ["alice", "bob"])

    def test_milestone_usernames_empty_table(self):
        self.assertEqual(self.repo.getMilestoneUsernames(), [])

    def test_delete_milestone_removes_only_that_row(self):
        goneId = self._milestone(MILESTONE_KIND_PLAYS, 1000)
        keptId = self._milestone(MILESTONE_KIND_PLAYS, 5000)
        self.repo.deleteMilestone(goneId)
        self.assertEqual({r["id"] for r in self.repo.getMilestonesForUser(self.USER)}, {keptId})


class TestRecalculateThresholdKinds(_RecalcTestCase):
    def setUp(self):
        super().setUp()
        for ts in (100.0, 200.0, 300.0):
            self._play(ts, timePlayed=HALF_HOUR_MS)

    def test_plays_row_moved_to_nth_play(self):
        milestoneId = self._milestone(MILESTONE_KIND_PLAYS, 2)
        updated = recalculateMilestoneDates(self.repo, self.USER, UTC)
        self.assertEqual(updated, 1)
        self.assertEqual(self._achievedAt(milestoneId), 200.0)

    def test_listen_time_row_moved_to_cumulative_crossing(self):
        milestoneId = self._milestone(MILESTONE_KIND_LISTEN_TIME, 1)
        recalculateMilestoneDates(self.repo, self.USER, UTC)
        self.assertEqual(self._achievedAt(milestoneId), 200.0)

    def test_unsupported_threshold_left_unchanged(self):
        # Data can shrink after seeding (overwrite import, stricter skip
        # threshold) - a row today's plays can't justify keeps its old date
        # rather than getting a guessed one.
        playsId = self._milestone(MILESTONE_KIND_PLAYS, 10)
        listenId = self._milestone(MILESTONE_KIND_LISTEN_TIME, 2)
        updated = recalculateMilestoneDates(self.repo, self.USER, UTC)
        self.assertEqual(updated, 0)
        self.assertEqual(self._achievedAt(playsId), SEEDED_AT)
        self.assertEqual(self._achievedAt(listenId), SEEDED_AT)

    def test_seen_flags_survive_recalculation(self):
        seenId = self._milestone(MILESTONE_KIND_PLAYS, 1, seen=True)
        unseenId = self._milestone(MILESTONE_KIND_PLAYS, 2, seen=False)
        recalculateMilestoneDates(self.repo, self.USER, UTC)
        rows = {r["id"]: r for r in self.repo.getMilestonesForUser(self.USER)}
        self.assertEqual(rows[seenId]["seen"], 1)
        self.assertEqual(rows[unseenId]["seen"], 0)

    def test_second_run_is_a_no_op(self):
        self._milestone(MILESTONE_KIND_PLAYS, 2)
        self.assertEqual(recalculateMilestoneDates(self.repo, self.USER, UTC), 1)
        self.assertEqual(recalculateMilestoneDates(self.repo, self.USER, UTC), 0)

    def test_user_without_milestones_is_a_no_op(self):
        self.assertEqual(recalculateMilestoneDates(self.repo, self.USER, UTC), 0)


class TestRecalculateStreak(_RecalcTestCase):
    def _dayPlay(self, day, offsetSeconds=3600):
        """One play on UTC day `day` (offset must keep it bucket-aligned so the
        expected achieved timestamp is exactly the bucket start)."""
        ts = float(day * DAY_SECONDS + offsetSeconds)
        assert ts % PLAY_BUCKET_SECONDS == 0
        self._play(ts)
        return ts

    def test_streak_row_moved_to_first_ever_run(self):
        # Days 10-11 form the FIRST 2-day run; days 20-22 a later 3-day run.
        # The 2-day threshold must land on the earlier run, not the later one.
        self._dayPlay(10)
        firstRunSecondDay = self._dayPlay(11)
        self._dayPlay(20)
        self._dayPlay(21)
        thirdRunDay = self._dayPlay(22)
        twoDayId = self._milestone(MILESTONE_KIND_STREAK, 2)
        threeDayId = self._milestone(MILESTONE_KIND_STREAK, 3)

        recalculateMilestoneDates(self.repo, self.USER, UTC)

        self.assertEqual(self._achievedAt(twoDayId), firstRunSecondDay)
        self.assertEqual(self._achievedAt(threeDayId), thirdRunDay)

    def test_streak_longer_than_any_run_left_unchanged(self):
        self._dayPlay(10)
        self._dayPlay(11)
        milestoneId = self._milestone(MILESTONE_KIND_STREAK, 7)
        self.assertEqual(recalculateMilestoneDates(self.repo, self.USER, UTC), 0)
        self.assertEqual(self._achievedAt(milestoneId), SEEDED_AT)

    def test_day_boundaries_follow_the_user_timezone(self):
        # Both plays fall on UTC day 100, but at UTC+2 the 23:30 play belongs
        # to the next local day - so the 2-day streak only exists there.
        morning = float(100 * DAY_SECONDS + 36000)   #< 10:00 UTC
        lateNight = float(100 * DAY_SECONDS + 84600)  #< 23:30 UTC = 01:30 local (+2)
        assert morning % PLAY_BUCKET_SECONDS == 0 and lateNight % PLAY_BUCKET_SECONDS == 0
        self._play(morning)
        self._play(lateNight)
        milestoneId = self._milestone(MILESTONE_KIND_STREAK, 2)

        self.assertEqual(recalculateMilestoneDates(self.repo, self.USER, UTC), 0)

        plusTwo = datetime.timezone(datetime.timedelta(hours=2))
        self.assertEqual(recalculateMilestoneDates(self.repo, self.USER, plusTwo), 1)
        self.assertEqual(self._achievedAt(milestoneId), lateNight)


class TestRecalculateTopArtist(_RecalcTestCase):
    A1_BUCKET = float(1000 * PLAY_BUCKET_SECONDS)
    A2_BUCKET = float(2000 * PLAY_BUCKET_SECONDS)
    A1_REGAIN_BUCKET = float(3000 * PLAY_BUCKET_SECONDS)

    def _detail(self, artistId):
        return json.dumps({"id": artistId, "name": f"Artist {artistId}"})

    def _leadChange(self):
        """a1 leads with 1 play, then a2 overtakes with 2 plays a bucket later."""
        self._play(self.A1_BUCKET, trackId="t1")
        self._play(self.A2_BUCKET, trackId="t2")
        self._play(self.A2_BUCKET + 300, trackId="t2")

    def test_single_row_moved_to_takeover_moment(self):
        self._leadChange()
        milestoneId = self._milestone(MILESTONE_KIND_TOP_ARTIST, 0, detail=self._detail("a2"))
        self.assertEqual(recalculateMilestoneDates(self.repo, self.USER, UTC), 1)
        self.assertEqual(self._achievedAt(milestoneId), self.A2_BUCKET)

    def test_regained_lead_uses_the_last_takeover(self):
        self._leadChange()
        self._play(self.A1_REGAIN_BUCKET, trackId="t1")
        self._play(self.A1_REGAIN_BUCKET + 300, trackId="t1")   #< a1 back to 3 > 2
        milestoneId = self._milestone(MILESTONE_KIND_TOP_ARTIST, 0, detail=self._detail("a1"))
        recalculateMilestoneDates(self.repo, self.USER, UTC)
        self.assertEqual(self._achievedAt(milestoneId), self.A1_REGAIN_BUCKET)

    def test_detail_mismatching_scan_leader_left_unchanged(self):
        # Tie-ordering / same-name merges can make getTopArtists disagree with
        # the id-based scan - then the safe answer is to not touch the row.
        self._leadChange()
        milestoneId = self._milestone(MILESTONE_KIND_TOP_ARTIST, 0, detail=self._detail("a1"))
        self.assertEqual(recalculateMilestoneDates(self.repo, self.USER, UTC), 0)
        self.assertEqual(self._achievedAt(milestoneId), SEEDED_AT)

    def test_multiple_top_artist_rows_left_unchanged(self):
        # With organic #1 changes on record, rewriting the latest row's date
        # could re-order getLatestMilestone and re-trigger a "new #1"
        # notification - only the lone seeded row is safe to move.
        self._leadChange()
        firstId = self._milestone(MILESTONE_KIND_TOP_ARTIST, 0, detail=self._detail("a1"))
        secondId = self._milestone(MILESTONE_KIND_TOP_ARTIST, 0, detail=self._detail("a2"))
        self.assertEqual(recalculateMilestoneDates(self.repo, self.USER, UTC), 0)
        self.assertEqual(self._achievedAt(firstId), SEEDED_AT)
        self.assertEqual(self._achievedAt(secondId), SEEDED_AT)

    def test_malformed_detail_left_unchanged(self):
        self._leadChange()
        milestoneId = self._milestone(MILESTONE_KIND_TOP_ARTIST, 0, detail="not json")
        self.assertEqual(recalculateMilestoneDates(self.repo, self.USER, UTC), 0)
        self.assertEqual(self._achievedAt(milestoneId), SEEDED_AT)


class TestRemoveUnsupportedRows(_RecalcTestCase):
    """removeUnsupported=True - the settled pass after an overwrite import:
    rows whose threshold the rewritten history can no longer reach are
    deleted, so the Milestones section reflects the data that actually
    exists. Organic recalculation passes keep the default (False) so e.g. a
    tightened skip threshold never deletes rows only to re-notify them when
    it's loosened again."""

    def setUp(self):
        super().setUp()
        for ts in (100.0, 200.0, 300.0):
            self._play(ts, timePlayed=HALF_HOUR_MS)

    def test_unreachable_threshold_rows_are_removed(self):
        keptId = self._milestone(MILESTONE_KIND_PLAYS, 2)
        self._milestone(MILESTONE_KIND_PLAYS, 10)          #< only 3 plays exist
        self._milestone(MILESTONE_KIND_LISTEN_TIME, 2)     #< only 1.5h exists
        self._milestone(MILESTONE_KIND_STREAK, 2)          #< all plays on one day

        changed = recalculateMilestoneDates(self.repo, self.USER, UTC, removeUnsupported=True)

        self.assertEqual(changed, 4)   #< 1 date update + 3 removals
        self.assertEqual({r["id"] for r in self.repo.getMilestonesForUser(self.USER)}, {keptId})
        self.assertEqual(self._achievedAt(keptId), 200.0)   #< supported rows still get re-dated

    def test_default_keeps_unreachable_rows(self):
        keptId = self._milestone(MILESTONE_KIND_PLAYS, 10)
        recalculateMilestoneDates(self.repo, self.USER, UTC)
        self.assertEqual({r["id"] for r in self.repo.getMilestonesForUser(self.USER)}, {keptId})
        self.assertEqual(self._achievedAt(keptId), SEEDED_AT)

    def test_top_artist_with_plays_is_never_removed(self):
        # A detail id the scan disagrees with is ambiguity (tie order,
        # same-name merges), not proof the data doesn't support it.
        rowId = self._milestone(MILESTONE_KIND_TOP_ARTIST, 0,
                                detail=json.dumps({"id": "zz", "name": "Nobody"}))
        recalculateMilestoneDates(self.repo, self.USER, UTC, removeUnsupported=True)
        self.assertIn(rowId, {r["id"] for r in self.repo.getMilestonesForUser(self.USER)})


class TestRemoveUnsupportedWithoutPlays(_RecalcTestCase):
    """A wiped history (overwrite import that removed everything) supports no
    milestone of any kind - with removeUnsupported every row goes, including
    top_artist rows (nobody can be #1 of an empty history)."""

    def test_all_rows_removed_when_history_is_empty(self):
        self._milestone(MILESTONE_KIND_PLAYS, 2)
        self._milestone(MILESTONE_KIND_STREAK, 2)
        self._milestone(MILESTONE_KIND_TOP_ARTIST, 0, detail=json.dumps({"id": "a1", "name": "A"}))
        self._milestone(MILESTONE_KIND_TOP_ARTIST, 0, detail=json.dumps({"id": "a2", "name": "B"}))

        changed = recalculateMilestoneDates(self.repo, self.USER, UTC, removeUnsupported=True)

        self.assertEqual(changed, 4)
        self.assertEqual(self.repo.getMilestonesForUser(self.USER), [])

    def test_default_keeps_rows_when_history_is_empty(self):
        self._milestone(MILESTONE_KIND_PLAYS, 2)
        self._milestone(MILESTONE_KIND_TOP_ARTIST, 0, detail=json.dumps({"id": "a1", "name": "A"}))
        self.assertEqual(recalculateMilestoneDates(self.repo, self.USER, UTC), 0)
        self.assertEqual(len(self.repo.getMilestonesForUser(self.USER)), 2)


class TestComputeStreakAchievedTimestamps(unittest.TestCase):
    def test_thresholds_land_on_their_exact_days(self):
        dayFirstTs = {
            "2024-01-01": 100.0, "2024-01-02": 200.0,
            "2024-01-04": 300.0, "2024-01-05": 400.0, "2024-01-06": 500.0,
        }
        out = computeStreakAchievedTimestamps(dayFirstTs, {2, 3})
        self.assertEqual(out, {2: 200.0, 3: 500.0})

    def test_first_occurrence_wins(self):
        dayFirstTs = {
            "2024-01-01": 100.0, "2024-01-02": 200.0,
            "2024-02-01": 300.0, "2024-02-02": 400.0,
        }
        self.assertEqual(computeStreakAchievedTimestamps(dayFirstTs, {2}), {2: 200.0})

    def test_unreached_thresholds_absent(self):
        self.assertEqual(computeStreakAchievedTimestamps({"2024-01-01": 100.0}, {2}), {})

    def test_empty_inputs(self):
        self.assertEqual(computeStreakAchievedTimestamps({}, {2}), {})
        self.assertEqual(computeStreakAchievedTimestamps({"2024-01-01": 1.0}, set()), {})


class TestComputeTopArtistTakeover(unittest.TestCase):
    def _row(self, bucketTs, artistId, plays):
        return {"bucketStartTs": bucketTs, "artistId": artistId,
                "artistName": f"Artist {artistId}", "plays": plays}

    def test_simple_takeover(self):
        rows = [self._row(900.0, "a1", 1), self._row(1800.0, "a2", 2)]
        self.assertEqual(computeTopArtistTakeover(rows), ("a2", 1800.0))

    def test_tie_does_not_displace_the_leader(self):
        rows = [self._row(900.0, "a1", 2), self._row(1800.0, "a2", 2)]
        self.assertEqual(computeTopArtistTakeover(rows), ("a1", 900.0))

    def test_regain_reports_last_takeover(self):
        rows = [self._row(900.0, "a1", 1), self._row(1800.0, "a2", 2),
                self._row(2700.0, "a1", 2)]   #< a1 back to 3 > 2
        self.assertEqual(computeTopArtistTakeover(rows), ("a1", 2700.0))

    def test_same_bucket_increments_apply_together(self):
        # Both artists move within one bucket: the leader check runs on the
        # bucket's combined result, deterministically.
        rows = [self._row(900.0, "a1", 1), self._row(900.0, "a2", 3)]
        self.assertEqual(computeTopArtistTakeover(rows), ("a2", 900.0))

    def test_empty_rows(self):
        self.assertIsNone(computeTopArtistTakeover([]))


class TestResolveUserTimezone(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "tz.db")
        self.addCleanup(self.repo.connectionManager.close)
        self.repo.upsertUser("alice", "alice@example.com")

    def test_uses_the_stored_iana_zone(self):
        self.repo.updateUserSettings("alice", "day", "Europe/Berlin")
        tz = resolveUserTimezone(self.repo, "alice")
        self.assertEqual(str(tz), "Europe/Berlin")

    def test_falls_back_without_a_stored_zone(self):
        tz = resolveUserTimezone(self.repo, "alice")
        self.assertIsInstance(tz, datetime.tzinfo)

    def test_falls_back_on_an_invalid_zone(self):
        self.repo.updateUserSettings("alice", "day", "Not/AZone")
        tz = resolveUserTimezone(self.repo, "alice")
        self.assertIsInstance(tz, datetime.tzinfo)


if __name__ == "__main__":
    unittest.main()
