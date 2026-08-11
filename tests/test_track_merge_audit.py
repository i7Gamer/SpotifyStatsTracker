"""The coverage audit's findings, pinned: every surface that answers about a
SONG spans its whole merge group.

The audit swept every track-keyed read in the app and classified it. These
tests cover the fixes it demanded - each one names the user-visible symptom it
prevents, because that is what a regression here costs.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase

SINGLE = "A" * 22
ALBUM_CUT = "B" * 22
OTHER = "C" * 22


class MergedPairTestCase(DatabaseTestCase):
    def _db(self, singlePlays=3, albumPlays=9):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO users (username, created_at) VALUES ('alice', 0)")
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES ('alb', 'A', '')")
            conn.execute("INSERT OR IGNORE INTO artists (id, name, url) VALUES ('art1', 'Artist', '')")
            for trackId, plays in ((SINGLE, singlePlays), (ALBUM_CUT, albumPlays)):
                conn.execute("INSERT INTO tracks (id, name, url, album_id, duration_ms) "
                             "VALUES (?, 'Shared Song', '', 'alb', 200000)", (trackId,))
                conn.execute("INSERT INTO track_artists (track_id, artist_id, position) "
                             "VALUES (?, 'art1', 0)", (trackId,))
                for i in range(plays):
                    conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                                 "VALUES ('alice', ?, ?, 200000)",
                                 (trackId, 1e9 + (0 if trackId == SINGLE else 5e5) + i))
            conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?", (ALBUM_CUT, SINGLE))
        return db


class TestPlayedFlagsSpanTheGroup(MergedPairTestCase):
    def test_a_viewer_who_played_only_the_sibling_still_counts(self):
        """The flag decides internal-link vs open.spotify.com, and the page an
        internal link lands on is the canonical's - which HAS this viewer's
        plays, on the other release. Linking them out is wrong for every
        surface this feeds: Now Playing, the friends strip, Compare."""
        db = self._db(singlePlays=3, albumPlays=0)

        #< asked about the ALBUM CUT, which the viewer never played directly
        self.assertEqual(db.repo.getPlayedTrackIds("alice", [ALBUM_CUT]), {ALBUM_CUT})

    def test_asked_about_the_member_it_also_answers_yes(self):
        db = self._db(singlePlays=0, albumPlays=9)

        self.assertEqual(db.repo.getPlayedTrackIds("alice", [SINGLE]), {SINGLE})

    def test_a_song_nobody_played_stays_unplayed(self):
        db = self._db(singlePlays=0, albumPlays=0)

        self.assertEqual(db.repo.getPlayedTrackIds("alice", [SINGLE, ALBUM_CUT]), set())

    def test_unknown_ids_still_answer_no(self):
        db = self._db()

        self.assertEqual(db.repo.getPlayedTrackIds("alice", ["Z" * 22]), set())


class TestRankMovementSeesTheWholeGroup(MergedPairTestCase):
    def test_a_group_played_only_via_its_member_is_not_new(self):
        """The 'new' badge on the Top lists: the entry id is canonical, the
        previous period's plays sat on the single, and 'new' must not lie."""
        db = self._db(singlePlays=3, albumPlays=0)

        played = db.repo.getEntitiesPlayedInRange("alice", "track", [ALBUM_CUT], 0, 2e9)

        self.assertEqual(played, [ALBUM_CUT])

    def test_an_unplayed_group_is_still_new(self):
        db = self._db(singlePlays=0, albumPlays=0)

        self.assertEqual(db.repo.getEntitiesPlayedInRange("alice", "track", [ALBUM_CUT], 0, 2e9), [])


class TestTheSongPageSpansTheGroup(MergedPairTestCase):
    def test_the_play_log_and_its_pager_cover_both_releases(self):
        """The canonical page's timeline: 12 entries and a pager sized 12, not
        the 9 the canonical release alone recorded."""
        db = self._db()

        self.assertEqual(db.repo.getPlaysCount("alice", trackId=ALBUM_CUT), 12)
        self.assertEqual(len(db.repo.getPlaysNewestFirst("alice", trackId=ALBUM_CUT, count=50)), 12)

    def test_asked_via_the_merged_member_the_log_is_the_same(self):
        db = self._db()

        self.assertEqual(db.repo.getPlaysCount("alice", trackId=SINGLE), 12)

    def test_the_bucketed_chart_covers_both_releases(self):
        db = self._db()

        buckets = db.repo.getBucketedPlayTotals("alice", trackId=ALBUM_CUT)

        self.assertEqual(sum(b["plays"] for b in buckets), 12)


