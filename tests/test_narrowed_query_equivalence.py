"""Narrowing a plays query to one artist/album must not change what it counts.

_itemFilterClauses is shared by every play-scan query that can be narrowed to a
single item (play counts, play lists, skip stats, the bucketed chart totals, the
play time range). Its artist/album clauses name the entity's track set as an IN
subquery rather than a correlated EXISTS, because the EXISTS form gave SQLite
nothing seekable on plays: it read the user's whole history and tested each row
(~1.2s for a well-played artist on a real library). The two forms are
semantically identical, and these tests exist so a future rewrite of that helper
cannot quietly change which plays are included.

Each case is checked against the same figure derived from the UNNARROWED query,
so the assertions describe the invariant rather than restating the SQL.
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase

ARTIST_A, ARTIST_B = "a1", "a2"
ALBUM_1, ALBUM_2 = "alb1", "alb2"
SKIP_MS = 1_000        #< under the default 5s threshold
LISTEN_MS = 60_000


def _track(trackId, albumId, artists):
    return {
        "id": trackId, "name": f"Song {trackId}",
        "artists": [{"id": a, "name": f"Artist {a}"} for a in artists],
        "imageId": albumId,
        "album": {"id": albumId, "name": f"Album {albumId}", "url": "u",
                  "imageId": albumId, "imageUrl": "", "totalTracks": 3, "releaseDate": 0},
        "duration": 200_000,
    }


class NarrowedQueryEquivalenceTestCase(DatabaseTestCase):
    """t1/t2 are on ALBUM_1 by ARTIST_A; t3 is on ALBUM_2 by ARTIST_B; t4 is on
    ALBUM_2 and credited to BOTH artists, so the artist narrowing has a
    multi-artist row to get wrong."""

    def _db(self):
        tracks = {
            "t1": _track("t1", ALBUM_1, [ARTIST_A]),
            "t2": _track("t2", ALBUM_1, [ARTIST_A]),
            "t3": _track("t3", ALBUM_2, [ARTIST_B]),
            "t4": _track("t4", ALBUM_2, [ARTIST_A, ARTIST_B]),
        }
        entries = [
            {"id": "t1", "playedAt": 100.0, "timePlayed": LISTEN_MS},
            {"id": "t1", "playedAt": 200.0, "timePlayed": SKIP_MS},
            {"id": "t2", "playedAt": 300.0, "timePlayed": LISTEN_MS},
            {"id": "t3", "playedAt": 400.0, "timePlayed": LISTEN_MS},
            {"id": "t3", "playedAt": 500.0, "timePlayed": SKIP_MS},
            {"id": "t4", "playedAt": 600.0, "timePlayed": LISTEN_MS},
            {"id": "t4", "playedAt": 700.0, "timePlayed": SKIP_MS},
        ]
        db = self._makeDb(tracks, entries)
        db.repo.recomputeSkipFlags()   #< insertPlay never classifies; callers do
        return db

    # Which tracks each narrowing should reach, as plain Python.
    TRACKS_BY_ARTIST = {ARTIST_A: {"t1", "t2", "t4"}, ARTIST_B: {"t3", "t4"}}
    TRACKS_BY_ALBUM = {ALBUM_1: {"t1", "t2"}, ALBUM_2: {"t3", "t4"}}

    def _plays(self, db, trackIds, skips=False):
        """The plays of `trackIds`, derived from the UNNARROWED query - what a
        correct narrowing has to reproduce. Most of these queries exclude skips
        by default (getPlayTimeRange always does), so the two populations are
        counted separately rather than assumed equal."""
        rows = db.repo.getPlaysNewestFirst(db.user, includeSkips=True)
        return [r for r in rows if r["id"] in trackIds and bool(r["isSkip"]) == skips]

    def test_play_count_narrowed_by_artist_matches_the_full_scan(self):
        db = self._db()
        for artistId, trackIds in self.TRACKS_BY_ARTIST.items():
            with self.subTest(artistId=artistId):
                self.assertEqual(db.repo.getPlaysCount(db.user, artistId=artistId),
                                 len(self._plays(db, trackIds)))

    def test_play_count_narrowed_by_album_matches_the_full_scan(self):
        db = self._db()
        for albumId, trackIds in self.TRACKS_BY_ALBUM.items():
            with self.subTest(albumId=albumId):
                self.assertEqual(db.repo.getPlaysCount(db.user, albumId=albumId),
                                 len(self._plays(db, trackIds)))

    def test_play_count_including_skips_still_narrows_correctly(self):
        db = self._db()
        for artistId, trackIds in self.TRACKS_BY_ARTIST.items():
            with self.subTest(artistId=artistId):
                expected = len(self._plays(db, trackIds)) + len(self._plays(db, trackIds, skips=True))
                self.assertEqual(
                    db.repo.getPlaysCount(db.user, artistId=artistId, includeSkips=True), expected)

    def test_play_list_narrowed_by_artist_returns_exactly_that_artists_plays(self):
        db = self._db()
        rows = db.repo.getPlaysNewestFirst(db.user, artistId=ARTIST_A)

        self.assertEqual({r["id"] for r in rows}, self.TRACKS_BY_ARTIST[ARTIST_A])
        # A track credited to two artists must appear once per PLAY, not once
        # per credit - a join-based narrowing would double it.
        self.assertEqual(len([r for r in rows if r["id"] == "t4"]), 1)   #< its one real play

    def test_play_list_narrowed_by_album_returns_exactly_that_albums_plays(self):
        db = self._db()
        rows = db.repo.getPlaysNewestFirst(db.user, albumId=ALBUM_2)

        self.assertEqual({r["id"] for r in rows}, self.TRACKS_BY_ALBUM[ALBUM_2])

    def test_bucketed_totals_narrowed_by_artist_sum_to_that_artists_plays(self):
        db = self._db()
        trackIds = self.TRACKS_BY_ARTIST[ARTIST_A]
        rows = db.repo.getBucketedPlayTotals(db.user, artistId=ARTIST_A)

        self.assertEqual(sum(r["plays"] for r in rows), len(self._plays(db, trackIds)))
        self.assertEqual(sum(r["skips"] for r in rows), len(self._plays(db, trackIds, skips=True)))
        self.assertEqual(sum(r["totalTimeListened"] for r in rows),
                         sum(r["timePlayed"] for r in self._plays(db, trackIds)))

    def test_bucketed_totals_narrowed_by_album_sum_to_that_albums_plays(self):
        db = self._db()
        trackIds = self.TRACKS_BY_ALBUM[ALBUM_1]
        rows = db.repo.getBucketedPlayTotals(db.user, albumId=ALBUM_1)

        self.assertEqual(sum(r["plays"] for r in rows), len(self._plays(db, trackIds)))
        self.assertEqual(sum(r["skips"] for r in rows), len(self._plays(db, trackIds, skips=True)))

    def test_skip_stats_narrowed_by_artist_counts_only_that_artist(self):
        db = self._db()
        stats = db.repo.getSkipStats(db.user, artistId=ARTIST_B)

        self.assertEqual(stats["skips"], 2)    #< t3's and t4's short plays
        self.assertEqual(stats["plays"], 2)    #< t3's and t4's real plays

    def test_skip_stats_narrowed_by_album_counts_only_that_album(self):
        db = self._db()
        stats = db.repo.getSkipStats(db.user, albumId=ALBUM_1)

        self.assertEqual(stats["skips"], 1)
        self.assertEqual(stats["plays"], 2)

    def test_play_time_range_narrowed_by_artist(self):
        """Skips are excluded unconditionally here, so t4's skip at 700 must not
        extend ARTIST_B's range past its last real listen at 600."""
        db = self._db()
        first, last = db.repo.getPlayTimeRange(db.user, artistId=ARTIST_B)

        self.assertEqual((first, last), (400.0, 600.0))

    def test_play_time_range_narrowed_by_album(self):
        db = self._db()
        first, last = db.repo.getPlayTimeRange(db.user, albumId=ALBUM_1)

        self.assertEqual((first, last), (100.0, 300.0))

    def test_an_unknown_id_narrows_to_nothing(self):
        db = self._db()

        self.assertEqual(db.repo.getPlaysCount(db.user, artistId="nope"), 0)
        self.assertEqual(db.repo.getPlaysCount(db.user, albumId="nope"), 0)
        self.assertEqual(db.repo.getBucketedPlayTotals(db.user, artistId="nope"), [])

    def test_narrowing_by_track_is_unaffected(self):
        """The track clause filters plays.track_id directly and was never part
        of the problem - pinned so a rewrite of the helper leaves it alone."""
        db = self._db()

        self.assertEqual(db.repo.getPlaysCount(db.user, trackId="t1"), 1)            #< skip excluded
        self.assertEqual(db.repo.getPlaysCount(db.user, trackId="t1", includeSkips=True), 2)
        self.assertEqual(db.repo.getPlayTimeRange(db.user, trackId="t1"), (100.0, 100.0))


if __name__ == "__main__":
    unittest.main()
