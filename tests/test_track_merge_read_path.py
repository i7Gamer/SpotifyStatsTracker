"""Phase 5: the global lists count a merged song once.

Scoped deliberately. A merge says two catalog rows are the same RECORDING, and
that is true globally - in Top Songs, in counts, in totals. It is not the right
answer on an album page, where the question is "what is on this album": the
canonical belongs to exactly one release, so merging there would show an album a
row whose title, cover and link belong to a different one. Album and artist
detail pages keep their own per-track rows.

Everything here is a no-op until something sets canonical_id, which is why the
change can ship ahead of the matcher ever running.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase

SINGLE = "A" * 22        #< the song as released on a single
ALBUM_CUT = "B" * 22     #< the same recording on an album


class TrackMergeReadPathTestCase(DatabaseTestCase):
    def _seed(self, db, merge=True):
        """The same recording twice: 3 plays on the single, 9 on the album cut."""
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO users (username, created_at) VALUES ('alice', 0)")
            for albumId, name in (("albSingle", "The Single"), ("albLP", "The Album")):
                conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES (?, ?, '')",
                             (albumId, name))
            conn.execute("INSERT OR IGNORE INTO artists (id, name, url) VALUES ('art1', 'Artist', '')")
            for trackId, albumId, plays in ((SINGLE, "albSingle", 3), (ALBUM_CUT, "albLP", 9)):
                conn.execute("INSERT INTO tracks (id, name, url, album_id, duration_ms) "
                             "VALUES (?, 'Shared Song', '', ?, 200000)", (trackId, albumId))
                conn.execute("INSERT INTO track_artists (track_id, artist_id, position) "
                             "VALUES (?, 'art1', 0)", (trackId,))
                for i in range(plays):
                    conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                                 "VALUES ('alice', ?, ?, 200000)", (trackId, 1e9 + hash(trackId) % 1000 + i))
            if merge:
                #< the album cut wins the election, being the more played
                conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?", (ALBUM_CUT, SINGLE))
        return db

    def _songs(self, db, **kwargs):
        return db.repo.getSongsPage("alice", **kwargs)


class TestGlobalListsMergeThem(TrackMergeReadPathTestCase):
    def test_the_top_list_shows_one_row_with_both_play_counts(self):
        db = self._seed(self._makeDb({}, []))

        songs = self._songs(db)

        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0]["plays"], 12)

    def test_the_row_carries_the_canonical_track(self):
        """Not an arbitrary member. Grouping by the canonical while selecting
        the played row's columns lets SQLite pick whichever it likes, so the
        title, cover and link could come from the version nobody chose."""
        db = self._seed(self._makeDb({}, []))

        songs = self._songs(db)

        self.assertEqual(songs[0]["id"], ALBUM_CUT)
        self.assertEqual(songs[0]["album"]["id"], "albLP")

    def test_the_count_agrees_with_the_page(self):
        """They are separate queries; pagination breaks if they disagree about
        how many rows exist."""
        db = self._seed(self._makeDb({}, []))

        self.assertEqual(db.repo.getSongsCount("alice"), len(self._songs(db)))

    def test_nothing_changes_until_something_merges(self):
        """The inert property that lets this ship ahead of the matcher."""
        db = self._seed(self._makeDb({}, []), merge=False)

        songs = self._songs(db)

        self.assertEqual(len(songs), 2)
        self.assertEqual(db.repo.getSongsCount("alice"), 2)


class TestDetailPagesKeepTheirOwnRows(TrackMergeReadPathTestCase):
    def test_an_album_page_shows_its_own_track_and_its_own_plays(self):
        """The question an album page answers is "what is on this album", and
        the answer cannot be a row belonging to a different release."""
        db = self._seed(self._makeDb({}, []))

        songs = self._songs(db, albumId="albSingle")

        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0]["id"], SINGLE)
        self.assertEqual(songs[0]["plays"], 3)

    def test_the_other_album_keeps_its_own_row_too(self):
        db = self._seed(self._makeDb({}, []))

        songs = self._songs(db, albumId="albLP")

        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0]["id"], ALBUM_CUT)
        self.assertEqual(songs[0]["plays"], 9)

    def test_an_artist_page_keeps_per_track_rows(self):
        db = self._seed(self._makeDb({}, []))

        songs = self._songs(db, artistId="art1")

        self.assertEqual(sorted(s["id"] for s in songs), sorted([SINGLE, ALBUM_CUT]))

    def test_asking_for_one_track_answers_about_its_whole_song(self):
        """Changed deliberately by the coverage audit. The trackId lookup is
        the song detail page's own query, and that page is the canonical's
        page - the row every merged global list links to. It used to answer
        per-release, which was the audit's central contradiction: a hero
        saying 3 plays under a caption promising "plays across all of them
        are counted together" while Top Songs said 12. Asking by EITHER end
        of the merge now returns the one canonical row with the group total;
        the route redirects when the answered id differs from the asked one."""
        db = self._seed(self._makeDb({}, []))

        for askedId in (SINGLE, ALBUM_CUT):
            with self.subTest(asked=askedId):
                songs = self._songs(db, trackId=askedId)

                self.assertEqual(len(songs), 1)
                self.assertEqual(songs[0]["id"], ALBUM_CUT)
                self.assertEqual(songs[0]["plays"], 12)


class TestTheOtherGlobalCounts(TrackMergeReadPathTestCase):
    def test_discovered_songs_counts_the_song_once(self):
        """"Songs discovered this year" is a count of SONGS. Two catalog rows
        for one recording discovered in the same year is one discovery, and it
        would otherwise inflate every Wrapped that counts them."""
        db = self._seed(self._makeDb({}, []))

        self.assertEqual(db.repo.getDiscoveredSongsCount("alice", 0, 4e9), 1)

    def test_discovered_songs_is_unchanged_without_a_merge(self):
        db = self._seed(self._makeDb({}, []), merge=False)

        self.assertEqual(db.repo.getDiscoveredSongsCount("alice", 0, 4e9), 2)

    def _skip(self, db, trackId, count=4):
        conn = db.repo._conn()
        with conn:
            for i in range(count):
                conn.execute("INSERT INTO plays (username, track_id, played_at, time_played, is_skip) "
                             "VALUES ('alice', ?, ?, 1000, 1)", (trackId, 3e9 + hash(trackId) % 500 + i))

    def test_skips_of_the_same_song_are_one_row(self):
        """A song skipped on both its releases is one song you skip, and the
        skip rate is only meaningful against the plays it is divided by - which
        the merge has already combined."""
        db = self._seed(self._makeDb({}, []))
        self._skip(db, SINGLE, 4)
        self._skip(db, ALBUM_CUT, 6)

        rows = db.repo.getMostSkippedTracks("alice", limit=10)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["skips"], 10)
        self.assertEqual(db.repo.getSkippedTracksCount("alice"), 1)

    def test_an_album_scoped_skip_list_keeps_its_own_rows(self):
        db = self._seed(self._makeDb({}, []))
        self._skip(db, SINGLE, 4)
        self._skip(db, ALBUM_CUT, 6)

        rows = db.repo.getMostSkippedTracks("alice", limit=10, albumId="albSingle")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["skips"], 4)


class TestSearchSpansTheMergeGroup(TrackMergeReadPathTestCase):
    """A search decides WHICH songs are listed. It must not also decide which
    of a song's plays are counted.

    The matched-id set lands on the PLAYED track while the aggregate groups by
    the canonical, so a term matching only one release turned the group's row
    into that release's subtotal - under the canonical's title, so the row said
    "Shared Song" either way and only the number moved. The same boundary
    Repository.getTaggedTrackIds expands across ("a tagged song's row shows a
    fraction of its count") and the rediscovery/fresh-find narrowings already
    fixed; this is the third crossing of it.

    Only 'The Single' and 'The Album' tell the two releases apart here - the
    track's own name is shared, which is what a merge means."""

    def test_a_term_matching_one_release_still_counts_the_whole_song(self):
        db = self._seed(self._makeDb({}, []))

        matchesSingle = self._songs(db, searchQuery="single")
        matchesAlbum = self._songs(db, searchQuery="album")

        self.assertEqual([r["plays"] for r in matchesSingle], [12])
        self.assertEqual([r["plays"] for r in matchesAlbum], [12])
        #< and it is the canonical's row in both cases, as it is unsearched
        self.assertEqual(matchesSingle[0]["id"], ALBUM_CUT)

    def test_a_term_matching_only_the_merged_away_release_still_finds_the_song(self):
        """The single lost the election, so its id appears in no list - but it
        is still where the words 'The Single' live, and a song you can only
        name by that release is still a song you can find."""
        db = self._seed(self._makeDb({}, []))

        self.assertEqual([r["id"] for r in self._songs(db, searchQuery="single")], [ALBUM_CUT])

    def test_the_searched_count_agrees_with_the_searched_page(self):
        db = self._seed(self._makeDb({}, []))

        for term in ("single", "album", "shared"):
            self.assertEqual(db.repo.getSongsCount("alice", searchQuery=term),
                             len(self._songs(db, searchQuery=term)), term)

    def test_an_unmerged_library_searches_exactly_as_it_did(self):
        """The expansion is a no-op without a merge - COALESCE(canonical_id,
        id) is the id - which is what keeps tests/test_search_two_phase's
        row-for-row equality true."""
        db = self._seed(self._makeDb({}, []), merge=False)

        self.assertEqual([(r["id"], r["plays"]) for r in self._songs(db, searchQuery="single")],
                         [(SINGLE, 3)])
        self.assertEqual([(r["id"], r["plays"]) for r in self._songs(db, searchQuery="album")],
                         [(ALBUM_CUT, 9)])

    def test_an_album_scoped_search_keeps_its_own_rows(self):
        """The album page asks "what is on this album", so it does not merge -
        and must not gain the sibling release's plays through the search set
        either."""
        db = self._seed(self._makeDb({}, []))

        rows = self._songs(db, albumId="albSingle", searchQuery="shared")

        self.assertEqual([(r["id"], r["plays"]) for r in rows], [(SINGLE, 3)])

    def test_a_searched_skip_list_counts_the_whole_song_too(self):
        """getMostSkippedTracks crosses the same boundary, and a skip RATE is
        the ratio of two numbers that must come from the same population."""
        db = self._seed(self._makeDb({}, []))
        TestTheOtherGlobalCounts._skip(self, db, SINGLE, 4)
        TestTheOtherGlobalCounts._skip(self, db, ALBUM_CUT, 6)

        rows = db.repo.getMostSkippedTracks("alice", limit=10, searchQuery="single")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["skips"], 10)
        self.assertEqual(rows[0]["plays"], 12)
        self.assertEqual(db.repo.getSkippedTracksCount("alice", searchQuery="single"), 1)


