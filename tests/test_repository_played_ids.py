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
        return sorted(self.repo.getPlayTimesInRange("alice", startTs, endTs))

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
