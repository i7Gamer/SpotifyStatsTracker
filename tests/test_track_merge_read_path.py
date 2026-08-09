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

    def test_asking_for_one_track_answers_about_that_track(self):
        """The song detail page asks by id; resolving a merged id to its
        canonical is Phase 6's job, not something to do silently here."""
        db = self._seed(self._makeDb({}, []))

        songs = self._songs(db, trackId=SINGLE)

        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0]["id"], SINGLE)
        self.assertEqual(songs[0]["plays"], 3)


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