class TestOnThisDayCountsTheSongOnce(MergedPairTestCase):
    def test_the_winner_is_the_merged_song_not_a_lesser_track(self):
        """3 plays of the single + 2 of the album cut on the same day is 5
        plays of one song; a 4-play other track must not win the year card."""
        import time as _time
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        dayTs = _time.time() - 365 * 86400   #< this day last year
        with conn:
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES ('alb', 'A', '')")
            for trackId, name in ((SINGLE, 'Shared Song'), (ALBUM_CUT, 'Shared Song'), (OTHER, 'Other Song')):
                conn.execute("INSERT INTO tracks (id, name, url, album_id, duration_ms) "
                             "VALUES (?, ?, '', 'alb', 200000)", (trackId, name))
            #< under the db's OWN user: getOnThisDay reads self.user
            for trackId, plays in ((SINGLE, 3), (ALBUM_CUT, 2), (OTHER, 4)):
                for i in range(plays):
                    conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                                 "VALUES (?, ?, ?, 200000)", (db.user, trackId, dayTs + i * 60))
            conn.execute("UPDATE tracks SET canonical_id=? WHERE id=?", (ALBUM_CUT, SINGLE))

        entries = db.getOnThisDay()

        self.assertTrue(entries, "expected an on-this-day entry")
        self.assertEqual(entries[0]["trackId"], ALBUM_CUT)
        self.assertEqual(entries[0]["playCount"], 5)


class TestGenresFollowTheCanonical(MergedPairTestCase):
    def _tagCanonical(self, db, genre="rock"):
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO track_genres (track_id, genre, position, inherited) "
                         "VALUES (?, ?, 0, 0)", (ALBUM_CUT, genre))

    def test_plays_on_the_untagged_member_still_count_for_the_genre(self):
        """The single's Last.fm lookup failed; the canonical is tagged rock.
        The song's genre is a property of the SONG, so all 12 plays are rock -
        not 9 rock and 3 nowhere."""
        db = self._db()
        self._tagCanonical(db)

        counts = db.repo.getGenrePlayCounts("alice", inherited=1)

        self.assertEqual({c["genre"]: c["plays"] for c in counts}.get("rock"), 12)

    def test_the_genre_top_tracks_list_shows_one_row(self):
        db = self._db()
        self._tagCanonical(db)

        rows = db.repo.getTopTracksForGenre("alice", "rock", includeInherited=1, limit=10)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], ALBUM_CUT)
        self.assertEqual(rows[0]["playCount"], 12)

    def test_the_share_denominator_counts_the_plays_the_numerator_counts(self):
        """"Share of tagged plays" divides one by the other, so the two have to
        agree about what a tagged play IS. The numerator credits a merged
        member's plays to the canonical's genres; a denominator still asking
        whether the PLAYED release carries a row of its own leaves this song's
        3 member plays in the top half and out of the bottom - 12/9, a share
        of 133.3% printed on the genre card."""
        db = self._db()
        self._tagCanonical(db)

        numerator = db.repo.getGenrePlayStats("alice", "rock", 1, None, None)["plays"]
        denominator = db.repo.getTotalGenreTaggedPlays("alice", 1, None, None)

        self.assertEqual(numerator, 12)
        self.assertEqual(denominator, 12)

    def test_a_genre_only_the_merged_away_release_carries_is_counted_the_same_way(self):
        """The mirror, and the reason this is about AGREEMENT rather than about
        a bigger number: genre membership lives on the canonical, so a row on
        the merged-away single is not the song's genre. Whatever the numerator
        says, the denominator has to say it too, or the shares stop summing."""
        db = self._db()
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO track_genres (track_id, genre, position, inherited) "
                         "VALUES (?, 'jazz', 0, 0)", (SINGLE,))

        numerator = db.repo.getGenrePlayStats("alice", "jazz", 1, None, None)["plays"]

        self.assertEqual(db.repo.getTotalGenreTaggedPlays("alice", 1, None, None), numerator)

    def test_the_denominator_is_unchanged_while_nothing_is_merged(self):
        """The two spellings must be equivalent before a merge exists - that
        equivalence is what lets the fast one run (see _genreMembershipJoin)."""
        db = self._db()
        self._tagCanonical(db)
        conn = db.repo._conn()
        with conn:
            conn.execute("UPDATE tracks SET canonical_id=NULL WHERE id=?", (SINGLE,))

        #< only the album cut is tagged, and nothing is merged, so only its 9
        self.assertEqual(db.repo.getTotalGenreTaggedPlays("alice", 1, None, None), 9)