class TestTrendCards(TrackMergeReadPathTestCase):
    def test_an_obsession_split_across_two_releases_still_counts(self):
        """The trend cards pick ONE track over a threshold, so a song whose
        plays are divided between a single and an album cut could miss a bar it
        has actually cleared - the card then shows nothing, or something
        quieter, for the song someone has had on repeat."""
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        import time as _time
        now = _time.time()
        with conn:
            conn.execute("INSERT OR IGNORE INTO users (username, created_at) VALUES ('alice', 0)")
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES ('alb', 'A', '')")
            for trackId in (SINGLE, ALBUM_CUT):
                conn.execute("INSERT INTO tracks (id, name, url, album_id, duration_ms) "
                             "VALUES (?, 'Shared Song', '', 'alb', 200000)", (trackId,))
                for i in range(6):   #< 6 each: 12 together, over the threshold
                    conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                                 "VALUES ('alice', ?, ?, 200000)",
                                 (trackId, now - (i + 1) * 3600))
            conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?", (ALBUM_CUT, SINGLE))

        trends = db.repo.getDashboardTrendsRaw("alice", now)

        obsession = trends.get("obsession")
        self.assertIsNotNone(obsession, trends)
        self.assertEqual(obsession["track_id"], ALBUM_CUT)
        self.assertEqual(obsession["recent_count"], 12)


