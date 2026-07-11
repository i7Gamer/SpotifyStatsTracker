import unittest
import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import (
    Repository, IMAGE_KIND_TRACK, IMAGE_STATUS_OK, IMAGE_STATUS_FAILED,
)


def makeTrack(trackId="t1", name="Song One", albumId="alb1", artistId="art1"):
    return {
        "id": trackId,
        "name": name,
        "url": f"http://example.com/track/{trackId}",
        "artists": [
            {"id": artistId, "name": "Artist One", "url": f"http://example.com/artist/{artistId}",
             "imageUrl": "", "imageId": artistId},
        ],
        "album": {
            "id": albumId, "name": "Album One", "url": f"http://example.com/album/{albumId}",
            "imageId": albumId, "imageUrl": "http://img.example.com/a.jpg",
            "totalTracks": 10, "releaseDate": 12345.0,
        },
        "imageUrl": "http://img.example.com/a.jpg",
        "imageId": albumId,
        "duration": 200000,
        "explicit": False,
        "isrc": "US1234567890",
        "discNumber": 1,
        "trackNumber": 3,
        "releaseDate": 12345.0,
    }


class RepositoryTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")

    def tearDown(self):
        self.repo.connectionManager.close()
        self._tmpdir.cleanup()


class TestTrackCatalog(RepositoryTestCase):
    def test_roundtrip_matches_input_shape(self):
        track = makeTrack()
        self.repo.upsertTrack(track)

        fetched = self.repo.getTrack("t1")

        self.assertEqual(fetched["id"], "t1")
        self.assertEqual(fetched["name"], "Song One")
        self.assertEqual(fetched["duration"], 200000)
        self.assertEqual(fetched["explicit"], False)
        self.assertEqual(fetched["isrc"], "US1234567890")
        self.assertEqual(fetched["discNumber"], 1)
        self.assertEqual(fetched["trackNumber"], 3)
        self.assertEqual(fetched["album"]["id"], "alb1")
        self.assertEqual(fetched["album"]["totalTracks"], 10)
        self.assertEqual(len(fetched["artists"]), 1)
        self.assertEqual(fetched["artists"][0]["id"], "art1")
        self.assertEqual(fetched["artists"][0]["name"], "Artist One")

    def test_unknown_track_returns_none(self):
        self.assertIsNone(self.repo.getTrack("missing"))

    def test_track_exists(self):
        self.assertFalse(self.repo.trackExists("t1"))
        self.repo.upsertTrack(makeTrack())
        self.assertTrue(self.repo.trackExists("t1"))

    def test_multi_artist_order_preserved(self):
        track = makeTrack()
        track["artists"] = [
            {"id": "a1", "name": "First", "url": "u", "imageUrl": "", "imageId": "a1"},
            {"id": "a2", "name": "Second", "url": "u", "imageUrl": "", "imageId": "a2"},
            {"id": "a3", "name": "Third", "url": "u", "imageUrl": "", "imageId": "a3"},
        ]
        self.repo.upsertTrack(track)

        fetched = self.repo.getTrack("t1")

        self.assertEqual([a["id"] for a in fetched["artists"]], ["a1", "a2", "a3"])

    def test_second_upsert_overwrites_and_replaces_artists(self):
        """Last write wins, matching the old tracks[id] = track dict-assignment
        semantics - including dropping artists no longer present."""
        track = makeTrack()
        self.repo.upsertTrack(track)

        updated = makeTrack()
        updated["name"] = "Song One (Remastered)"
        updated["artists"] = [
            {"id": "art2", "name": "Artist Two", "url": "u", "imageUrl": "", "imageId": "art2"},
        ]
        self.repo.upsertTrack(updated)

        fetched = self.repo.getTrack("t1")
        self.assertEqual(fetched["name"], "Song One (Remastered)")
        self.assertEqual([a["id"] for a in fetched["artists"]], ["art2"])

    def test_shared_album_and_artist_are_not_duplicated_across_tracks(self):
        trackA = makeTrack(trackId="t1", albumId="alb1", artistId="art1")
        trackB = makeTrack(trackId="t2", albumId="alb1", artistId="art1")
        self.repo.upsertTrack(trackA)
        self.repo.upsertTrack(trackB)

        conn = self.repo._conn()
        albumCount = conn.execute("SELECT COUNT(*) AS c FROM albums WHERE id='alb1'").fetchone()["c"]
        artistCount = conn.execute("SELECT COUNT(*) AS c FROM artists WHERE id='art1'").fetchone()["c"]
        self.assertEqual(albumCount, 1)
        self.assertEqual(artistCount, 1)