class TestTagsAreSongLevel(MergedPairTestCase):
    def test_a_tag_added_via_the_member_lands_on_the_canonical(self):
        db = self._db()

        db.repo.addTag("alice", "favs", "track", SINGLE)

        row = db.repo._conn().execute(
            "SELECT entity_id FROM user_tags WHERE tag='favs'").fetchone()
        self.assertEqual(row["entity_id"], ALBUM_CUT)

    def test_a_pre_merge_tag_on_the_member_shows_on_the_canonical(self):
        db = self._db()
        conn = db.repo._conn()
        with conn:   #< written raw, the way a pre-merge row would exist
            conn.execute("INSERT INTO user_tags (username, tag, entity_type, entity_id, created_at) "
                         "VALUES ('alice', 'oldtag', 'track', ?, 1)", (SINGLE,))

        self.assertIn("oldtag", db.repo.getTagsForEntity("alice", "track", ALBUM_CUT))

    def test_removing_from_the_canonical_removes_the_legacy_row_too(self):
        """Without the group-wide delete the union-read resurrects the tag
        immediately - an unremovable tag, which reads as a broken button."""
        db = self._db()
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO user_tags (username, tag, entity_type, entity_id, created_at) "
                         "VALUES ('alice', 'oldtag', 'track', ?, 1)", (SINGLE,))

        self.assertTrue(db.repo.removeTag("alice", "oldtag", "track", ALBUM_CUT))
        self.assertEqual(db.repo.getTagsForEntity("alice", "track", ALBUM_CUT), [])

    def test_a_tag_on_two_releases_counts_one_song(self):
        db = self._db()
        conn = db.repo._conn()
        with conn:
            for trackId in (SINGLE, ALBUM_CUT):
                conn.execute("INSERT INTO user_tags (username, tag, entity_type, entity_id, created_at) "
                             "VALUES ('alice', 'both', 'track', ?, 1)", (trackId,))

        tags = {t["tag"]: t["count"] for t in db.repo.getUserTags("alice")}
        self.assertEqual(tags["both"], 1)

    def test_tagged_track_ids_cover_the_whole_group(self):
        """These ids feed the playlist export's play filters: tag the single,
        and the exported row must still count the album cut's plays."""
        db = self._db()
        db.repo.addTag("alice", "favs", "track", SINGLE)

        ids = set(db.repo.getTaggedTrackIds("alice", ["favs"]))

        self.assertEqual(ids, {SINGLE, ALBUM_CUT})


class TestEntitySongCountsMatchTheListBesideThem(MergedPairTestCase):
    """"Unique Songs Listened" counts what the page below it lists.

    This one used to collapse the merge group, which reads as the same
    statement the global lists make - but the list it captions does NOT merge
    (_mergesCanonically keeps per-release rows on an entity page, so an album
    is never handed a row belonging to a different release). The hero then
    said 1 over two visible rows of the same song, which is the very shape the
    merge audit called a defect: a number nobody can reconcile with what is on
    screen is indistinguishable from a wrong one, whether it spans more than
    the list or less.

    The global counts are where "a song is a song" stays observable - Top
    Songs' own Unique Songs, discovered songs, Wrapped - and those still
    merge, because the lists they caption merge too."""

    def test_the_artist_count_matches_its_own_song_list(self):
        db = self._db()

        artists = db.repo.getArtistAggregates("alice")
        songs = db.repo.getSongsPage("alice", artistId="art1")

        self.assertEqual(len(artists), 1)
        self.assertEqual(artists[0]["uniqueSongCount"], len(songs))
        self.assertEqual(artists[0]["uniqueSongCount"], 2)

    def test_the_album_count_matches_its_own_song_list(self):
        db = self._db()

        albums = db.repo.getAlbumsPage("alice", albumId="alb")
        songs = db.repo.getSongsPage("alice", albumId="alb")

        self.assertEqual(albums[0]["uniqueSongCount"], len(songs))
        self.assertEqual(albums[0]["uniqueSongCount"], 2)

    def test_the_global_song_count_still_merges(self):
        """The counterpart, and the reason this is a scoping fix rather than a
        revert: Top Songs' own Unique Songs captions a list that DOES merge."""
        db = self._db()

        self.assertEqual(db.repo.getSongsCount("alice"), 1)
        self.assertEqual(len(db.repo.getSongsPage("alice")), 1)


if __name__ == "__main__":
    unittest.main()
