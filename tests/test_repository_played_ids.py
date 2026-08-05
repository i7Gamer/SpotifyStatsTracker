"""getPlayedTrackIds/getPlayedArtistIds/getPlayedAlbumIds - the batched
"does this user have any data for these ids" lookups the Compare page uses
to decide whether a counterpart's song/artist/album links to the viewer's
own detail page or out to Spotify (see app.py's _markLinkExternally). These
must match a real play-history check, not top-list membership: a track can
be genuinely played without ranking in anyone's top-N.
"""
import sys
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import Repository


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


class TestPlayedIds(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)

        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")

        # t1: a1 primary, album al1. t2: a2 primary, SAME album al1 (never
        # played - proves album credit comes from ANY track on it, not just
        # the specific track originally linked to that album id).
        self.repo.upsertTrack(_track("t1", ["a1"], "al1"))
        self.repo.upsertTrack(_track("t2", ["a2"], "al1"))
        # t3: a3 primary, a1 SECONDARY, album al2 - proves artist credit
        # isn't limited to the primary billing position.
        self.repo.upsertTrack(_track("t3", ["a3", "a1"], "al2"))
        # t4/al3/a4: never played anywhere - the "zero data" control.
        self.repo.upsertTrack(_track("t4", ["a4"], "al3"))
        # t5: played, but deliberately excluded from every query below - the
        # IN(...) filter must not leak plays for ids nobody asked about.
        self.repo.upsertTrack(_track("t5", ["a5"], "al4"))
        self.repo.commit()

        for trackId, playedAt in (("t1", 100), ("t3", 200), ("t5", 300)):
            self.repo.insertPlay("alice", trackId, playedAt, 60000)
        self.repo.insertPlay("bob", "t2", 400, 60000)   #< bob's plays must not count for alice
        self.repo.commit()


class TestGetPlayedTrackIds(TestPlayedIds):
    def test_returns_exactly_the_played_subset_of_the_queried_ids(self):
        result = self.repo.getPlayedTrackIds("alice", ["t1", "t2", "t3", "t4"])
        self.assertEqual(result, {"t1", "t3"})

    def test_ids_outside_the_query_list_are_never_returned(self):
        """t5 was played but isn't in the queried list - must not appear."""
        result = self.repo.getPlayedTrackIds("alice", ["t1", "t4"])
        self.assertEqual(result, {"t1"})

    def test_another_users_plays_do_not_count(self):
        result = self.repo.getPlayedTrackIds("alice", ["t2"])
        self.assertEqual(result, set())

    def test_empty_id_list_returns_empty_set(self):
        self.assertEqual(self.repo.getPlayedTrackIds("alice", []), set())

    def test_unknown_user_returns_empty_set(self):
        self.assertEqual(self.repo.getPlayedTrackIds("ghost", ["t1"]), set())


class TestGetPlayedArtistIds(TestPlayedIds):
    def test_credits_the_primary_artist_of_a_played_track(self):
        result = self.repo.getPlayedArtistIds("alice", ["a1", "a2", "a3", "a4"])
        self.assertIn("a1", result)
        self.assertIn("a3", result)

    def test_credits_a_secondary_billed_artist_too(self):
        """a1 is credited on t3 at position 1 (secondary), not just t1's
        primary billing - any credited position counts."""
        result = self.repo.getPlayedArtistIds("alice", ["a1"])
        self.assertEqual(result, {"a1"})

    def test_artist_with_no_played_track_is_excluded(self):
        result = self.repo.getPlayedArtistIds("alice", ["a2", "a4"])
        self.assertEqual(result, set())

    def test_empty_id_list_returns_empty_set(self):
        self.assertEqual(self.repo.getPlayedArtistIds("alice", []), set())


class TestGetPlayedAlbumIds(TestPlayedIds):
    def test_credits_an_album_via_any_track_on_it(self):
        """al1 holds t1 (played) and t2 (never played) - the album still
        counts as played because SOME track on it was."""
        result = self.repo.getPlayedAlbumIds("alice", ["al1", "al2", "al3"])
        self.assertEqual(result, {"al1", "al2"})

    def test_album_with_no_played_track_is_excluded(self):
        result = self.repo.getPlayedAlbumIds("alice", ["al3"])
        self.assertEqual(result, set())

    def test_empty_id_list_returns_empty_set(self):
        self.assertEqual(self.repo.getPlayedAlbumIds("alice", []), set())