if __name__ == "__main__":
    unittest.main()


class TestTrendNarrowingSpansTheMergeGroup(TrackMergeReadPathTestCase):
    """The trend queries narrow their candidates with an `IN (recently played)`
    subquery for speed. Narrowing by the PLAYED id while grouping by the
    canonical splits a merge group across the boundary: the recently-played
    release passes the filter, the other release's rows are silently dropped
    from the aggregate, and the aggregate lies about the group's history."""

    SECONDS_PER_DAY = 86400

    def _seedSplitHistory(self, db, oldDaysAgo=200, oldPlays=4, recentPlays=1):
        """Old plays on the album cut, recent plays on the single, merged. The
        song's true history spans both releases; each release alone tells a
        different (wrong) story."""
        import time as _time
        now = _time.time()
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO users (username, created_at) VALUES ('alice', 0)")
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES ('alb', 'A', '')")
            for trackId in (SINGLE, ALBUM_CUT):
                conn.execute("INSERT INTO tracks (id, name, url, album_id, duration_ms) "
                             "VALUES (?, 'Shared Song', '', 'alb', 200000)", (trackId,))
            for i in range(oldPlays):
                conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                             "VALUES ('alice', ?, ?, 200000)",
                             (ALBUM_CUT, now - (oldDaysAgo + i) * self.SECONDS_PER_DAY))
            for i in range(recentPlays):
                conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                             "VALUES ('alice', ?, ?, 200000)", (SINGLE, now - (i + 1) * 3600))
            conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?", (ALBUM_CUT, SINGLE))
        return now

    def test_a_rediscovery_via_the_other_release_is_found(self):
        """Played the album cut heavily half a year ago, came back to the song
        via its single this week: that IS a rediscovery of the song, and only
        an aggregate spanning both releases can see it - the old plays live on
        the release the narrowing filter would drop."""
        db = self._makeDb({}, [])
        now = self._seedSplitHistory(db, oldDaysAgo=200, oldPlays=4, recentPlays=1)

        trends = db.repo.getDashboardTrendsRaw("alice", now)

        rediscovery = trends.get("rediscovery")
        self.assertIsNotNone(rediscovery, trends)
        self.assertEqual(rediscovery["track_id"], ALBUM_CUT)
        self.assertEqual(rediscovery["old_count"], 4)

    def test_a_known_song_played_from_a_new_release_is_not_a_fresh_find(self):
        """The false positive: drop the other release's history and the group's
        MIN(played_at) is this week, so a song known for half a year reads as
        discovered days ago. First heard is a property of the SONG."""
        db = self._makeDb({}, [])
        now = self._seedSplitHistory(db, oldDaysAgo=200, oldPlays=1, recentPlays=3)

        trends = db.repo.getDashboardTrendsRaw("alice", now)

        freshFind = trends.get("freshFind")
        if freshFind is not None:
            self.assertNotEqual(freshFind["track_id"], ALBUM_CUT,
                                "a song first heard 200 days ago reported as a fresh find")
