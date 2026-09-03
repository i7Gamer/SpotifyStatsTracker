# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The genre backfill queue answers about the SONG, like every genre READ does.

Every genre read resolves the merge group (_genreMembershipJoin joins
track_genres on COALESCE(trk.canonical_id, trk.id)); the queue and the write
did not. So a genre row earned by a release that later became a MEMBER went
unreadable - the distribution queries return nothing for those plays, coverage
counts them uncovered, the card badges empty - and the elected canonical
re-entered the queue ranked by its own play count instead of the group's.

The repair is deliberately queue-side, not read-side: nothing here changes what
a read resolves, so the two tests that pin today's asymmetry
(test_track_merge_read_path.TestGenreCoverageAgreesWithGenreStats and
test_track_merge_audit.TestGenresFollowTheCanonical) keep passing untouched.
Genre rows are never moved or deleted either, so unmerging stays lossless -
the canonical is simply re-queued and gets its OWN Last.fm answer.

2026-09-03 review, H1.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
from Database.repository import GENRE_BACKFILL_RETRY_SECONDS

SINGLE = "A" * 22
ALBUM_CUT = "B" * 22
USER = "alice"


class GenreQueueTestCase(DatabaseTestCase):
    """A pair of releases of one song: the member carries most of the plays,
    the canonical carries few. That asymmetry is the point - the queue must
    rank the CANONICAL by the group's total, which is where the old query
    buried it."""

    def _db(self, singlePlays=9, albumPlays=1, merged=True):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO users (username, created_at) VALUES (?, 0)",
                         (USER,))
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES ('alb', 'A', '')")
            conn.execute("INSERT OR IGNORE INTO artists (id, name, url) "
                         "VALUES ('art1', 'Artist', '')")
            conn.execute("INSERT OR IGNORE INTO artists (id, name, url) "
                         "VALUES ('art2', 'Other Artist', '')")
            for trackId, name, artistId, plays in (
                    (SINGLE, "Shared Song - 2011 Remaster", "art2", singlePlays),
                    (ALBUM_CUT, "Shared Song", "art1", albumPlays)):
                conn.execute("INSERT INTO tracks (id, name, url, album_id, duration_ms) "
                             "VALUES (?, ?, '', 'alb', 200000)", (trackId, name))
                conn.execute("INSERT INTO track_artists (track_id, artist_id, position) "
                             "VALUES (?, ?, 0)", (trackId, artistId))
                for i in range(plays):
                    conn.execute(
                        "INSERT INTO plays (username, track_id, played_at, time_played) "
                        "VALUES (?, ?, ?, 200000)",
                        (USER, trackId, 1e9 + (0 if trackId == SINGLE else 5e5) + i))
            if merged:
                conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?", (ALBUM_CUT, SINGLE))
        return db

    def _stale(self, db, trackId):
        conn = db.repo._conn()
        with conn:
            conn.execute("UPDATE tracks SET lastfm_attempted_at=? WHERE id=?",
                         (time.time() - GENRE_BACKFILL_RETRY_SECONDS - 1, trackId))