class TestGetRecentlyRecordedTrackIds(unittest.TestCase):
    """Backs the listener's missed-play cross-check: unlike getPlayedTrackIds
    this one is time-bounded, because "played once last year" is no evidence
    that the play currently sitting in the connect-state queue was captured."""

    HOUR = 3600

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)

        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")
        for trackId in ("recent", "old", "skipped", "bobs"):
            self.repo.upsertTrack(_track(trackId, ["a1"], "al1"))
        self.repo.commit()

        now = time.time()
        self.repo.insertPlay("alice", "recent", now - self.HOUR, 60000)
        self.repo.insertPlay("alice", "old", now - 48 * self.HOUR, 60000)
        #< a skip is still a captured play - the question is "did we record it",
        #  not "did they listen through"
        self.repo.insertPlay("alice", "skipped", now - self.HOUR, 2000, is_skip=1)
        self.repo.insertPlay("bob", "bobs", now - self.HOUR, 60000)
        self.repo.commit()

    def _lookup(self, trackIds, sinceSeconds=6 * 3600):
        return self.repo.getRecentlyRecordedTrackIds("alice", trackIds, sinceSeconds)

    def test_returns_tracks_played_inside_the_window(self):
        self.assertEqual(self._lookup(["recent"]), {"recent"})

    def test_excludes_tracks_last_played_before_the_window(self):
        self.assertEqual(self._lookup(["old"]), set())

    def test_a_wider_window_reaches_the_older_play(self):
        self.assertEqual(self._lookup(["old"], sinceSeconds=72 * self.HOUR), {"old"})

    def test_skips_count_as_recorded(self):
        self.assertEqual(self._lookup(["skipped"]), {"skipped"})

    def test_another_users_plays_do_not_count(self):
        self.assertEqual(self._lookup(["bobs"]), set())

    def test_unplayed_track_is_absent(self):
        self.assertEqual(self._lookup(["never-seen"]), set())

    def test_batches_a_mixed_list_in_one_call(self):
        self.assertEqual(
            self._lookup(["recent", "old", "skipped", "bobs", "never-seen"]),
            {"recent", "skipped"},
        )

    def test_empty_id_list_returns_empty_set_without_querying(self):
        self.assertEqual(self._lookup([]), set())