class TestPlaylistCatalog(RepositoryTestCase):
    def test_roundtrip(self):
        self.assertFalse(self.repo.playlistKnown("p1", "playlist"))
        self.repo.upsertPlaylistName("p1", "playlist", "My Playlist")
        self.assertTrue(self.repo.playlistKnown("p1", "playlist"))
        self.assertEqual(self.repo.getPlaylistName("p1", "playlist"), "My Playlist")

    def test_album_and_playlist_ids_are_independent_namespaces(self):
        self.repo.upsertPlaylistName("x1", "album", "Album Name")
        self.assertIsNone(self.repo.getPlaylistName("x1", "playlist"))
        self.assertEqual(self.repo.getPlaylistName("x1", "album"), "Album Name")

    def test_private_playlist_name_is_stored_as_none(self):
        self.repo.upsertPlaylistName("p2", "playlist", None)
        self.assertTrue(self.repo.playlistKnown("p2", "playlist"))
        self.assertIsNone(self.repo.getPlaylistName("p2", "playlist"))


class TestImageClaiming(RepositoryTestCase):
    def test_first_claim_succeeds_second_is_blocked(self):
        self.assertTrue(self.repo.tryClaimImageDownload("img1", IMAGE_KIND_TRACK))
        self.assertFalse(self.repo.tryClaimImageDownload("img1", IMAGE_KIND_TRACK))

    def test_claim_blocked_once_marked_ok(self):
        self.repo.tryClaimImageDownload("img1", IMAGE_KIND_TRACK)
        self.repo.markImageStatus("img1", IMAGE_KIND_TRACK, IMAGE_STATUS_OK)
        self.assertFalse(self.repo.tryClaimImageDownload("img1", IMAGE_KIND_TRACK))
        self.assertEqual(self.repo.imageStatus("img1", IMAGE_KIND_TRACK), IMAGE_STATUS_OK)

    def test_failed_download_can_be_reclaimed(self):
        self.repo.tryClaimImageDownload("img1", IMAGE_KIND_TRACK)
        self.repo.markImageStatus("img1", IMAGE_KIND_TRACK, IMAGE_STATUS_FAILED)
        self.assertTrue(self.repo.tryClaimImageDownload("img1", IMAGE_KIND_TRACK))

    def test_track_and_artist_kinds_are_independent(self):
        self.assertTrue(self.repo.tryClaimImageDownload("shared-id", "track"))
        self.assertTrue(self.repo.tryClaimImageDownload("shared-id", "artist"))