class TestTheQueueAsksAboutTheSong(GenreQueueTestCase):
    def test_the_canonical_is_queued_and_the_member_is_not(self):
        """Looking the member up writes its answer to an id no read resolves."""
        db = self._db()

        ids = [r["id"] for r in db.repo.getTracksMissingGenres(10, username=USER)]

        self.assertEqual(ids, [ALBUM_CUT])

    def test_the_canonical_is_ranked_by_the_whole_group(self):
        """The bug that made this urgent: the canonical is the LESS played
        release (1 play against the member's 9), so ordering by its own count
        buried it behind every ordinary track. Its rank is the song's 10."""
        db = self._db(singlePlays=9, albumPlays=1)

        rows = db.repo.getTracksMissingGenres(10, username=USER)

        self.assertEqual(rows[0]["play_count"], 10)

    def test_the_lookup_name_and_artist_are_the_canonicals_own(self):
        """The Last.fm lookup goes by NAME, and the write lands on the row that
        was queued - so if they came from different releases the answer stored
        would vary run to run. Both must be the canonical's."""
        db = self._db()

        row = db.repo.getTracksMissingGenres(10, username=USER)[0]

        self.assertEqual(row["name"], "Shared Song")
        self.assertEqual(row["artist_id"], "art1")
        self.assertEqual(row["artist_name"], "Artist")

    def test_a_canonical_with_no_plays_of_its_own_is_still_queued(self):
        """Every play in the group sits on the member. The canonical is what
        every read resolves to, so it is what has to be looked up."""
        db = self._db(singlePlays=9, albumPlays=0)

        rows = db.repo.getTracksMissingGenres(10, username=USER)

        self.assertEqual([r["id"] for r in rows], [ALBUM_CUT])
        self.assertEqual(rows[0]["play_count"], 9)

    def test_own_genres_on_the_canonical_take_it_out_of_the_queue(self):
        db = self._db()
        db.repo.replaceTrackGenres(ALBUM_CUT, ["rock"], inherited=False)
        self._stale(db, ALBUM_CUT)

        ids = [r["id"] for r in db.repo.getTracksMissingGenres(10, username=USER)]

        self.assertEqual(ids, [])

    def test_own_genres_on_the_MEMBER_do_not(self):
        """The member's rows are unreadable by every aggregate, so they are not
        an answer for the song - the canonical still needs looking up."""
        db = self._db()
        db.repo.replaceTrackGenres(SINGLE, ["rock"], inherited=False)
        self._stale(db, ALBUM_CUT)

        ids = [r["id"] for r in db.repo.getTracksMissingGenres(10, username=USER)]

        self.assertEqual(ids, [ALBUM_CUT])

    def test_inherited_only_rows_on_the_canonical_requeue_after_the_ttl(self):
        db = self._db()
        db.repo.replaceTrackGenres(ALBUM_CUT, ["rock"], inherited=True)
        db.repo.markTracksLastfmAttempted([ALBUM_CUT])

        self.assertEqual(db.repo.getTracksMissingGenres(10, username=USER), [])

        self._stale(db, ALBUM_CUT)
        ids = [r["id"] for r in db.repo.getTracksMissingGenres(10, username=USER)]

        self.assertEqual(ids, [ALBUM_CUT])

    def test_the_global_queue_spans_the_group_too(self):
        """username=None is the instance-wide queue the backfiller runs."""
        db = self._db()

        rows = db.repo.getTracksMissingGenres(10, username=None)

        self.assertEqual([r["id"] for r in rows], [ALBUM_CUT])
        self.assertEqual(rows[0]["play_count"], 10)

    def test_a_canonical_without_a_primary_artist_is_excluded(self):
        """Structural exclusion, unchanged from the unmerged query: a track with
        no position-0 artist can neither be looked up nor inherit. It now
        excludes the whole GROUP, because the canonical is what would be
        looked up."""
        db = self._db()
        conn = db.repo._conn()
        with conn:
            conn.execute("DELETE FROM track_artists WHERE track_id=?", (ALBUM_CUT,))

        self.assertEqual(db.repo.getTracksMissingGenres(10, username=USER), [])


class TestTheUnmergedQueueIsUnchanged(GenreQueueTestCase):
    """The canonical hop costs a PK probe per play row, which is what
    _anyTrackMerges exists to avoid. With nothing merged the original statement
    must run and return exactly what it always did."""

    def test_both_releases_are_queued_separately_and_ranked_by_their_own_plays(self):
        db = self._db(singlePlays=9, albumPlays=1, merged=False)

        rows = db.repo.getTracksMissingGenres(10, username=USER)

        self.assertEqual([(r["id"], r["play_count"]) for r in rows],
                         [(SINGLE, 9), (ALBUM_CUT, 1)])

    def test_each_release_carries_its_own_name_and_artist(self):
        db = self._db(merged=False)

        rows = {r["id"]: r for r in db.repo.getTracksMissingGenres(10, username=USER)}

        self.assertEqual(rows[SINGLE]["name"], "Shared Song - 2011 Remaster")
        self.assertEqual(rows[SINGLE]["artist_id"], "art2")
        self.assertEqual(rows[ALBUM_CUT]["name"], "Shared Song")

    def test_the_limit_still_applies(self):
        db = self._db(merged=False)

        self.assertEqual(len(db.repo.getTracksMissingGenres(1, username=USER)), 1)