class TestGetPlayTimesInRange(unittest.TestCase):
    """Backs the Web API backfill's dedup: the listener's in-memory caches only
    cover the current listener object's lifetime, so after a reconnect the
    database is the only thing that knows which of the last 50 Web API plays
    were already recorded."""

    HOUR = 3600

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)

        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")
        for trackId in ("t1", "t2", "t3", "t4"):
            self.repo.upsertTrack(_track(trackId, ["a1"], "al1"))
        self.repo.commit()

        self.base = 1_700_000_000.0
        self.repo.insertPlay("alice", "t1", self.base, 60000)
        self.repo.insertPlay("alice", "t2", self.base + 600, 2000, is_skip=1)  #< a skip is still recorded
        self.repo.insertPlay("alice", "t3", self.base + 10 * self.HOUR, 60000)  #< outside the window
        self.repo.insertPlay("bob", "t4", self.base + 60, 60000)
        self.repo.commit()

    def _lookup(self, startTs, endTs):
        return sorted(playedAt for _trackId, playedAt, _createdAt in self._lookupPairs(startTs, endTs))

    def _lookupPairs(self, startTs, endTs):
        return self.repo.getTrackPlayTimesInRange("alice", startTs, endTs)

    def _insertPlayCreatedAt(self, trackId, playedAt, createdAt, created_reason, is_skip=0):
        """A play whose insert-time stamp is controlled: created_at is stamped
        with time.time() inside insertPlay, so pin the clock for the call."""
        with patch("Database.queries.plays.time") as mockTime:
            mockTime.time.return_value = createdAt
            self.repo.insertPlay("alice", trackId, playedAt, 60000,
                                 created_reason=created_reason, is_skip=is_skip)
        self.repo.commit()

    def test_each_time_carries_the_track_it_belongs_to(self):
        """The backfill's dedup compares per track: a timestamp on its own let a
        recorded play of one track suppress a genuinely missing play of another
        (their played_at values sit seconds apart under gapless playback)."""
        self.assertEqual(sorted(self._lookupPairs(self.base - 10, self.base + 1200)),
                         [("t1", self.base, None), ("t2", self.base + 600, None)])

    def test_listener_rows_carry_their_created_at_as_the_observed_end(self):
        """The listener inserts a play at the track-change moment, so a listener
        row's created_at IS the observed end of the play, pauses included - the
        anchor the backfill's end-time dedup arm compares against."""
        self._insertPlayCreatedAt("t4", self.base + 300, self.base + 700,
                                  "listener_play (user: alice)")

        self.assertIn(("t4", self.base + 300, self.base + 700),
                      self._lookupPairs(self.base - 10, self.base + 1200))

    def test_non_listener_rows_never_carry_a_created_at(self):
        """An import row's created_at is the moment the import ran - unrelated
        to when the play ended - and a backfill row's is the poll moment. Only
        a listener row's insert time means "this play just finished"."""
        self._insertPlayCreatedAt("t4", self.base + 300, self.base + 700,
                                  "history_import (user: alice)")

        self.assertIn(("t4", self.base + 300, None),
                      self._lookupPairs(self.base - 10, self.base + 1200))

    def test_a_listener_row_starting_before_the_window_is_found_via_its_end(self):
        """A paused play's start can sit MORE than one track-length before its
        end - the exact case the end-time arm exists for - so the row must be
        findable by when it ended, not only by when it started."""
        self._insertPlayCreatedAt("t4", self.base - 5000, self.base + 100,
                                  "listener_play (user: alice)")

        self.assertIn(("t4", self.base - 5000, self.base + 100),
                      self._lookupPairs(self.base - 10, self.base + 1200))

    def test_a_listener_skip_row_never_anchors_an_end(self):
        """A skip's created_at is when the user skipped AWAY, not the end of a
        play the feed still owes us. Letting it anchor the caller's +/-10s
        end-time arm meant a skip could suppress the backfill of a real play -
        the user skips a track, the listener then dies and misses the full
        replay, and the Web API's start-reading of that replay lands within
        10s of the skip's stamp. The item is dropped before it ever reaches
        the insert guard, whose own is_skip=0 would have let it through, and
        every later poll collides identically: the play is lost for good.
        The three sibling implementations of this pairing rule all restrict
        to is_skip=0 (hasPlayNearTime, getPlaysWithSourceInRange, the sweep)."""
        self._insertPlayCreatedAt("t4", self.base + 300, self.base + 700,
                                  "listener_play (user: alice)", is_skip=1)

        self.assertIn(("t4", self.base + 300, None),
                      self._lookupPairs(self.base - 10, self.base + 1200))

    def test_a_skip_is_not_reached_into_the_window_by_its_created_at(self):
        """The reach-back arm exists to find a play whose END landed in the
        window; a skip row has no such end, so its stamp must not pull it in
        either. Its own played_at still counts wherever that falls."""
        self._insertPlayCreatedAt("t4", self.base - 5000, self.base + 100,
                                  "listener_play (user: alice)", is_skip=1)

        self.assertEqual([], [row for row in self._lookupPairs(self.base - 10, self.base + 1200)
                              if row[0] == "t4"])

    def test_returns_played_at_values_inside_the_window(self):
        self.assertEqual(self._lookup(self.base - 10, self.base + 1200), [self.base, self.base + 600])

    def test_bounds_are_inclusive(self):
        """The caller pads the window by exactly the dedup tolerance, so an
        exclusive bound would drop the play sitting on the edge."""
        self.assertEqual(self._lookup(self.base, self.base), [self.base])

    def test_skips_count_as_recorded(self):
        self.assertEqual(self._lookup(self.base + 600, self.base + 600), [self.base + 600])

    def test_plays_outside_the_window_are_excluded(self):
        self.assertEqual(self._lookup(self.base - 10, self.base + 60), [self.base])

    def test_another_users_plays_do_not_count(self):
        """bob's play sits inside the window - scoping it to alice is what keeps
        one account's history from suppressing another's backfill."""
        self.assertNotIn(self.base + 60, self._lookup(self.base - 10, self.base + 1200))

    def test_empty_window_returns_nothing(self):
        self.assertEqual(self._lookup(self.base + 2 * self.HOUR, self.base + 3 * self.HOUR), [])


if __name__ == "__main__":
    unittest.main()