class TestPlaysHistory(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.upsertTrack(makeTrack(trackId="t2"))

    def test_insert_and_count(self):
        self.assertTrue(self.repo.insertPlay("alice", "t1", 1000.0, 5000))
        self.assertEqual(self.repo.getPlaysCount("alice"), 1)

    def test_exact_duplicate_play_is_rejected(self):
        self.assertTrue(self.repo.insertPlay("alice", "t1", 1000.0, 5000))
        self.assertFalse(self.repo.insertPlay("alice", "t1", 1000.0, 5000))
        self.assertEqual(self.repo.getPlaysCount("alice"), 1)

    def test_same_track_replayed_at_different_time_is_allowed(self):
        self.assertTrue(self.repo.insertPlay("alice", "t1", 1000.0, 5000))
        self.assertTrue(self.repo.insertPlay("alice", "t1", 2000.0, 5000))
        self.assertEqual(self.repo.getPlaysCount("alice"), 2)

    def test_newest_first_ordering(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t2", 3000.0, 5000)
        self.repo.insertPlay("alice", "t1", 2000.0, 5000)

        entries = self.repo.getPlaysNewestFirst("alice")

        self.assertEqual([e["playedAt"] for e in entries], [3000.0, 2000.0, 1000.0])

    def test_oldest_first_ordering(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t2", 3000.0, 5000)
        self.repo.insertPlay("alice", "t1", 2000.0, 5000)

        entries = self.repo.getPlaysOldestFirst("alice")

        self.assertEqual([e["playedAt"] for e in entries], [1000.0, 2000.0, 3000.0])

    def test_pagination_count_and_start_index(self):
        for i in range(5):
            self.repo.insertPlay("alice", "t1", float(i), 5000)

        page = self.repo.getPlaysNewestFirst("alice", count=2, startIndex=1)

        self.assertEqual([e["playedAt"] for e in page], [3.0, 2.0])

    def test_plays_are_scoped_per_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("bob", "t1", 1000.0, 5000)

        self.assertEqual(self.repo.getPlaysCount("alice"), 1)
        self.assertEqual(self.repo.getPlaysCount("bob"), 1)

    def test_played_from_is_preserved(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000, playedFrom="playlist:xyz")
        entries = self.repo.getPlaysNewestFirst("alice")
        self.assertEqual(entries[0]["playedFrom"], "playlist:xyz")


class TestTransactionControl(RepositoryTestCase):
    """upsertTrack/insertPlay don't auto-commit, so a caller (e.g. a bulk import)
    can compose several of them into one all-or-nothing transaction."""

    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")

    def test_uncommitted_track_upsert_is_still_visible_on_same_connection(self):
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.assertTrue(self.repo.trackExists("t1"))  #< read-your-own-writes, no commit() call yet

    def test_uncommitted_play_insert_is_still_visible_on_same_connection(self):
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.assertEqual(self.repo.getPlaysCount("alice"), 1)

    def test_rollback_discards_uncommitted_track_and_play(self):
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)

        self.repo.rollback()

        self.assertFalse(self.repo.trackExists("t1"))
        self.assertEqual(self.repo.getPlaysCount("alice"), 0)

    def test_commit_then_new_connection_sees_the_data(self):
        """Simulates a second thread (a fresh connection to the same file) reading
        after commit() - the real cross-thread visibility the app depends on."""
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.commit()

        otherConnRepo = Repository(self.repo.connectionManager.dbPath)
        try:
            self.assertTrue(otherConnRepo.trackExists("t1"))
            self.assertEqual(otherConnRepo.getPlaysCount("alice"), 1)
        finally:
            otherConnRepo.connectionManager.close()


class TestStatsAggregates(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")

    def _track(self, trackId, albumId, *artistIds):
        track = makeTrack(trackId=trackId, albumId=albumId)
        track["artists"] = [
            {"id": aid, "name": f"Artist {aid}", "url": "u", "imageUrl": "", "imageId": aid}
            for aid in artistIds
        ]
        return track

    def test_get_all_tracks_reconstructs_every_track(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1", "a2"))

        tracks = {t["id"]: t for t in self.repo.getAllTracks()}

        self.assertEqual(set(tracks.keys()), {"t1", "t2"})
        self.assertEqual([a["id"] for a in tracks["t2"]["artists"]], ["a1", "a2"])

    def test_play_aggregates_by_track(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 200.0, 2000)
        self.repo.commit()

        aggregates = self.repo.getPlayAggregatesByTrack("alice")

        self.assertEqual(len(aggregates), 1)
        self.assertEqual(aggregates[0]["trackId"], "t1")
        self.assertEqual(aggregates[0]["plays"], 2)
        self.assertEqual(aggregates[0]["totalTimeListened"], 3000)
        self.assertEqual(aggregates[0]["firstListenedAt"], 100.0)

    def test_play_aggregates_respect_date_range(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 5000.0, 2000)
        self.repo.commit()

        aggregates = self.repo.getPlayAggregatesByTrack("alice", startTs=0, endTs=1000)

        self.assertEqual(aggregates[0]["plays"], 1)
        self.assertEqual(aggregates[0]["totalTimeListened"], 1000)

    def test_artist_aggregates_grouped_by_artist_id_not_name(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.commit()

        aggregates = {a["id"]: a for a in self.repo.getArtistAggregates("alice")}

        self.assertEqual(set(aggregates.keys()), {"a1", "a2"})
        self.assertEqual(aggregates["a1"]["plays"], 1)
        self.assertEqual(aggregates["a1"]["totalTimeListened"], 1000)
        self.assertEqual(aggregates["a1"]["uniqueSongCount"], 1)
        self.assertEqual(aggregates["a1"]["firstListenedAt"], 100.0)

    def test_artist_aggregates_unique_song_count(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 200.0, 1000)
        self.repo.insertPlay("alice", "t2", 300.0, 1000)
        self.repo.commit()

        aggregates = {a["id"]: a for a in self.repo.getArtistAggregates("alice")}

        self.assertEqual(aggregates["a1"]["plays"], 3)
        self.assertEqual(aggregates["a1"]["uniqueSongCount"], 2)

    def test_plays_in_range(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 200.0, 2000)
        self.repo.commit()

        plays = self.repo.getPlaysInRange("alice")

        self.assertEqual(sorted(p["playedAt"] for p in plays), [100.0, 200.0])

    def test_play_artist_pairs_yields_one_row_per_artist(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.commit()

        pairs = self.repo.getPlayArtistPairsInRange("alice")

        self.assertEqual(sorted(p["artistName"] for p in pairs), ["Artist a1", "Artist a2"])

    def test_play_totals(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 200.0, 2000)
        self.repo.commit()

        count, total = self.repo.getPlayTotals("alice")

        self.assertEqual(count, 2)
        self.assertEqual(total, 3000)

    def test_play_totals_empty_range_returns_zero(self):
        count, total = self.repo.getPlayTotals("alice")
        self.assertEqual((count, total), (0, 0))


class TestSongsPage(RepositoryTestCase):
    """getSongsPage()/getSongsCount() replace the old N+1 getTrack()-per-row
    loop with a single batched query - these tests pin down the merged output
    shape, SQL-level ordering/tie-breaking, and LIMIT/OFFSET pagination."""

    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")

    def _track(self, trackId, albumId, *artistIds, name=None):
        track = makeTrack(trackId=trackId, name=name or f"Song {trackId}", albumId=albumId)
        track["artists"] = [
            {"id": aid, "name": f"Artist {aid}", "url": "u", "imageUrl": "", "imageId": aid}
            for aid in artistIds
        ]
        return track

    def test_returns_merged_shape_with_plays_and_track_metadata(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 200.0, 2000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice")

        self.assertEqual(len(songs), 1)
        song = songs[0]
        self.assertEqual(song["id"], "t1")
        self.assertEqual(song["name"], "Song t1")
        self.assertEqual(song["album"]["id"], "alb1")
        self.assertEqual(song["plays"], 2)
        self.assertEqual(song["totalTimeListened"], 3000)
        self.assertEqual(song["firstListenedAt"], 100.0)

    def test_multi_artist_order_preserved(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", "a2", "a3"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.commit()

        song = self.repo.getSongsPage("alice")[0]

        self.assertEqual([a["id"] for a in song["artists"]], ["a1", "a2", "a3"])

    def _seedThreeSongs(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="Bravo"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1", name="Alpha"))
        self.repo.upsertTrack(self._track("t3", "alb1", "a1", name="Charlie"))
        self.repo.insertPlay("alice", "t1", 100.0, 5000)   # t1: 1 play, 5000ms
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t2", 300.0, 1000)   # t2: 2 plays, 2000ms
        self.repo.insertPlay("alice", "t3", 400.0, 9000)   # t3: 1 play, 9000ms
        self.repo.commit()

    def test_order_by_plays_descending(self):
        self._seedThreeSongs()

        songs = self.repo.getSongsPage("alice", sortBy="plays")

        # t1 and t3 tie on plays (1 each); tie-break is totalTimeListened desc,
        # and t3's 9000ms beats t1's 5000ms.
        self.assertEqual([s["id"] for s in songs], ["t2", "t3", "t1"])

    def test_order_by_total_time_listened_descending(self):
        self._seedThreeSongs()

        songs = self.repo.getSongsPage("alice", sortBy="totalTimeListened")

        self.assertEqual([s["id"] for s in songs], ["t3", "t1", "t2"])

    def test_order_by_name_ascending(self):
        self._seedThreeSongs()

        songs = self.repo.getSongsPage("alice", sortBy="name")

        self.assertEqual([s["name"] for s in songs], ["Alpha", "Bravo", "Charlie"])

    def test_invalid_sort_by_raises_value_error(self):
        self._seedThreeSongs()

        with self.assertRaises(ValueError):
            self.repo.getSongsPage("alice", sortBy="; DROP TABLE plays;--")

    def test_limit_and_offset_paginate_default_order(self):
        self._seedThreeSongs()

        firstPage = self.repo.getSongsPage("alice", sortBy="plays", limit=2, offset=0)
        secondPage = self.repo.getSongsPage("alice", sortBy="plays", limit=2, offset=2)

        self.assertEqual([s["id"] for s in firstPage], ["t2", "t3"])
        self.assertEqual([s["id"] for s in secondPage], ["t1"])

    def test_limit_none_returns_everything(self):
        self._seedThreeSongs()

        songs = self.repo.getSongsPage("alice", limit=None)

        self.assertEqual(len(songs), 3)

    def test_offset_past_end_returns_empty(self):
        self._seedThreeSongs()

        songs = self.repo.getSongsPage("alice", limit=2, offset=10)

        self.assertEqual(songs, [])

    def test_no_plays_returns_empty(self):
        songs = self.repo.getSongsPage("alice")
        self.assertEqual(songs, [])

    def test_tied_rows_paginate_deterministically(self):
        """Two songs identical on plays/totalTimeListened/name have no natural
        tie-break in SQL GROUP BY output order - track_id is used as the final
        tie-break so paging never repeats or drops a row."""
        self.repo.upsertTrack(self._track("t2", "alb1", "a1", name="Same"))
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="Same"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        firstCall = [s["id"] for s in self.repo.getSongsPage("alice", sortBy="plays")]
        secondCall = [s["id"] for s in self.repo.getSongsPage("alice", sortBy="plays")]
        page1 = [s["id"] for s in self.repo.getSongsPage("alice", sortBy="plays", limit=1, offset=0)]
        page2 = [s["id"] for s in self.repo.getSongsPage("alice", sortBy="plays", limit=1, offset=1)]

        self.assertEqual(firstCall, secondCall)
        self.assertEqual(page1 + page2, firstCall)

    def test_date_range_filtering(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 5000.0, 2000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", startTs=0, endTs=1000)

        self.assertEqual(songs[0]["plays"], 1)
        self.assertEqual(songs[0]["totalTimeListened"], 1000)

    def test_missing_album_row_falls_back_like_get_track(self):
        """The LEFT JOIN can in principle return no matching album row, and
        _songRowToDict must degrade gracefully like getTrack()'s equivalent
        fallback - exercised directly against a synthetic row rather than via
        the database, since tracks.album_id is a NOT NULL foreign key and this
        state can't actually be produced through the public API (see
        test_db_schema.py::test_foreign_keys_enforced)."""
        row = {
            "track_id": "t1", "name": "Song", "url": "u", "image_id": "img1",
            "duration_ms": 1000, "explicit": 0, "isrc": None,
            "disc_number": 1, "track_number": 1,
            "album_id": None, "album_name": None, "album_url": None,
            "album_total_tracks": None, "album_release_date": None,
            "album_image_id": None, "album_image_url": None,
            "plays": 1, "total_time_listened": 1000, "first_listened_at": 100.0,
        }

        song = Repository._songRowToDict(row, [])

        self.assertIsNone(song["album"])
        self.assertEqual(song["imageUrl"], "")
        self.assertIsNone(song["releaseDate"])

    def test_songs_count_matches_distinct_track_count(self):
        self._seedThreeSongs()
        self.assertEqual(self.repo.getSongsCount("alice"), 3)

    def test_songs_count_respects_date_range(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 5000.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getSongsCount("alice", startTs=0, endTs=1000), 1)

    def test_songs_count_zero_when_no_plays(self):
        self.assertEqual(self.repo.getSongsCount("alice"), 0)


class TestUsersAndCookies(RepositoryTestCase):
    def test_upsert_and_lookup_by_email(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.assertEqual(self.repo.getUsernameForEmail("alice@example.com"), "alice")

    def test_unknown_email_returns_none(self):
        self.assertIsNone(self.repo.getUsernameForEmail("nobody@example.com"))

    def test_username_exists(self):
        self.assertFalse(self.repo.usernameExists("alice"))
        self.repo.upsertUser("alice", "alice@example.com")
        self.assertTrue(self.repo.usernameExists("alice"))

    def test_upsert_is_idempotent_on_conflict(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("alice", "alice@example.com")  #< must not raise
        self.assertTrue(self.repo.usernameExists("alice"))

    def test_get_email_for_username(self):
        self.assertIsNone(self.repo.getEmailForUsername("alice"))  #< doesn't exist yet
        self.repo.upsertUser("alice", None)
        self.assertIsNone(self.repo.getEmailForUsername("alice"))  #< exists, but no email on record
        self.repo.upsertUser("bob", "bob@example.com")
        self.assertEqual(self.repo.getEmailForUsername("bob"), "bob@example.com")

    def test_set_user_email_claims_an_orphaned_username(self):
        self.repo.upsertUser("alice", None)

        self.repo.setUserEmail("alice", "alice@example.com")

        self.assertEqual(self.repo.getEmailForUsername("alice"), "alice@example.com")
        self.assertEqual(self.repo.getUsernameForEmail("alice@example.com"), "alice")

    def test_cookies_default_to_none(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.assertIsNone(self.repo.getUserCookies("alice"))

    def test_cookies_roundtrip(self):
        self.repo.upsertUser("alice", "alice@example.com")
        cookies = {"sp_dc": "abc123", "sp_key": "def456"}

        self.repo.setUserCookies("alice", cookies)

        self.assertEqual(self.repo.getUserCookies("alice"), cookies)

    def test_cookies_can_be_updated(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.setUserCookies("alice", {"sp_dc": "old"})
        self.repo.setUserCookies("alice", {"sp_dc": "new"})
        self.assertEqual(self.repo.getUserCookies("alice"), {"sp_dc": "new"})

    def test_get_all_users_with_cookies_excludes_users_without_cookies(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.setUserCookies("alice", {"sp_dc": "abc"})

        result = self.repo.getAllUsersWithCookies()

        self.assertEqual(result, [("alice", "alice@example.com")])

    def test_get_all_users_with_cookies_empty_when_none_logged_in(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.assertEqual(self.repo.getAllUsersWithCookies(), [])


class TestImportProgress(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")

    def test_unknown_user_returns_none(self):
        self.assertIsNone(self.repo.readProgress("alice"))

    def test_write_then_read(self):
        self.repo.writeProgress("alice", "running", 5, 10, "Imported 5 of 10", False)

        progress = self.repo.readProgress("alice")

        self.assertEqual(progress["status"], "running")
        self.assertEqual(progress["current"], 5)
        self.assertEqual(progress["total"], 10)
        self.assertEqual(progress["percentage"], 50)
        self.assertEqual(progress["message"], "Imported 5 of 10")
        self.assertFalse(progress["error"])

    def test_percentage_zero_when_total_is_zero(self):
        self.repo.writeProgress("alice", "running", 0, 0, "Starting", False)
        self.assertEqual(self.repo.readProgress("alice")["percentage"], 0)

    def test_write_overwrites_previous_progress(self):
        self.repo.writeProgress("alice", "running", 1, 10, "step 1", False)
        self.repo.writeProgress("alice", "complete", 10, 10, "done", False)

        progress = self.repo.readProgress("alice")
        self.assertEqual(progress["status"], "complete")
        self.assertEqual(progress["current"], 10)

    def test_progress_is_scoped_per_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.writeProgress("alice", "running", 1, 10, "", False)

        self.assertIsNone(self.repo.readProgress("bob"))


if __name__ == "__main__":
    unittest.main()