class TestAMergeRequeuesTheNewCanonical(GenreQueueTestCase):
    """A merge changes which release every read asks about. If the new
    canonical was already marked attempted, nothing would ever look it up again
    and the song stays genre-less for good - so the merge clears the mark."""

    def test_the_matcher_requeues_a_canonical_that_carries_no_own_genres(self):
        db = self._db(merged=False)
        db.repo.replaceTrackGenres(SINGLE, ["rock"], inherited=False)
        db.repo.markTracksLastfmAttempted([SINGLE, ALBUM_CUT])
        conn = db.repo._conn()
        with conn:
            conn.execute("UPDATE tracks SET isrc='ISRC00000001'")

        db.repo.mergeTracksByIsrc()

        self.assertIsNone(self._attempted(db, ALBUM_CUT))
        #< the member keeps its stamp AND its rows: unmerging must stay lossless
        self.assertIsNotNone(self._attempted(db, SINGLE))
        self.assertEqual([g["genre"] for g in db.repo.getTrackGenres(SINGLE)], ["rock"])

    def test_a_canonical_that_already_has_own_genres_is_left_alone(self):
        db = self._db(merged=False)
        db.repo.replaceTrackGenres(ALBUM_CUT, ["rock"], inherited=False)
        db.repo.markTracksLastfmAttempted([SINGLE, ALBUM_CUT])
        conn = db.repo._conn()
        with conn:
            conn.execute("UPDATE tracks SET isrc='ISRC00000001'")

        db.repo.mergeTracksByIsrc()

        self.assertIsNotNone(self._attempted(db, ALBUM_CUT))

    def test_a_manual_merge_requeues_its_target_too(self):
        db = self._db(merged=False)
        db.repo.markTracksLastfmAttempted([SINGLE, ALBUM_CUT])

        db.repo.mergeTrackManually(SINGLE, ALBUM_CUT, decidedBy="tester")

        self.assertIsNone(self._attempted(db, ALBUM_CUT))

    def test_a_manual_merge_into_a_tagged_target_leaves_it_alone(self):
        db = self._db(merged=False)
        db.repo.replaceTrackGenres(ALBUM_CUT, ["rock"], inherited=False)
        db.repo.markTracksLastfmAttempted([SINGLE, ALBUM_CUT])

        db.repo.mergeTrackManually(SINGLE, ALBUM_CUT, decidedBy="tester")

        self.assertIsNotNone(self._attempted(db, ALBUM_CUT))

    def _attempted(self, db, trackId):
        return db.repo._conn().execute(
            "SELECT lastfm_attempted_at FROM tracks WHERE id=?", (trackId,)).fetchone()[0]


class TestTheBacklogMigrationLever(GenreQueueTestCase):
    """requeueCanonicalsOfMergedGroupsWithoutOwnGenres: the one-time sweep for
    libraries merged before the queue learned about groups. Same shape as the
    three requeue* levers migrations 1.19.0/1.24.0/1.29.0 already shipped."""

    def test_it_requeues_a_canonical_whose_group_has_genres_it_cannot_read(self):
        db = self._db()
        db.repo.replaceTrackGenres(SINGLE, ["rock"], inherited=False)
        db.repo.markTracksLastfmAttempted([SINGLE, ALBUM_CUT])

        cleared = db.repo.requeueCanonicalsOfMergedGroupsWithoutOwnGenres()

        self.assertEqual(cleared, 1)
        self.assertIsNone(self._attempted(db, ALBUM_CUT))
        self.assertIsNotNone(self._attempted(db, SINGLE))

    def test_a_canonical_with_its_own_genres_is_not_requeued(self):
        db = self._db()
        db.repo.replaceTrackGenres(ALBUM_CUT, ["rock"], inherited=False)
        db.repo.markTracksLastfmAttempted([SINGLE, ALBUM_CUT])

        self.assertEqual(db.repo.requeueCanonicalsOfMergedGroupsWithoutOwnGenres(), 0)

    def test_an_unmerged_track_is_never_touched(self):
        """Scoped to canonicals of real groups - a plain track that came back
        tag-less is not this lever's business, and requeuing it would re-spend
        Last.fm quota on an answer that has not changed."""
        db = self._db(merged=False)
        db.repo.markTracksLastfmAttempted([SINGLE, ALBUM_CUT])

        self.assertEqual(db.repo.requeueCanonicalsOfMergedGroupsWithoutOwnGenres(), 0)
        self.assertIsNotNone(self._attempted(db, ALBUM_CUT))

    def test_it_is_idempotent(self):
        db = self._db()
        db.repo.replaceTrackGenres(SINGLE, ["rock"], inherited=False)
        db.repo.markTracksLastfmAttempted([SINGLE, ALBUM_CUT])

        db.repo.requeueCanonicalsOfMergedGroupsWithoutOwnGenres()

        self.assertEqual(db.repo.requeueCanonicalsOfMergedGroupsWithoutOwnGenres(), 0)

    def _attempted(self, db, trackId):
        return db.repo._conn().execute(
            "SELECT lastfm_attempted_at FROM tracks WHERE id=?", (trackId,)).fetchone()[0]


if __name__ == "__main__":
    unittest.main()
