"""Repository/Database layer for the Last.fm genre backfill: key storage,
app settings, genre join tables, the backfill queue queries, play-weighted
coverage and the genre distribution."""
import datetime
import sys
import os
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
from Database.repository import (
    GENRE_BACKFILL_RETRY_SECONDS, INHERITED_GENRES_SETTING_KEY, BIOGRAPHY_BACKFILL_RETRY_SECONDS,
)


def _dt(ts: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)


class LastfmApiKeyTestCase(DatabaseTestCase):
    def test_key_round_trips_and_is_encrypted_at_rest(self):
        db = self._makeDb({}, [])
        db.repo.updateUserLastfmApiKey("testuser", "my-api-key-123")

        raw = db.repo._conn().execute(
            "SELECT lastfm_api_key FROM users WHERE username=?", ("testuser",)
        ).fetchone()["lastfm_api_key"]
        self.assertTrue(raw.startswith("enc:v1:"))
        self.assertNotIn("my-api-key-123", raw)
        self.assertEqual(db.repo.getUserLastfmApiKey("testuser"), "my-api-key-123")

    def test_none_clears_the_key(self):
        db = self._makeDb({}, [])
        db.repo.updateUserLastfmApiKey("testuser", "my-api-key-123")
        db.repo.updateUserLastfmApiKey("testuser", None)
        self.assertIsNone(db.repo.getUserLastfmApiKey("testuser"))

    def test_unknown_user_reads_as_none(self):
        db = self._makeDb({}, [])
        self.assertIsNone(db.repo.getUserLastfmApiKey("nobody"))

    def test_legacy_plaintext_value_passes_through(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("UPDATE users SET lastfm_api_key=? WHERE username=?",
                         ("plain-legacy-key", "testuser"))
        self.assertEqual(db.repo.getUserLastfmApiKey("testuser"), "plain-legacy-key")


class AppSettingsTestCase(DatabaseTestCase):
    def test_missing_key_returns_default(self):
        db = self._makeDb({}, [])
        self.assertIsNone(db.repo.getAppSetting("nope"))
        self.assertEqual(db.repo.getAppSetting("nope", "fallback"), "fallback")

    def test_set_get_and_overwrite(self):
        db = self._makeDb({}, [])
        db.repo.setAppSetting("some_key", "one")
        self.assertEqual(db.repo.getAppSetting("some_key"), "one")
        db.repo.setAppSetting("some_key", "two")
        self.assertEqual(db.repo.getAppSetting("some_key"), "two")

    def test_inherited_genres_defaults_on_and_flips(self):
        db = self._makeDb({}, [])
        self.assertTrue(db.repo.isInheritedGenresEnabled())
        db.repo.setInheritedGenresEnabled(False)
        self.assertFalse(db.repo.isInheritedGenresEnabled())
        self.assertIsNotNone(db.repo.getAppSetting(INHERITED_GENRES_SETTING_KEY))
        db.repo.setInheritedGenresEnabled(True)
        self.assertTrue(db.repo.isInheritedGenresEnabled())

    def test_artist_bio_enabled_defaults_on_and_flips(self):
        db = self._makeDb({}, [])
        self.assertTrue(db.repo.isArtistBioEnabled())
        db.repo.setArtistBioEnabled(False)
        self.assertFalse(db.repo.isArtistBioEnabled())
        db.repo.setArtistBioEnabled(True)
        self.assertTrue(db.repo.isArtistBioEnabled())

    def test_album_bio_enabled_defaults_on_and_flips(self):
        db = self._makeDb({}, [])
        self.assertTrue(db.repo.isAlbumBioEnabled())
        db.repo.setAlbumBioEnabled(False)
        self.assertFalse(db.repo.isAlbumBioEnabled())
        db.repo.setAlbumBioEnabled(True)
        self.assertTrue(db.repo.isAlbumBioEnabled())


class GenreWriteTestCase(DatabaseTestCase):
    def _db(self):
        tracks = {"t1": {"id": "t1", "name": "Song", "artists": [{"id": "a1", "name": "Artist"}]}}
        entries = [{"id": "t1", "playedAt": 1000, "timePlayed": 5000}]
        return self._makeDb(tracks, entries)

    def test_artist_genres_round_trip_in_position_order(self):
        db = self._db()
        db.repo.replaceArtistGenres("a1", ["rock", "indie rock", "shoegaze"])
        self.assertEqual(db.repo.getArtistGenres("a1"), ["rock", "indie rock", "shoegaze"])

    def test_replace_overwrites_all_previous_rows(self):
        db = self._db()
        db.repo.replaceArtistGenres("a1", ["rock", "indie rock", "shoegaze"])
        db.repo.replaceArtistGenres("a1", ["pop"])
        self.assertEqual(db.repo.getArtistGenres("a1"), ["pop"])

    def test_track_genres_carry_the_inherited_flag(self):
        db = self._db()
        db.repo.replaceTrackGenres("t1", ["rock"], inherited=True)
        self.assertEqual(db.repo.getTrackGenres("t1"),
                         [{"genre": "rock", "inherited": True}])

        # An own-tags result later replaces inherited rows entirely.
        db.repo.replaceTrackGenres("t1", ["dream pop"], inherited=False)
        self.assertEqual(db.repo.getTrackGenres("t1"),
                         [{"genre": "dream pop", "inherited": False}])

    def test_album_genres_carry_the_inherited_flag(self):
        db = self._db()
        albumId = db.repo._conn().execute(
            "SELECT album_id FROM tracks WHERE id='t1'").fetchone()["album_id"]
        db.repo.replaceAlbumGenres(albumId, ["rock", "pop"], inherited=True)
        self.assertEqual(db.repo.getAlbumGenres(albumId),
                         [{"genre": "rock", "inherited": True},
                          {"genre": "pop", "inherited": True}])

    def test_mark_lastfm_attempted_stamps_each_kind(self):
        db = self._db()
        albumId = db.repo._conn().execute(
            "SELECT album_id FROM tracks WHERE id='t1'").fetchone()["album_id"]
        before = time.time()
        db.repo.markArtistsLastfmAttempted(["a1"])
        db.repo.markAlbumsLastfmAttempted([albumId])
        db.repo.markTracksLastfmAttempted(["t1"])
        conn = db.repo._conn()
        for table, idValue in (("artists", "a1"), ("albums", albumId), ("tracks", "t1")):
            stamp = conn.execute(
                f"SELECT lastfm_attempted_at FROM {table} WHERE id=?", (idValue,)
            ).fetchone()["lastfm_attempted_at"]
            self.assertIsNotNone(stamp)
            self.assertGreaterEqual(stamp, before)

    def test_mark_with_empty_list_is_a_noop(self):
        db = self._db()
        db.repo.markArtistsLastfmAttempted([])   #< must not raise

    def test_artist_lastfm_state_reflects_attempt_and_genres(self):
        db = self._db()
        state = db.repo.getArtistLastfmState("a1")
        self.assertIsNone(state["attempted_at"])
        self.assertEqual(state["genres"], [])

        db.repo.replaceArtistGenres("a1", ["rock"])
        db.repo.markArtistsLastfmAttempted(["a1"])
        state = db.repo.getArtistLastfmState("a1")
        self.assertIsNotNone(state["attempted_at"])
        self.assertEqual(state["genres"], ["rock"])

    def test_artist_bio_state_reflects_attempt_and_bio(self):
        db = self._db()
        state = db.repo.getArtistBioState("a1")
        self.assertIsNone(state["attempted_at"])
        self.assertIsNone(state["bio"])

        before = time.time()
        db.repo.setArtistBio("a1", "A band from somewhere.")
        state = db.repo.getArtistBioState("a1")
        self.assertEqual(state["bio"], "A band from somewhere.")
        self.assertIsNotNone(state["attempted_at"])
        self.assertGreaterEqual(state["attempted_at"], before)

    def test_artist_bio_state_stamps_attempted_even_with_no_bio(self):
        """A definitive "nothing usable" result still stamps attempted_at -
        same permanent-once-tried contract as artist images - so the lazy
        fetch never retries it."""
        db = self._db()
        db.repo.setArtistBio("a1", None)
        state = db.repo.getArtistBioState("a1")
        self.assertIsNone(state["bio"])
        self.assertIsNotNone(state["attempted_at"])

    def test_artist_bio_state_for_unknown_artist_reads_as_untried(self):
        db = self._db()
        state = db.repo.getArtistBioState("nonexistent")
        self.assertIsNone(state["bio"])
        self.assertIsNone(state["attempted_at"])

    def test_requeue_corrupted_biographies_clears_bios_missing_terminal_punctuation(self):
        """The 1.26.0 -> 1.27.0 migration's lever: bios fetched before the
        bio.content + sentence-boundary-truncation fix landed are stuck
        mid-sentence forever (bio IS NOT NULL, so they'd never re-enter
        getArtistsMissingBiographies on their own) - this clears them back
        to untried so the corrected extraction re-runs immediately."""
        db = self._db()
        db.repo.setArtistBio("a1", "This bio was cut off mid-sen")   #< no terminal punctuation

        cleared = db.repo.requeueCorruptedBiographies()

        self.assertEqual(cleared, 1)
        state = db.repo.getArtistBioState("a1")
        self.assertIsNone(state["bio"])
        self.assertIsNone(state["attempted_at"])

    def test_requeue_corrupted_biographies_leaves_well_formed_bios_alone(self):
        db = self._db()
        db.repo.setArtistBio("a1", "This bio ends properly.")

        cleared = db.repo.requeueCorruptedBiographies()

        self.assertEqual(cleared, 0)
        self.assertEqual(db.repo.getArtistBioState("a1")["bio"], "This bio ends properly.")

    def test_requeue_corrupted_biographies_accepts_exclamation_and_question_marks(self):
        db = self._db()
        db.repo.setArtistBio("a1", "What a band!")

        cleared = db.repo.requeueCorruptedBiographies()

        self.assertEqual(cleared, 0)

    def test_requeue_corrupted_biographies_ignores_artists_with_no_bio(self):
        """A NULL bio (never attempted, or a definitive "no bio available")
        isn't corrupted text - it must not be swept up and reset."""
        db = self._db()
        before = time.time()
        db.repo.setArtistBio("a1", None)

        cleared = db.repo.requeueCorruptedBiographies()

        self.assertEqual(cleared, 0)
        state = db.repo.getArtistBioState("a1")
        self.assertIsNone(state["bio"])
        self.assertGreaterEqual(state["attempted_at"], before)   #< untouched, not re-cleared


class GenreQueueTestCase(DatabaseTestCase):
    """Queue semantics: play-count priority, own vs global scope, and the
    retry TTL (attempted entities leave the queue; empty ones come back after
    GENRE_BACKFILL_RETRY_SECONDS; entities with own genres never do)."""

    def _db(self):
        tracks = {
            "tA": {"id": "tA", "name": "Song A",
                   "artists": [{"id": "aX", "name": "Artist X"}, {"id": "aF", "name": "Feature"}],
                   "album": self._album("alP", "Album P")},
            "tB": {"id": "tB", "name": "Song B",
                   "artists": [{"id": "aY", "name": "Artist Y"}],
                   "album": self._album("alQ", "Album Q")},
        }
        entries = [
            {"id": "tA", "playedAt": 1000, "timePlayed": 5000},
            {"id": "tA", "playedAt": 2000, "timePlayed": 5000},
            {"id": "tA", "playedAt": 3000, "timePlayed": 5000},
            {"id": "tB", "playedAt": 4000, "timePlayed": 5000},
        ]
        db = self._makeDb(tracks, entries, username="user1")
        # A second user in the same shared DB, playing their own track.
        db.repo.upsertUser("user2", "user2@example.com")
        from conftest import normalizeTrackForTest
        db.repo.upsertTrack(normalizeTrackForTest(
            {"id": "tC", "name": "Song C",
             "artists": [{"id": "aZ", "name": "Artist Z"}],
             "album": self._album("alR", "Album R")}))
        db.repo.insertPlay("user2", "tC", 5000, 5000, None)
        db.repo.commit()
        return db

    @staticmethod
    def _album(albumId, name):
        return {"id": albumId, "name": name, "url": "http://example.com/album",
                "imageId": albumId, "imageUrl": "", "totalTracks": 1, "releaseDate": 0}

    def test_artist_queue_is_play_count_ordered_within_own_scope(self):
        db = self._db()
        rows = db.repo.getArtistsMissingGenres(10, username="user1")
        # aX (position 0) and aF (position 1) are both credited on all 3 plays
        # of tA, so they tie on play_count and sort by id; aY trails on 1 play.
        self.assertEqual([r["id"] for r in rows], ["aF", "aX", "aY"])
        byId = {r["id"]: r for r in rows}
        self.assertEqual(byId["aX"]["play_count"], 3)
        self.assertEqual(byId["aX"]["name"], "Artist X")

    def test_artists_within_the_position_cutoff_are_queued(self):
        """aF is credited at position 1 on tA - within
        GENRE_BACKFILL_MAX_ARTIST_POSITION, so it's queued alongside the
        primary artist, not just position-0 artists."""
        db = self._db()
        rows = db.repo.getArtistsMissingGenres(10, username="user1")
        byId = {r["id"]: r for r in rows}
        self.assertIn("aF", byId)
        self.assertEqual(byId["aF"]["play_count"], 3)   #< credited on all 3 plays of tA

    def test_artists_beyond_the_position_cutoff_are_not_queued(self):
        db = self._db()
        from conftest import normalizeTrackForTest
        db.repo.upsertTrack(normalizeTrackForTest(
            {"id": "tD", "name": "Song D",
             "artists": [{"id": f"aP{i}", "name": f"Artist P{i}"} for i in range(6)],
             "album": self._album("alS", "Album S")}))
        db.repo.insertPlay("user1", "tD", 6000, 5000, None)
        db.repo.commit()

        rows = db.repo.getArtistsMissingGenres(20, username="user1")
        ids = [r["id"] for r in rows]
        for i in range(5):   #< positions 0-4, within the cutoff
            self.assertIn(f"aP{i}", ids)
        self.assertNotIn("aP5", ids)   #< position 5, beyond the cutoff

    def test_global_scope_spans_all_users(self):
        db = self._db()
        rows = db.repo.getArtistsMissingGenres(10)
        # aF ties aX at 3 plays (id-sorted first); aY ties aZ at 1 play.
        self.assertEqual([r["id"] for r in rows], ["aF", "aX", "aY", "aZ"])

    def test_limit_is_respected(self):
        db = self._db()
        rows = db.repo.getArtistsMissingGenres(1, username="user1")
        self.assertEqual([r["id"] for r in rows], ["aF"])

    def test_recently_attempted_entities_leave_the_queue(self):
        db = self._db()
        db.repo.markArtistsLastfmAttempted(["aX"])
        rows = db.repo.getArtistsMissingGenres(10, username="user1")
        self.assertEqual([r["id"] for r in rows], ["aF", "aY"])

    def test_empty_entities_requeue_after_the_retry_ttl(self):
        db = self._db()
        db.repo.markArtistsLastfmAttempted(["aX"])
        conn = db.repo._conn()
        staleStamp = time.time() - GENRE_BACKFILL_RETRY_SECONDS - 1
        with conn:
            conn.execute("UPDATE artists SET lastfm_attempted_at=? WHERE id='aX'", (staleStamp,))
        rows = db.repo.getArtistsMissingGenres(10, username="user1")
        self.assertIn("aX", [r["id"] for r in rows])

    def test_entities_with_own_genres_never_requeue(self):
        db = self._db()
        db.repo.replaceArtistGenres("aX", ["rock"])
        db.repo.markArtistsLastfmAttempted(["aX"])
        conn = db.repo._conn()
        staleStamp = time.time() - GENRE_BACKFILL_RETRY_SECONDS - 1
        with conn:
            conn.execute("UPDATE artists SET lastfm_attempted_at=? WHERE id='aX'", (staleStamp,))
        rows = db.repo.getArtistsMissingGenres(10, username="user1")
        self.assertNotIn("aX", [r["id"] for r in rows])

    def test_track_queue_carries_the_primary_artist(self):
        db = self._db()
        rows = db.repo.getTracksMissingGenres(10, username="user1")
        self.assertEqual([r["id"] for r in rows], ["tA", "tB"])
        self.assertEqual(rows[0]["artist_id"], "aX")
        self.assertEqual(rows[0]["artist_name"], "Artist X")
        self.assertEqual(rows[0]["name"], "Song A")

    def test_tracks_with_inherited_only_genres_requeue_after_the_ttl(self):
        db = self._db()
        db.repo.replaceTrackGenres("tA", ["rock"], inherited=True)
        db.repo.markTracksLastfmAttempted(["tA"])

        rows = db.repo.getTracksMissingGenres(10, username="user1")
        self.assertNotIn("tA", [r["id"] for r in rows])   #< fresh attempt: out of the queue

        conn = db.repo._conn()
        staleStamp = time.time() - GENRE_BACKFILL_RETRY_SECONDS - 1
        with conn:
            conn.execute("UPDATE tracks SET lastfm_attempted_at=? WHERE id='tA'", (staleStamp,))
        rows = db.repo.getTracksMissingGenres(10, username="user1")
        self.assertIn("tA", [r["id"] for r in rows])      #< inherited rows don't satisfy it

        db.repo.replaceTrackGenres("tA", ["rock"], inherited=False)
        rows = db.repo.getTracksMissingGenres(10, username="user1")
        self.assertNotIn("tA", [r["id"] for r in rows])   #< own rows do

    def test_tracks_without_a_primary_artist_are_not_queued(self):
        tracks = {"tOrphan": {"id": "tOrphan", "name": "No Artist", "artists": []}}
        entries = [{"id": "tOrphan", "playedAt": 1000, "timePlayed": 5000}]
        db = self._makeDb(tracks, entries)
        rows = db.repo.getTracksMissingGenres(10, username="testuser")
        self.assertEqual(rows, [])

    def test_album_queue_mirrors_track_semantics(self):
        db = self._db()
        rows = db.repo.getAlbumsMissingGenres(10, username="user1")
        self.assertEqual([r["id"] for r in rows], ["alP", "alQ"])
        self.assertEqual(rows[0]["name"], "Album P")
        self.assertEqual(rows[0]["play_count"], 3)

    def test_album_primary_artists_are_derived_from_their_tracks(self):
        db = self._db()
        primaries = db.repo.getAlbumPrimaryArtists(["alP", "alQ"])
        self.assertEqual(primaries["alP"], {"artist_id": "aX", "artist_name": "Artist X"})
        self.assertEqual(primaries["alQ"], {"artist_id": "aY", "artist_name": "Artist Y"})

    def test_album_primary_artist_prefers_the_most_frequent_with_stable_ties(self):
        from conftest import normalizeTrackForTest
        db = self._db()
        # alS has two tracks with different primary artists -> tie broken by id.
        for trackId, artistId in (("tS1", "aM"), ("tS2", "aB")):
            db.repo.upsertTrack(normalizeTrackForTest(
                {"id": trackId, "name": trackId,
                 "artists": [{"id": artistId, "name": artistId}],
                 "album": self._album("alS", "Album S")}))
        db.repo.commit()
        primaries = db.repo.getAlbumPrimaryArtists(["alS"])
        self.assertEqual(primaries["alS"]["artist_id"], "aB")

    def test_album_primary_artists_with_no_input_or_unknown_ids(self):
        db = self._db()
        self.assertEqual(db.repo.getAlbumPrimaryArtists([]), {})
        self.assertEqual(db.repo.getAlbumPrimaryArtists(["missing"]), {})


class BiographyQueueTestCase(DatabaseTestCase):
    """getArtistsMissingBiographies: the background biography backfiller's
    queue. Same play-count-ordered, own-vs-global-scope shape as
    getArtistsMissingGenres, but the retry condition keys off bio (not a
    join table) - a stale attempt only requeues while bio is still NULL, an
    artist with real bio text never does."""

    def _db(self):
        tracks = {
            "tA": {"id": "tA", "name": "Song A",
                   "artists": [{"id": "aX", "name": "Artist X"}, {"id": "aF", "name": "Feature"}],
                   "album": self._album("alP", "Album P")},
            "tB": {"id": "tB", "name": "Song B",
                   "artists": [{"id": "aY", "name": "Artist Y"}],
                   "album": self._album("alQ", "Album Q")},
        }
        entries = [
            {"id": "tA", "playedAt": 1000, "timePlayed": 5000},
            {"id": "tA", "playedAt": 2000, "timePlayed": 5000},
            {"id": "tA", "playedAt": 3000, "timePlayed": 5000},
            {"id": "tB", "playedAt": 4000, "timePlayed": 5000},
        ]
        db = self._makeDb(tracks, entries, username="user1")
        # A second user in the same shared DB, playing their own track.
        db.repo.upsertUser("user2", "user2@example.com")
        from conftest import normalizeTrackForTest
        db.repo.upsertTrack(normalizeTrackForTest(
            {"id": "tC", "name": "Song C",
             "artists": [{"id": "aZ", "name": "Artist Z"}],
             "album": self._album("alR", "Album R")}))
        db.repo.insertPlay("user2", "tC", 5000, 5000, None)
        db.repo.commit()
        return db

    @staticmethod
    def _album(albumId, name):
        return {"id": albumId, "name": name, "url": "http://example.com/album",
                "imageId": albumId, "imageUrl": "", "totalTracks": 1, "releaseDate": 0}

    def test_artist_queue_is_play_count_ordered_within_own_scope(self):
        db = self._db()
        rows = db.repo.getArtistsMissingBiographies(10, username="user1")
        self.assertEqual([r["id"] for r in rows], ["aX", "aY"])
        self.assertEqual(rows[0]["play_count"], 3)
        self.assertEqual(rows[0]["name"], "Artist X")

    def test_featured_artists_are_not_queued(self):
        db = self._db()
        rows = db.repo.getArtistsMissingBiographies(10, username="user1")
        self.assertNotIn("aF", [r["id"] for r in rows])   #< only position-0 artists

    def test_global_scope_spans_all_users(self):
        db = self._db()
        rows = db.repo.getArtistsMissingBiographies(10)
        self.assertEqual([r["id"] for r in rows], ["aX", "aY", "aZ"])   #< 3 plays, then ties by id

    def test_limit_is_respected(self):
        db = self._db()
        rows = db.repo.getArtistsMissingBiographies(1, username="user1")
        self.assertEqual([r["id"] for r in rows], ["aX"])

    def test_recently_attempted_entities_leave_the_queue(self):
        db = self._db()
        db.repo.setArtistBio("aX", "A bio.")
        rows = db.repo.getArtistsMissingBiographies(10, username="user1")
        self.assertEqual([r["id"] for r in rows], ["aY"])

    def test_empty_bios_requeue_after_the_retry_ttl(self):
        db = self._db()
        db.repo.setArtistBio("aX", None)
        conn = db.repo._conn()
        staleStamp = time.time() - BIOGRAPHY_BACKFILL_RETRY_SECONDS - 1
        with conn:
            conn.execute("UPDATE artists SET bio_attempted_at=? WHERE id='aX'", (staleStamp,))
        rows = db.repo.getArtistsMissingBiographies(10, username="user1")
        self.assertIn("aX", [r["id"] for r in rows])

    def test_artists_with_a_real_bio_never_requeue(self):
        db = self._db()
        db.repo.setArtistBio("aX", "A real bio.")
        conn = db.repo._conn()
        staleStamp = time.time() - BIOGRAPHY_BACKFILL_RETRY_SECONDS - 1
        with conn:
            conn.execute("UPDATE artists SET bio_attempted_at=? WHERE id='aX'", (staleStamp,))
        rows = db.repo.getArtistsMissingBiographies(10, username="user1")
        self.assertNotIn("aX", [r["id"] for r in rows])


class AlbumBioStateTestCase(DatabaseTestCase):
    """getAlbumBioState/setAlbumBio: mirrors the artist-bio state round trip
    (getArtistBioState/setArtistBio) for albums."""

    def test_unset_album_reads_as_unattempted(self):
        db = self._makeDb({}, [])
        state = db.repo.getAlbumBioState("unknown")
        self.assertIsNone(state["bio"])
        self.assertIsNone(state["attempted_at"])

    def test_bio_and_attempted_at_round_trip(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('al1', 'Album', '')")

        db.repo.setAlbumBio("al1", "A great album.")

        state = db.repo.getAlbumBioState("al1")
        self.assertEqual(state["bio"], "A great album.")
        self.assertIsNotNone(state["attempted_at"])

    def test_definitive_no_bio_still_stamps_attempted(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('al1', 'Album', '')")

        db.repo.setAlbumBio("al1", None)

        state = db.repo.getAlbumBioState("al1")
        self.assertIsNone(state["bio"])
        self.assertIsNotNone(state["attempted_at"])

    def test_requeue_decorated_albums_without_bios_clears_only_the_stuck_decorated_ones(self):
        """The 1.28.0 -> 1.29.0 migration's lever: albums attempted before the
        album-bio lookup gained cleanLookupName's decoration-stripping retry
        are stuck (bio IS NULL, bio_attempted_at IS NOT NULL) - only the
        decorated ones are cleared back to untried so the fixed lookup retries
        with the undecorated title. Undecorated no-bio albums, decorated
        albums that DID get a bio, and never-attempted albums are left alone."""
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alDeluxe', 'Album D (Deluxe Edition)', '')")
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alPlain', 'Album P', '')")
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alDeluxeWithBio', 'Album W (Deluxe Edition)', '')")
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alNeverTried', 'Album N (Deluxe Edition)', '')")
        db.repo.setAlbumBio("alDeluxe", None)                 #< decorated, attempted, no bio -> requeue
        db.repo.setAlbumBio("alPlain", None)                  #< undecorated, attempted, no bio -> leave
        db.repo.setAlbumBio("alDeluxeWithBio", "Has a bio.")  #< decorated but has a bio -> leave

        cleared = db.repo.requeueDecoratedAlbumsWithoutBios()

        self.assertEqual(cleared, 1)
        self.assertIsNone(db.repo.getAlbumBioState("alDeluxe")["attempted_at"])
        self.assertIsNotNone(db.repo.getAlbumBioState("alPlain")["attempted_at"])
        self.assertEqual(db.repo.getAlbumBioState("alDeluxeWithBio")["bio"], "Has a bio.")
        self.assertIsNotNone(db.repo.getAlbumBioState("alDeluxeWithBio")["attempted_at"])
        self.assertIsNone(db.repo.getAlbumBioState("alNeverTried")["attempted_at"])


class AlbumBiographyQueueTestCase(DatabaseTestCase):
    """getAlbumsMissingBiographies: same play-count-ordered, own-vs-global-
    scope shape as BiographyQueueTestCase's artist queue, keyed off
    albums.bio."""

    def _db(self):
        tracks = {
            "tA": {"id": "tA", "name": "Song A",
                   "artists": [{"id": "aX", "name": "Artist X"}],
                   "album": self._album("alP", "Album P")},
            "tB": {"id": "tB", "name": "Song B",
                   "artists": [{"id": "aY", "name": "Artist Y"}],
                   "album": self._album("alQ", "Album Q")},
        }
        entries = [
            {"id": "tA", "playedAt": 1000, "timePlayed": 5000},
            {"id": "tA", "playedAt": 2000, "timePlayed": 5000},
            {"id": "tA", "playedAt": 3000, "timePlayed": 5000},
            {"id": "tB", "playedAt": 4000, "timePlayed": 5000},
        ]
        db = self._makeDb(tracks, entries, username="user1")
        # A second user in the same shared DB, playing their own album.
        db.repo.upsertUser("user2", "user2@example.com")
        from conftest import normalizeTrackForTest
        db.repo.upsertTrack(normalizeTrackForTest(
            {"id": "tC", "name": "Song C",
             "artists": [{"id": "aZ", "name": "Artist Z"}],
             "album": self._album("alR", "Album R")}))
        db.repo.insertPlay("user2", "tC", 5000, 5000, None)
        db.repo.commit()
        return db

    @staticmethod
    def _album(albumId, name):
        return {"id": albumId, "name": name, "url": "http://example.com/album",
                "imageId": albumId, "imageUrl": "", "totalTracks": 1, "releaseDate": 0}

    def test_album_queue_is_play_count_ordered_within_own_scope(self):
        db = self._db()
        rows = db.repo.getAlbumsMissingBiographies(10, username="user1")
        self.assertEqual([r["id"] for r in rows], ["alP", "alQ"])
        self.assertEqual(rows[0]["play_count"], 3)
        self.assertEqual(rows[0]["name"], "Album P")

    def test_global_scope_spans_all_users(self):
        db = self._db()
        rows = db.repo.getAlbumsMissingBiographies(10)
        self.assertEqual([r["id"] for r in rows], ["alP", "alQ", "alR"])

    def test_limit_is_respected(self):
        db = self._db()
        rows = db.repo.getAlbumsMissingBiographies(1, username="user1")
        self.assertEqual([r["id"] for r in rows], ["alP"])

    def test_recently_attempted_entities_leave_the_queue(self):
        db = self._db()
        db.repo.setAlbumBio("alP", "A bio.")
        rows = db.repo.getAlbumsMissingBiographies(10, username="user1")
        self.assertEqual([r["id"] for r in rows], ["alQ"])

    def test_empty_bios_requeue_after_the_retry_ttl(self):
        db = self._db()
        db.repo.setAlbumBio("alP", None)
        conn = db.repo._conn()
        staleStamp = time.time() - BIOGRAPHY_BACKFILL_RETRY_SECONDS - 1
        with conn:
            conn.execute("UPDATE albums SET bio_attempted_at=? WHERE id='alP'", (staleStamp,))
        rows = db.repo.getAlbumsMissingBiographies(10, username="user1")
        self.assertIn("alP", [r["id"] for r in rows])

    def test_albums_with_a_real_bio_never_requeue(self):
        db = self._db()
        db.repo.setAlbumBio("alP", "A real bio.")
        conn = db.repo._conn()
        staleStamp = time.time() - BIOGRAPHY_BACKFILL_RETRY_SECONDS - 1
        with conn:
            conn.execute("UPDATE albums SET bio_attempted_at=? WHERE id='alP'", (staleStamp,))
        rows = db.repo.getAlbumsMissingBiographies(10, username="user1")
        self.assertNotIn("alP", [r["id"] for r in rows])


class BiographyCoverageTestCase(DatabaseTestCase):
    """getBiographyCoverage: entity-count (not play-weighted) coverage for
    the Overview "Biography Backfill Progress" widget."""

    def _db(self):
        tracks = {
            "tA": {"id": "tA", "name": "Song A",
                   "artists": [{"id": "aX", "name": "Artist X"}],
                   "album": self._album("alP", "Album P")},
            "tB": {"id": "tB", "name": "Song B",
                   "artists": [{"id": "aY", "name": "Artist Y"}],
                   "album": self._album("alQ", "Album Q")},
        }
        entries = [
            {"id": "tA", "playedAt": 1000, "timePlayed": 5000},
            {"id": "tB", "playedAt": 2000, "timePlayed": 5000},
        ]
        return self._makeDb(tracks, entries, username="user1")

    @staticmethod
    def _album(albumId, name):
        return {"id": albumId, "name": name, "url": "http://example.com/album",
                "imageId": albumId, "imageUrl": "", "totalTracks": 1, "releaseDate": 0}

    def test_nothing_backfilled_yet(self):
        db = self._db()
        coverage = db.repo.getBiographyCoverage("user1")
        self.assertEqual(coverage["artist"], {"covered": 0, "total": 2})
        self.assertEqual(coverage["album"], {"covered": 0, "total": 2})

    def test_covered_counts_rise_as_bios_are_stored(self):
        db = self._db()
        db.repo.setArtistBio("aX", "Bio X")
        db.repo.setAlbumBio("alP", "Album bio P")

        coverage = db.repo.getBiographyCoverage("user1")

        self.assertEqual(coverage["artist"], {"covered": 1, "total": 2})
        self.assertEqual(coverage["album"], {"covered": 1, "total": 2})

    def test_a_definitive_no_bio_does_not_count_as_covered(self):
        db = self._db()
        db.repo.setArtistBio("aX", None)
        db.repo.setAlbumBio("alP", None)

        coverage = db.repo.getBiographyCoverage("user1")

        self.assertEqual(coverage["artist"]["covered"], 0)
        self.assertEqual(coverage["album"]["covered"], 0)

    def test_scoped_to_the_requested_user(self):
        db = self._db()
        db.repo.upsertUser("user2", "user2@example.com")
        from conftest import normalizeTrackForTest
        db.repo.upsertTrack(normalizeTrackForTest(
            {"id": "tC", "name": "Song C",
             "artists": [{"id": "aZ", "name": "Artist Z"}],
             "album": self._album("alR", "Album R")}))
        db.repo.insertPlay("user2", "tC", 5000, 5000, None)
        db.repo.commit()

        coverage = db.repo.getBiographyCoverage("user1")

        self.assertEqual(coverage["artist"]["total"], 2)   #< user2's aZ excluded
        self.assertEqual(coverage["album"]["total"], 2)


class GenreCoverageTestCase(DatabaseTestCase):
    """Play-weighted coverage: t1 fully covered (2 plays), t2 covered only via
    an inherited track genre + its artist (1 play), t3 uncovered (1 play)."""

    def _db(self):
        album = lambda albumId: {"id": albumId, "name": albumId, "url": "u",
                                 "imageId": albumId, "imageUrl": "", "totalTracks": 1, "releaseDate": 0}
        tracks = {
            "t1": {"id": "t1", "name": "One", "artists": [{"id": "aX", "name": "X"}], "album": album("alP")},
            "t2": {"id": "t2", "name": "Two", "artists": [{"id": "aX", "name": "X"}], "album": album("alQ")},
            "t3": {"id": "t3", "name": "Three", "artists": [{"id": "aZ", "name": "Z"}], "album": album("alR")},
        }
        entries = [
            {"id": "t1", "playedAt": 1000, "timePlayed": 5000},
            {"id": "t1", "playedAt": 2000, "timePlayed": 5000},
            {"id": "t2", "playedAt": 3000, "timePlayed": 5000},
            {"id": "t3", "playedAt": 4000, "timePlayed": 5000},
        ]
        db = self._makeDb(tracks, entries)
        db.repo.replaceArtistGenres("aX", ["rock"])
        db.repo.replaceTrackGenres("t1", ["rock"], inherited=False)
        db.repo.replaceAlbumGenres("alP", ["rock"], inherited=False)
        db.repo.replaceTrackGenres("t2", ["rock"], inherited=True)
        return db

    def test_counts_and_percentages_including_inherited(self):
        db = self._db()
        coverage = db.getGenreCoverage(includeInherited=True)
        self.assertEqual(coverage["song"], {"covered": 3, "total": 4, "percent": 75.0, "ownPercent": 50.0})
        self.assertEqual(coverage["album"], {"covered": 2, "total": 4, "percent": 50.0, "ownPercent": 50.0})
        self.assertEqual(coverage["artist"], {"covered": 3, "total": 4, "percent": 75.0, "ownPercent": 75.0})
        self.assertAlmostEqual(coverage["overall"]["percent"], 66.7, places=1)

    def test_excluding_inherited_drops_only_inherited_categories(self):
        db = self._db()
        coverage = db.getGenreCoverage(includeInherited=False)
        self.assertEqual(coverage["song"]["covered"], 2)     #< t2's inherited row no longer counts
        self.assertEqual(coverage["album"]["covered"], 2)
        self.assertEqual(coverage["artist"]["covered"], 3)   #< artists have no inherited concept

    def test_own_percent_ignores_the_inherited_toggle(self):
        """ownPercent always counts non-inherited rows only - the coverage
        panel's "own tags" split must not move with the admin toggle. For
        artists (no inherited concept) it equals the plain percent."""
        db = self._db()
        for includeInherited in (True, False):
            coverage = db.getGenreCoverage(includeInherited=includeInherited)
            self.assertEqual(coverage["song"]["ownPercent"], 50.0)     #< only t1's 2 own-tag plays
            self.assertEqual(coverage["album"]["ownPercent"], 50.0)
            self.assertEqual(coverage["artist"]["ownPercent"],
                             coverage["artist"]["percent"])

    def test_default_reads_the_app_setting(self):
        db = self._db()
        db.repo.setInheritedGenresEnabled(False)
        self.assertEqual(db.getGenreCoverage()["song"]["covered"], 2)
        db.repo.setInheritedGenresEnabled(True)
        self.assertEqual(db.getGenreCoverage()["song"]["covered"], 3)

    def test_date_range_scopes_the_denominator(self):
        db = self._db()
        coverage = db.getGenreCoverage(startDate=_dt(2500), endDate=_dt(4500), includeInherited=True)
        self.assertEqual(coverage["song"], {"covered": 1, "total": 2, "percent": 50.0, "ownPercent": 0.0})

    def test_no_plays_yields_zeros_without_dividing(self):
        db = self._makeDb({}, [])
        coverage = db.getGenreCoverage()
        for category in ("song", "album", "artist"):
            self.assertEqual(coverage[category],
                             {"covered": 0, "total": 0, "percent": 0.0, "ownPercent": 0.0})
        self.assertEqual(coverage["overall"]["percent"], 0.0)


class GenreDistributionTestCase(DatabaseTestCase):
    def _db(self):
        tracks = {
            "t1": {"id": "t1", "name": "One", "artists": [{"id": "aX", "name": "X"}]},
            "t2": {"id": "t2", "name": "Two", "artists": [{"id": "aX", "name": "X"}]},
        }
        entries = [
            {"id": "t1", "playedAt": 1000, "timePlayed": 5000},
            {"id": "t1", "playedAt": 2000, "timePlayed": 5000},
            {"id": "t2", "playedAt": 3000, "timePlayed": 5000},
        ]
        db = self._makeDb(tracks, entries)
        db.repo.replaceTrackGenres("t1", ["rock", "indie rock"], inherited=False)
        db.repo.replaceTrackGenres("t2", ["rock"], inherited=True)
        return db

    def test_counts_each_play_once_per_genre_ordered_by_plays_then_name(self):
        db = self._db()
        distribution = db.getGenreDistribution(includeInherited=True)
        self.assertEqual(distribution, {"rock": 3, "indie rock": 2})
        self.assertEqual(list(distribution), ["rock", "indie rock"])

    def test_inherited_rows_can_be_excluded(self):
        db = self._db()
        distribution = db.getGenreDistribution(includeInherited=False)
        self.assertEqual(distribution, {"rock": 2, "indie rock": 2})

    def test_default_reads_the_app_setting(self):
        db = self._db()
        db.repo.setInheritedGenresEnabled(False)
        self.assertEqual(db.getGenreDistribution()["rock"], 2)

    def test_limit_and_range(self):
        db = self._db()
        distribution = db.getGenreDistribution(limit=1, includeInherited=True)
        self.assertEqual(distribution, {"rock": 3})
        distribution = db.getGenreDistribution(startDate=_dt(2500), includeInherited=True)
        self.assertEqual(distribution, {"rock": 1})

    def test_alphabetical_tiebreak(self):
        db = self._db()
        db.repo.replaceTrackGenres("t1", ["zeta", "alpha"], inherited=False)
        db.repo.replaceTrackGenres("t2", [], inherited=False)
        distribution = db.getGenreDistribution(includeInherited=True)
        self.assertEqual(list(distribution), ["alpha", "zeta"])


class GenresForEntityTestCase(DatabaseTestCase):
    """Database.getGenresFor{Track,Album,Artist} - the per-item lookups the
    track-card genre badge uses, exposing Repository.getTrackGenres/
    getAlbumGenres/getArtistGenres on the Database facade with the same
    inherited-genre toggle every other genre stat respects (artists have no
    inherited concept - nothing to toggle there)."""

    def _db(self):
        album = lambda albumId: {"id": albumId, "name": albumId, "url": "u",
                                 "imageId": albumId, "imageUrl": "", "totalTracks": 1, "releaseDate": 0}
        tracks = {
            "t1": {"id": "t1", "name": "One", "artists": [{"id": "aX", "name": "X"}], "album": album("alP")},
        }
        entries = [{"id": "t1", "playedAt": 1000, "timePlayed": 5000}]
        db = self._makeDb(tracks, entries)
        db.repo.replaceArtistGenres("aX", ["rock", "post-punk"])
        db.repo.replaceTrackGenres("t1", ["dream pop"], inherited=False)
        db.repo.replaceAlbumGenres("alP", ["indie rock"], inherited=True)
        return db

    def test_track_genres_returns_names_in_position_order(self):
        db = self._db()
        self.assertEqual(db.getGenresForTrack("t1"), ["dream pop"])

    def test_track_genres_empty_for_untagged_track(self):
        db = self._db()
        self.assertEqual(db.getGenresForTrack("nope"), [])

    def test_track_genres_respects_inherited_toggle(self):
        db = self._db()
        db.repo.replaceTrackGenres("t1", ["dream pop"], inherited=True)
        self.assertEqual(db.getGenresForTrack("t1", includeInherited=True), ["dream pop"])
        self.assertEqual(db.getGenresForTrack("t1", includeInherited=False), [])

    def test_album_genres_respects_inherited_toggle(self):
        db = self._db()
        self.assertEqual(db.getGenresForAlbum("alP", includeInherited=True), ["indie rock"])
        self.assertEqual(db.getGenresForAlbum("alP", includeInherited=False), [])

    def test_album_genres_default_reads_the_app_setting(self):
        db = self._db()
        db.repo.setInheritedGenresEnabled(False)
        self.assertEqual(db.getGenresForAlbum("alP"), [])
        db.repo.setInheritedGenresEnabled(True)
        self.assertEqual(db.getGenresForAlbum("alP"), ["indie rock"])

    def test_album_genres_empty_for_untagged_album(self):
        db = self._db()
        self.assertEqual(db.getGenresForAlbum("nope"), [])

    def test_artist_genres_returns_names_in_position_order(self):
        db = self._db()
        self.assertEqual(db.getGenresForArtist("aX"), ["rock", "post-punk"])

    def test_artist_genres_empty_for_untagged_artist(self):
        db = self._db()
        self.assertEqual(db.getGenresForArtist("nope"), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
