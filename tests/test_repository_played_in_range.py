"""getEntitiesPlayedInRange - "which of these entities did this user play at
all, in this window", for the Top lists' rank-movement badge.

The badge's hardest claim is "new". Placing an entry needs its rank in the
previous period, which the movement endpoint reads from a bounded scan of that
period's top PREVIOUS_WINDOW_SCAN_LIMIT - and absence from a scan that hit its
limit means "we did not look far enough", not "it was not there". A year of one
person's listening is thousands of entries deep, so on any range past a few
months that scan can never be complete, and "new" could never be claimed.

This answers the question the scan cannot: existence, exactly, for the <=50
entries actually on the page. It is deliberately NOT the ranking query narrowed
down - narrowing that on the joined table drops it to a full window scan for
artists and albums (measured: 12ms and 90ms), because the entity id cannot
reach the plays index. Driving from the entity side instead lets every kind
seek into plays' primary key (username, track_id, played_at): 0.5ms, 2.5ms and
23ms for a year of the reference library, against 11-75ms for the aggregate it
replaces.

No search or tag filter: both decide WHICH entities are listed, and the caller
passes ids that already survived them. fullPlaysOnly is different - it decides
whether a play counts as a listen at all, so a period whose plays were all
partial genuinely did not hear the track."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import Repository

_DURATION_MS = 200_000
_MARCH = 1772452800.0     #< 2026-03-02 12:00 UTC
_FEBRUARY = 1770033600.0  #< 2026-02-02 12:00 UTC


def _track(trackId, artistIds, albumId):
    return {
        "id": trackId, "name": f"Track {trackId}", "url": "",
        "artists": [{"id": aid, "name": f"Artist {aid}", "url": "", "imageUrl": "", "imageId": aid}
                    for aid in artistIds],
        "album": {"id": albumId, "name": f"Album {albumId}", "url": "", "imageId": albumId,
                  "imageUrl": "", "totalTracks": 10, "releaseDate": 0.0},
        "imageUrl": "", "imageId": albumId, "duration": _DURATION_MS, "explicit": False,
        "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0.0,
    }


class PlayedInRangeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")

        #< t1 and t2 share album al1, so an album counts a play of ANY of its
        #  tracks; t2 also credits a1 in second position, so artist credit is
        #  not limited to the primary billing
        self.repo.upsertTrack(_track("t1", ["a1"], "al1"))
        self.repo.upsertTrack(_track("t2", ["a2", "a1"], "al1"))
        self.repo.upsertTrack(_track("t3", ["a3"], "al2"))
        self.repo.commit()

    def _play(self, username, trackId, at, timePlayed=_DURATION_MS, isSkip=0):
        self.repo.insertPlay(username, trackId, at, timePlayed, is_skip=isSkip)
        self.repo.commit()

    def _played(self, kind, ids, start=_FEBRUARY - 86400, end=_FEBRUARY + 86400,
                fullPlaysOnly=False, username="alice"):
        return set(self.repo.getEntitiesPlayedInRange(username, kind, ids, start, end,
                                                      fullPlaysOnly=fullPlaysOnly))


class TestWhatCountsAsPlayed(PlayedInRangeTestCase):
    def test_a_track_played_in_the_window_is_reported(self):
        self._play("alice", "t1", _FEBRUARY)

        self.assertEqual(self._played("track", ["t1", "t3"]), {"t1"})

    def test_a_play_outside_the_window_is_not(self):
        self._play("alice", "t1", _MARCH)

        self.assertEqual(self._played("track", ["t1"]), set())

    def test_another_users_play_is_not(self):
        self._play("bob", "t1", _FEBRUARY)

        self.assertEqual(self._played("track", ["t1"]), set())

    def test_a_skip_is_not_a_listen(self):
        """The ranking these badges compare against counts real plays only, so
        an entry whose previous period was all skips is honestly new."""
        self._play("alice", "t1", _FEBRUARY, isSkip=1)

        self.assertEqual(self._played("track", ["t1"]), set())

    def test_an_artist_counts_a_play_of_any_of_their_tracks(self):
        self._play("alice", "t2", _FEBRUARY)   #< credits a2 primary, a1 second

        self.assertEqual(self._played("artist", ["a1", "a2", "a3"]), {"a1", "a2"})

    def test_an_album_counts_a_play_of_any_track_on_it(self):
        self._play("alice", "t2", _FEBRUARY)   #< t2 is on al1, like t1

        self.assertEqual(self._played("album", ["al1", "al2"]), {"al1"})

    def test_the_window_is_half_open_like_every_other_range_here(self):
        self._play("alice", "t1", _FEBRUARY)
        self._play("alice", "t3", _FEBRUARY + 100)

        self.assertEqual(self._played("track", ["t1", "t3"], start=_FEBRUARY,
                                      end=_FEBRUARY + 100), {"t1"})


class TestTheFullPlaysFilter(PlayedInRangeTestCase):
    def test_a_partial_play_does_not_count_when_full_plays_only_is_on(self):
        """The page ranks by full listens, so the badge has to agree with it -
        otherwise an entry the list treats as unheard last month is reported as
        merely having moved."""
        self._play("alice", "t1", _FEBRUARY, timePlayed=1_000)

        self.assertEqual(self._played("track", ["t1"], fullPlaysOnly=True), set())
        self.assertEqual(self._played("track", ["t1"], fullPlaysOnly=False), {"t1"})

    def test_a_complete_play_counts_either_way(self):
        self._play("alice", "t1", _FEBRUARY)

        self.assertEqual(self._played("track", ["t1"], fullPlaysOnly=True), {"t1"})

    def test_it_reaches_artists_and_albums_too(self):
        self._play("alice", "t2", _FEBRUARY, timePlayed=1_000)

        self.assertEqual(self._played("artist", ["a1", "a2"], fullPlaysOnly=True), set())
        self.assertEqual(self._played("album", ["al1"], fullPlaysOnly=True), set())


class TestTheEdges(PlayedInRangeTestCase):
    def test_no_ids_asks_nothing(self):
        """An empty page must not become an unbounded question."""
        self._play("alice", "t1", _FEBRUARY)

        self.assertEqual(self._played("track", []), set())

    def test_an_unknown_kind_is_refused_rather_than_guessed(self):
        with self.assertRaises(ValueError):
            self.repo.getEntitiesPlayedInRange("alice", "playlist", ["p1"], 0, 1)

    def test_ids_that_do_not_exist_are_simply_absent(self):
        self._play("alice", "t1", _FEBRUARY)

        self.assertEqual(self._played("track", ["t1", "nope"]), {"t1"})

    def test_each_entity_is_reported_once_however_many_plays_it_had(self):
        for offset in range(5):
            self._play("alice", "t1", _FEBRUARY + offset * 600)

        result = self.repo.getEntitiesPlayedInRange(
            "alice", "track", ["t1"], _FEBRUARY - 86400, _FEBRUARY + 86400)

        self.assertEqual(result, ["t1"])


if __name__ == "__main__":
    unittest.main()
