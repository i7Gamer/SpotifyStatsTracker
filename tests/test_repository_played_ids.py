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


if __name__ == "__main__":
    unittest.main()
