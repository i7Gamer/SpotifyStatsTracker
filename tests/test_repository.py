import unittest
import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import (
    Repository, IMAGE_KIND_TRACK, IMAGE_STATUS_OK, IMAGE_STATUS_FAILED,
    SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON,
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


class TestGetTracksByIds(RepositoryTestCase):
    """Batch equivalent of getTrack(), used by Database._paginateEntries() to
    avoid a 3-query-per-play N+1 when hydrating a page of history."""

    def test_returns_a_dict_keyed_by_track_id(self):
        self.repo.upsertTrack(makeTrack(trackId="t1", name="Song One"))
        self.repo.upsertTrack(makeTrack(trackId="t2", name="Song Two", albumId="alb2", artistId="art2"))

        result = self.repo.getTracksByIds(["t1", "t2"])

        self.assertEqual(set(result.keys()), {"t1", "t2"})
        self.assertEqual(result["t1"]["name"], "Song One")
        self.assertEqual(result["t2"]["name"], "Song Two")

    def test_result_matches_getTrack_for_the_same_id(self):
        self.repo.upsertTrack(makeTrack())

        viaBatch = self.repo.getTracksByIds(["t1"])["t1"]
        viaSingle = self.repo.getTrack("t1")

        self.assertEqual(viaBatch, viaSingle)

    def test_unknown_ids_are_simply_absent_from_the_result(self):
        self.repo.upsertTrack(makeTrack(trackId="t1"))

        result = self.repo.getTracksByIds(["t1", "missing"])

        self.assertEqual(set(result.keys()), {"t1"})

    def test_empty_id_list_returns_empty_dict_without_querying(self):
        self.assertEqual(self.repo.getTracksByIds([]), {})

    def test_tracks_on_different_albums_each_get_their_own_album(self):
        self.repo.upsertTrack(makeTrack(trackId="t1", albumId="alb1", artistId="art1"))
        self.repo.upsertTrack(makeTrack(trackId="t2", albumId="alb2", artistId="art2"))

        result = self.repo.getTracksByIds(["t1", "t2"])

        self.assertEqual(result["t1"]["album"]["id"], "alb1")
        self.assertEqual(result["t2"]["album"]["id"], "alb2")

    def test_multi_artist_order_preserved_per_track(self):
        track = makeTrack(trackId="t1")
        track["artists"] = [
            {"id": "a1", "name": "First", "url": "u", "imageUrl": "", "imageId": "a1"},
            {"id": "a2", "name": "Second", "url": "u", "imageUrl": "", "imageId": "a2"},
        ]
        self.repo.upsertTrack(track)

        result = self.repo.getTracksByIds(["t1"])

        self.assertEqual([a["id"] for a in result["t1"]["artists"]], ["a1", "a2"])


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


class TestUpsertTrackRobustness(RepositoryTestCase):
    def test_upsert_track_handles_missing_album_and_artists(self):
        """upsertTrack should construct a fallback album and default to no artists if they are None/missing, avoiding ProgrammingError."""
        track = {
            "id": "t_no_album",
            "name": "Song No Album",
            "url": "https://open.spotify.com/track/t_no_album",
            "imageId": "album_t_no_album",
            "imageUrl": "",
            "duration": 180000,
            "explicit": False,
            "isrc": "",
            "discNumber": 1,
            "trackNumber": 1,
        }
        
        self.repo.upsertTrack(track)
        self.repo.commit()
        
        db_track = self.repo.getTrack("t_no_album")
        self.assertIsNotNone(db_track)
        self.assertEqual(db_track["name"], "Song No Album")
        self.assertEqual(db_track["album"]["name"], "Song No Album")


class TestUpsertTrackGuards(RepositoryTestCase):
    """upsertTrack is last-write-wins for real metadata, but degraded records
    must never clobber good catalog data."""

    def _syntheticTrack(self, trackId="t1"):
        return {
            "id": trackId,
            "name": "Fabricated Name",
            "url": "",
            "artists": [{"id": "artist_md5", "name": "X", "url": "", "imageUrl": "", "imageId": "artist_md5"}],
            "album": {
                "id": f"album_{trackId}", "name": "Fabricated Name", "url": "",
                "imageId": f"album_{trackId}", "imageUrl": "", "totalTracks": 1, "releaseDate": 0.0,
            },
            "imageUrl": "", "imageId": f"album_{trackId}",
            "duration": 1000, "explicit": False, "isrc": "",
            "discNumber": 1, "trackNumber": 1, "releaseDate": 0.0,
            "created_reason": SYNTHETIC_FALLBACK_REASON,
        }

    def test_fallback_record_never_degrades_real_metadata(self):
        self.repo.upsertTrack(makeTrack())  #< real row with real album/artists

        self.repo.upsertTrack(self._syntheticTrack())

        fetched = self.repo.getTrack("t1")
        self.assertEqual(fetched["name"], "Song One")
        self.assertEqual(fetched["url"], "http://example.com/track/t1")
        self.assertEqual(fetched["album"]["id"], "alb1")
        self.assertEqual([a["id"] for a in fetched["artists"]], ["art1"])
        self.assertIsNone(fetched["created_reason"])

    def test_fallback_record_still_overwrites_blanked_row(self):
        """A row stored from a blanked (region-restricted) lookup has no name -
        fallback data carrying the export's own names must keep healing it."""
        blanked = makeTrack()
        blanked["name"] = ""
        self.repo.upsertTrack(blanked)

        self.repo.upsertTrack(self._syntheticTrack())

        self.assertEqual(self.repo.getTrack("t1")["name"], "Fabricated Name")

    def test_fallback_record_still_updates_existing_fallback_row(self):
        self.repo.upsertTrack(self._syntheticTrack())

        longer = self._syntheticTrack()
        longer["duration"] = 240000
        self.repo.upsertTrack(longer)

        self.assertEqual(self.repo.getTrack("t1")["duration"], 240000)

    def test_real_record_still_replaces_fallback_row(self):
        self.repo.upsertTrack(self._syntheticTrack())

        self.repo.upsertTrack(makeTrack())

        fetched = self.repo.getTrack("t1")
        self.assertEqual(fetched["name"], "Song One")
        self.assertIsNone(fetched["created_reason"])

    def test_empty_artists_list_preserves_existing_artist_links(self):
        self.repo.upsertTrack(makeTrack())

        noArtists = makeTrack()
        noArtists["artists"] = []
        self.repo.upsertTrack(noArtists)

        fetched = self.repo.getTrack("t1")
        self.assertEqual([a["id"] for a in fetched["artists"]], ["art1"])


class TestAlbumMetadataGuards(RepositoryTestCase):
    """A partial backfill response must never regress album fields another
    source already filled."""

    def test_blank_values_do_not_regress_existing_metadata(self):
        self.repo.upsertTrack(makeTrack())  #< alb1: releaseDate 12345.0, totalTracks 10, "Album One"

        self.repo.updateAlbumMetadata("alb1", 0.0, 0, name=None)

        album = self.repo.getTrack("t1")["album"]
        self.assertEqual(album["releaseDate"], 12345.0)
        self.assertEqual(album["totalTracks"], 10)
        self.assertEqual(album["name"], "Album One")

    def test_real_values_update_metadata(self):
        self.repo.upsertTrack(makeTrack())

        self.repo.updateAlbumMetadata("alb1", 1600000000.0, 12, name="New Name")

        album = self.repo.getTrack("t1")["album"]
        self.assertEqual(album["releaseDate"], 1600000000.0)
        self.assertEqual(album["totalTracks"], 12)
        self.assertEqual(album["name"], "New Name")

    def test_partial_response_updates_only_provided_fields(self):
        self.repo.upsertTrack(makeTrack())

        self.repo.updateAlbumMetadata("alb1", 1600000000.0, 0, name=None)

        album = self.repo.getTrack("t1")["album"]
        self.assertEqual(album["releaseDate"], 1600000000.0)
        self.assertEqual(album["totalTracks"], 10)
        self.assertEqual(album["name"], "Album One")


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

    def test_get_plays_with_source_in_range_returns_created_reason_and_respects_window(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000, created_reason="listener_play (user: alice)")
        self.repo.insertPlay("alice", "t1", 1003.0, 5000, created_reason="web_api_backfill_play (user: alice)")
        self.repo.insertPlay("alice", "t2", 1500.0, 5000)  #< legacy row, no created_reason
        self.repo.insertPlay("alice", "t1", 3000.0, 5000, created_reason="history_import (user: alice)")
        self.repo.commit()

        plays = self.repo.getPlaysWithSourceInRange("alice", 900.0, 2000.0)

        self.assertEqual(len(plays), 3)
        byTime = {p["playedAt"]: p for p in plays}
        self.assertEqual(byTime[1000.0]["id"], "t1")
        self.assertEqual(byTime[1000.0]["createdReason"], "listener_play (user: alice)")
        self.assertEqual(byTime[1003.0]["createdReason"], "web_api_backfill_play (user: alice)")
        self.assertIsNone(byTime[1500.0]["createdReason"])
        self.assertEqual(byTime[1500.0]["timePlayed"], 5000)

    def test_delete_zero_duration_plays_removes_only_zero_and_negative(self):
        conn = self.repo._conn()
        with conn:
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                "INSERT INTO plays (username, track_id, played_at, time_played) VALUES (?, ?, ?, ?)",
                ("alice", "t1", 1000.0, 0)
            )
            conn.execute(
                "INSERT INTO plays (username, track_id, played_at, time_played) VALUES (?, ?, ?, ?)",
                ("alice", "t1", 2000.0, -5)
            )
            conn.execute(
                "INSERT INTO plays (username, track_id, played_at, time_played) VALUES (?, ?, ?, ?)",
                ("alice", "t1", 3000.0, 5000)
            )
        self.repo.commit()

        removedCount = self.repo.deleteZeroDurationPlays()
        self.repo.commit()

        self.assertEqual(removedCount, 2)
        self.assertEqual(self.repo.getPlaysCount("alice"), 1)
        self.assertEqual(self.repo.getPlaysNewestFirst("alice")[0]["timePlayed"], 5000)

    def test_delete_zero_duration_plays_spans_every_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        conn = self.repo._conn()
        with conn:
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                "INSERT INTO plays (username, track_id, played_at, time_played) VALUES (?, ?, ?, ?)",
                ("alice", "t1", 1000.0, 0)
            )
            conn.execute(
                "INSERT INTO plays (username, track_id, played_at, time_played) VALUES (?, ?, ?, ?)",
                ("bob", "t1", 1000.0, 0)
            )
        self.repo.commit()

        removedCount = self.repo.deleteZeroDurationPlays()

        self.assertEqual(removedCount, 2)

    def test_delete_zero_duration_plays_is_noop_when_none_exist(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.commit()

        self.assertEqual(self.repo.deleteZeroDurationPlays(), 0)
        self.assertEqual(self.repo.getPlaysCount("alice"), 1)

    def test_delete_play_removes_the_exact_row(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t2", 1000.0, 5000)
        self.repo.commit()

        deleted = self.repo.deletePlay("alice", "t1", 1000.0)
        self.repo.commit()

        self.assertTrue(deleted)
        self.assertEqual(self.repo.getPlaysCount("alice"), 1)
        self.assertEqual(self.repo.getPlaysNewestFirst("alice")[0]["id"], "t2")

    def test_delete_play_returns_false_when_no_match(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.commit()

        deleted = self.repo.deletePlay("alice", "t1", 9999.0)

        self.assertFalse(deleted)
        self.assertEqual(self.repo.getPlaysCount("alice"), 1)

    def test_delete_play_is_scoped_per_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.insertPlay("bob", "t1", 1000.0, 5000)
        self.repo.commit()

        deleted = self.repo.deletePlay("alice", "t1", 1000.0)
        self.repo.commit()

        self.assertTrue(deleted)
        self.assertEqual(self.repo.getPlaysCount("alice"), 0)
        self.assertEqual(self.repo.getPlaysCount("bob"), 1)

    def _playCreatedColumns(self, username, trackId, playedAt):
        conn = self.repo._conn()
        row = conn.execute(
            "SELECT created_at, created_reason FROM plays WHERE username=? AND track_id=? AND played_at=?",
            (username, trackId, playedAt),
        ).fetchone()
        return row["created_at"], row["created_reason"]

    def test_insert_play_stores_created_reason_and_created_at(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000, created_reason="listener_play (user: alice)")
        self.repo.commit()

        createdAt, createdReason = self._playCreatedColumns("alice", "t1", 1000.0)
        self.assertEqual(createdReason, "listener_play (user: alice)")
        self.assertIsNotNone(createdAt)

    def test_insert_play_without_created_reason_leaves_it_none(self):
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.commit()

        createdAt, createdReason = self._playCreatedColumns("alice", "t1", 1000.0)
        self.assertIsNone(createdReason)
        self.assertIsNone(createdAt)

    def test_updating_an_existing_play_does_not_change_created_reason(self):
        """Mirrors upsertTrack()'s semantics: created_reason/created_at are
        set once, on first insert, and never overwritten by a later update
        (e.g. a duplicate play arriving with a corrected time_played)."""
        self.repo.insertPlay("alice", "t1", 1000.0, 5000, created_reason="listener_play (user: alice)")
        self.repo.commit()

        self.repo.insertPlay("alice", "t1", 1000.0, 8000, created_reason="history_import (user: alice)")
        self.repo.commit()

        createdAt, createdReason = self._playCreatedColumns("alice", "t1", 1000.0)
        self.assertEqual(createdReason, "listener_play (user: alice)")
        self.assertEqual(self.repo.getPlaysNewestFirst("alice")[0]["timePlayed"], 8000)


def makeSyntheticTrack(trackId="synth1", name="Ghost Song", artist="Ghost Artist"):
    """Mirrors Importer._createSyntheticTrack's output shape: empty urls and the
    synthetic created_reason marker, no created_at."""
    return {
        "id": trackId,
        "name": name,
        "url": "",
        "artists": [
            {"id": f"artist_{trackId}", "name": artist, "url": "", "imageUrl": "", "imageId": f"artist_{trackId}"},
        ],
        "album": {
            "id": f"album_{trackId}", "name": name, "url": "", "imageId": f"album_{trackId}",
            "imageUrl": "", "totalTracks": 1, "releaseDate": 0.0,
        },
        "imageUrl": "",
        "imageId": f"album_{trackId}",
        "duration": 10354,
        "explicit": False,
        "isrc": "",
        "discNumber": 1,
        "trackNumber": 1,
        "releaseDate": 0.0,
        "created_reason": SYNTHETIC_FALLBACK_REASON,
    }


class TestSyntheticTrackLifecycle(RepositoryTestCase):
    def _trackCreatedColumns(self, trackId):
        row = self.repo._conn().execute(
            "SELECT created_at, created_reason FROM tracks WHERE id=?", (trackId,)
        ).fetchone()
        return row["created_at"], row["created_reason"]

    def test_synthetic_insert_stamps_created_at(self):
        """A created_reason without a created_at breaks the 'reason implies
        timestamp' invariant insertPlay() documents - the repo must stamp it."""
        self.repo.upsertTrack(makeSyntheticTrack())
        self.repo.commit()

        createdAt, createdReason = self._trackCreatedColumns("synth1")
        self.assertEqual(createdReason, SYNTHETIC_FALLBACK_REASON)
        self.assertIsNotNone(createdAt)

    def test_synthetic_reupsert_keeps_marker(self):
        """Re-importing history round-trips the synthetic dict (via getAllTracks)
        - the marker must survive, matching the created-on-INSERT-only rule."""
        self.repo.upsertTrack(makeSyntheticTrack())
        self.repo.upsertTrack(makeSyntheticTrack(), created_reason="history_import (user: alice)")
        self.repo.commit()

        _, createdReason = self._trackCreatedColumns("synth1")
        self.assertEqual(createdReason, SYNTHETIC_FALLBACK_REASON)

    def test_real_metadata_promotes_synthetic_row(self):
        """A track that turns out to exist on Spotify (e.g. the listener fetches
        the same id later) must lose the synthetic marker so the UI stops badging
        it as Deleted/Unavailable."""
        self.repo.upsertTrack(makeSyntheticTrack(trackId="t1"))
        self.repo.upsertTrack(makeTrack(trackId="t1"), created_reason="listener_fetch (user: alice)")
        self.repo.commit()

        createdAt, createdReason = self._trackCreatedColumns("t1")
        self.assertEqual(createdReason, "listener_fetch (user: alice)")
        self.assertIsNotNone(createdAt)
        self.assertEqual(self.repo.getTrack("t1")["url"], "http://example.com/track/t1")

    def test_real_metadata_without_reason_clears_synthetic_marker(self):
        self.repo.upsertTrack(makeSyntheticTrack(trackId="t1"))
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.commit()

        _, createdReason = self._trackCreatedColumns("t1")
        self.assertIsNone(createdReason)

    def test_real_metadata_promotes_restricted_row(self):
        """Same promotion as synthetic rows: a restricted-fallback row overwritten
        by real metadata loses its May-be-unavailable marker."""
        restricted = makeTrack(trackId="t1")
        restricted["created_reason"] = RESTRICTED_FALLBACK_REASON
        self.repo.upsertTrack(restricted)
        self.repo.upsertTrack(makeTrack(trackId="t1"), created_reason="listener_fetch (user: alice)")
        self.repo.commit()

        createdAt, createdReason = self._trackCreatedColumns("t1")
        self.assertEqual(createdReason, "listener_fetch (user: alice)")
        self.assertIsNotNone(createdAt)

    def test_restricted_reupsert_keeps_marker(self):
        """Re-imports round-trip the restricted marker through the catalog cache -
        it must survive, like the synthetic marker does."""
        restricted = makeTrack(trackId="t1")
        restricted["created_reason"] = RESTRICTED_FALLBACK_REASON
        self.repo.upsertTrack(restricted)
        self.repo.upsertTrack(dict(restricted), created_reason="history_import (user: alice)")
        self.repo.commit()

        _, createdReason = self._trackCreatedColumns("t1")
        self.assertEqual(createdReason, RESTRICTED_FALLBACK_REASON)

    def test_conflict_keeps_non_synthetic_created_reason(self):
        """The promotion exception applies only to synthetic rows - a real row's
        provenance is still never overwritten on conflict."""
        self.repo.upsertTrack(makeTrack(trackId="t1"), created_reason="history_import (user: alice)")
        self.repo.upsertTrack(makeTrack(trackId="t1"), created_reason="listener_fetch (user: alice)")
        self.repo.commit()

        _, createdReason = self._trackCreatedColumns("t1")
        self.assertEqual(createdReason, "history_import (user: alice)")

    def test_song_rows_include_created_reason(self):
        """getSongsPage feeds the top-songs/dashboard track cards - it must
        carry created_reason so the Deleted/Unavailable badge can render there."""
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertTrack(makeSyntheticTrack(trackId="synth1"))
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.insertPlay("alice", "synth1", 1000.0, 5000)
        self.repo.insertPlay("alice", "t1", 2000.0, 5000)
        self.repo.commit()

        songs = {song["id"]: song for song in self.repo.getSongsPage("alice")}
        self.assertEqual(songs["synth1"]["created_reason"], SYNTHETIC_FALLBACK_REASON)
        self.assertIsNone(songs["t1"]["created_reason"])


class TestAvailabilityReason(RepositoryTestCase):
    def test_roundtrip_and_clear_on_later_upsert(self):
        """availability_reason reflects the latest lookup (current state, not
        provenance): stored on upsert, cleared when a later upsert has none."""
        track = makeTrack(trackId="t1")
        track["availability_reason"] = "COUNTRY_RESTRICTED"
        self.repo.upsertTrack(track)
        self.repo.commit()
        self.assertEqual(self.repo.getTrack("t1")["availability_reason"], "COUNTRY_RESTRICTED")

        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.commit()
        self.assertIsNone(self.repo.getTrack("t1")["availability_reason"])

    def test_song_rows_include_availability_reason(self):
        self.repo.upsertUser("alice", "alice@example.com")
        track = makeTrack(trackId="t1")
        track["availability_reason"] = "COUNTRY_RESTRICTED"
        self.repo.upsertTrack(track)
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice")
        self.assertEqual(songs[0]["availability_reason"], "COUNTRY_RESTRICTED")

    def test_add_availability_columns_if_missing_on_legacy_db(self):
        import sqlite3

        legacyPath = Path(self._tmpdir.name) / "legacy.db"
        conn = sqlite3.connect(legacyPath)
        conn.execute("CREATE TABLE tracks (id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, album_id TEXT NOT NULL)")
        conn.execute("CREATE TABLE albums (id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL)")
        conn.commit()
        conn.close()

        legacyRepo = Repository(legacyPath)
        try:
            legacyRepo.addAvailabilityColumnsIfMissing()
            legacyRepo.addAvailabilityColumnsIfMissing()  #< idempotent
            trackCols = {r["name"] for r in legacyRepo._conn().execute("PRAGMA table_info(tracks)").fetchall()}
            albumCols = {r["name"] for r in legacyRepo._conn().execute("PRAGMA table_info(albums)").fetchall()}
            self.assertIn("availability_reason", trackCols)
            self.assertIn("backfill_attempted_at", albumCols)
        finally:
            legacyRepo.connectionManager.close()


class TestHasPlayNearTime(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertTrack(makeTrack(trackId="t1"))
        self.repo.upsertTrack(makeTrack(trackId="t2"))
        self.repo.insertPlay("alice", "t1", 1000.0, 5000)
        self.repo.commit()

    def test_true_within_tolerance(self):
        self.assertTrue(self.repo.hasPlayNearTime("alice", "t1", 1050.0, 100))

    def test_true_at_exact_boundary(self):
        self.assertTrue(self.repo.hasPlayNearTime("alice", "t1", 1100.0, 100))
        self.assertTrue(self.repo.hasPlayNearTime("alice", "t1", 900.0, 100))

    def test_false_just_outside_tolerance(self):
        self.assertFalse(self.repo.hasPlayNearTime("alice", "t1", 1101.0, 100))
        self.assertFalse(self.repo.hasPlayNearTime("alice", "t1", 899.0, 100))

    def test_false_for_different_track_id(self):
        self.assertFalse(self.repo.hasPlayNearTime("alice", "t2", 1050.0, 100))

    def test_false_for_different_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.assertFalse(self.repo.hasPlayNearTime("bob", "t1", 1050.0, 100))


def makeSearchableTrack(trackId, name, artistName, albumName):
    """Unlike makeTrack() (which hardcodes "Artist One"/"Album One" regardless
    of id - fine for id-uniqueness tests, wrong for text-search tests), this
    lets each fixture track carry genuinely distinct searchable text."""
    return {
        "id": trackId,
        "name": name,
        "url": f"http://example.com/track/{trackId}",
        "artists": [
            {"id": f"{trackId}-artist", "name": artistName, "url": "http://example.com/artist",
             "imageUrl": "", "imageId": f"{trackId}-artist"},
        ],
        "album": {
            "id": f"{trackId}-album", "name": albumName, "url": "http://example.com/album",
            "imageId": f"{trackId}-album", "imageUrl": "", "totalTracks": 1, "releaseDate": 12345.0,
        },
        "imageUrl": "", "imageId": f"{trackId}-album", "duration": 200000, "explicit": False,
        "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 12345.0,
    }


class TestSearchPlays(RepositoryTestCase):
    """searchPlays()/searchPlaysCount() match a play's track name, artist(s),
    album, or source playlist - pushed down into SQL (with LIMIT/OFFSET)
    instead of requiring every play to be fetched and filtered in Python."""

    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.upsertTrack(makeSearchableTrack("t1", "Bohemian Rhapsody", "Queen", "A Night at the Opera"))
        self.repo.upsertTrack(makeSearchableTrack("t2", "Another One Bites the Dust", "Queen", "The Game"))
        self.repo.upsertTrack(makeSearchableTrack("t3", "Unrelated Song", "Random Artist", "Random Album"))

    def test_matches_track_name(self):
        self.repo.insertPlay("alice", "t1", 100.0, 5000)
        self.repo.insertPlay("alice", "t3", 200.0, 5000)

        results = self.repo.searchPlays("alice", "bohemian")

        self.assertEqual([r["id"] for r in results], ["t1"])

    def test_match_is_case_insensitive(self):
        self.repo.insertPlay("alice", "t1", 100.0, 5000)

        results = self.repo.searchPlays("alice", "BOHEMIAN")

        self.assertEqual([r["id"] for r in results], ["t1"])

    def test_matches_artist_name(self):
        self.repo.insertPlay("alice", "t1", 100.0, 5000)
        self.repo.insertPlay("alice", "t3", 200.0, 5000)

        results = self.repo.searchPlays("alice", "Queen")

        self.assertEqual([r["id"] for r in results], ["t1"])

    def test_matches_album_name(self):
        self.repo.insertPlay("alice", "t1", 100.0, 5000)
        self.repo.insertPlay("alice", "t3", 200.0, 5000)

        results = self.repo.searchPlays("alice", "Night at the Opera")

        self.assertEqual([r["id"] for r in results], ["t1"])

    def test_matches_playlist_name(self):
        self.repo.upsertPlaylistName("pl1", "playlist", "Road Trip Mix")
        self.repo.insertPlay("alice", "t1", 100.0, 5000, playedFrom="playlist:pl1")
        self.repo.insertPlay("alice", "t3", 200.0, 5000)

        results = self.repo.searchPlays("alice", "road trip")

        self.assertEqual([r["id"] for r in results], ["t1"])

    def test_no_match_returns_empty(self):
        self.repo.insertPlay("alice", "t1", 100.0, 5000)

        self.assertEqual(self.repo.searchPlays("alice", "nonexistent"), [])
        self.assertEqual(self.repo.searchPlaysCount("alice", "nonexistent"), 0)

    def test_percent_and_underscore_are_matched_literally_not_as_wildcards(self):
        self.repo.upsertTrack(makeSearchableTrack("t4", "100% Pure Love", "Random Artist", "Random Album"))
        self.repo.insertPlay("alice", "t1", 100.0, 5000)
        self.repo.insertPlay("alice", "t4", 200.0, 5000)

        results = self.repo.searchPlays("alice", "100%")

        self.assertEqual([r["id"] for r in results], ["t4"])

    def test_results_are_scoped_per_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.insertPlay("alice", "t1", 100.0, 5000)
        self.repo.insertPlay("bob", "t1", 100.0, 5000)

        self.assertEqual(self.repo.searchPlaysCount("alice", "bohemian"), 1)
        self.assertEqual(len(self.repo.searchPlays("bob", "bohemian")), 1)

    def test_ordered_newest_first(self):
        """"the" matches t1 via its album ("A Night at the Opera") and t2 via
        its own name ("...Bites the Dust")."""
        self.repo.insertPlay("alice", "t1", 100.0, 5000)
        self.repo.insertPlay("alice", "t2", 300.0, 5000)
        self.repo.insertPlay("alice", "t1", 200.0, 5000)

        results = self.repo.searchPlays("alice", "the")

        self.assertEqual([r["playedAt"] for r in results], [300.0, 200.0, 100.0])

    def test_limit_and_offset_paginate_matches(self):
        for i in range(5):
            self.repo.insertPlay("alice", "t1", float(i), 5000)

        page = self.repo.searchPlays("alice", "bohemian", limit=2, offset=1)

        self.assertEqual([r["playedAt"] for r in page], [3.0, 2.0])

    def test_count_matches_full_result_length(self):
        for i in range(5):
            self.repo.insertPlay("alice", "t1", float(i), 5000)
        self.repo.insertPlay("alice", "t3", 100.0, 5000)

        self.assertEqual(self.repo.searchPlaysCount("alice", "bohemian"), 5)
        self.assertEqual(len(self.repo.searchPlays("alice", "bohemian")), 5)


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

    def test_artist_aggregates_filtered_by_artist_id(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.commit()

        aggregates = self.repo.getArtistAggregates("alice", artistId="a1")

        self.assertEqual([a["id"] for a in aggregates], ["a1"])

    def test_artist_aggregates_filtered_by_unknown_artist_id_returns_empty(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getArtistAggregates("alice", artistId="missing"), [])

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

    def test_artist_aggregates_sorted_by_plays_descending_by_default(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t2", 300.0, 1000)
        self.repo.commit()

        aggregates = self.repo.getArtistAggregates("alice")

        self.assertEqual([a["id"] for a in aggregates], ["a2", "a1"])

    def test_artist_aggregates_sorted_by_name_ascending(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        aggregates = self.repo.getArtistAggregates("alice", sortBy="name")

        self.assertEqual([a["id"] for a in aggregates], ["a1", "a2"])  #< "Artist a1" < "Artist a2"

    def test_artist_aggregates_name_sort_is_case_insensitive(self):
        """SQLite's default BINARY collation sorts every uppercase letter
        before every lowercase one, so "Banana" would otherwise land before
        "apple"/"cherry" instead of interleaving alphabetically by letter."""
        def trackWithArtist(trackId, artistId, artistName):
            track = makeTrack(trackId=trackId, albumId="alb1")
            track["artists"] = [{"id": artistId, "name": artistName, "url": "u", "imageUrl": "", "imageId": artistId}]
            return track

        self.repo.upsertTrack(trackWithArtist("t1", "a1", "apple"))
        self.repo.upsertTrack(trackWithArtist("t2", "a2", "Banana"))
        self.repo.upsertTrack(trackWithArtist("t3", "a3", "cherry"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t3", 300.0, 1000)
        self.repo.commit()

        aggregates = self.repo.getArtistAggregates("alice", sortBy="name")

        self.assertEqual([a["id"] for a in aggregates], ["a1", "a2", "a3"])  #< apple, Banana, cherry

    def test_artist_aggregates_rejects_unknown_sortby(self):
        with self.assertRaises(ValueError):
            self.repo.getArtistAggregates("alice", sortBy="not_a_real_column")

    def test_artist_aggregates_limit_and_offset_paginate(self):
        for i in range(5):
            trackId, artistId = f"t{i}", f"a{i}"
            self.repo.upsertTrack(self._track(trackId, "alb1", artistId))
            self.repo.insertPlay("alice", trackId, float(i), (i + 1) * 1000)  #< distinct play counts for a stable sort
        self.repo.commit()

        page = self.repo.getArtistAggregates("alice", limit=2, offset=1)

        self.assertEqual(len(page), 2)

    def test_artist_aggregates_filtered_by_search_query(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        aggregates = self.repo.getArtistAggregates("alice", searchQuery="a1")

        self.assertEqual([a["id"] for a in aggregates], ["a1"])

    def test_get_artists_count(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getArtistsCount("alice"), 2)

    def test_get_artists_count_filtered_by_search_query(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getArtistsCount("alice", searchQuery="a1"), 1)

    def test_get_artist_totals_sums_across_every_artist(self):
        """A multi-artist track's plays are counted once per artist on it - the
        totals are a sum of each artist's own aggregate, not the track-level
        total getPlayTotals() would give."""
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", "a2"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 2000)
        self.repo.commit()

        totalPlays, totalUnique, totalTime = self.repo.getArtistTotals("alice")

        # a1: 2 plays (t1, t2), 2 unique songs; a2: 1 play (t1), 1 unique song.
        self.assertEqual(totalPlays, 3)
        self.assertEqual(totalUnique, 3)
        self.assertEqual(totalTime, 4000)

    def test_get_artist_totals_empty_range_returns_zeros(self):
        self.assertEqual(self.repo.getArtistTotals("alice"), (0, 0, 0))

    def test_ranged_play_queries_use_the_time_index(self):
        """The old static '(? IS NULL OR played_at >= ?)' range clause is
        non-sargable - SQLite can't use played_at as an index range bound
        through the OR, so every ranged query walked the user's whole play
        history. The clause must emit only the bounds that exist, letting
        the (username, played_at) index prune the scan."""
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.commit()
        conn = self.repo._conn()

        params = ["alice"]
        clause = self.repo._dateRangeClause(params, 50.0, 150.0)
        plan = "\n".join(row[3] for row in conn.execute(
            f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM plays WHERE username = ?{clause}", params))

        self.assertIn("idx_plays_user_time", plan)
        self.assertIn("played_at", plan)   #< the index is used as a RANGE scan, not just the username prefix

    def test_date_range_clause_emits_no_conditions_for_all_time(self):
        params = ["alice"]
        clause = self.repo._dateRangeClause(params, None, None)
        self.assertEqual(clause, "")
        self.assertEqual(params, ["alice"])

    def test_bucketed_play_totals_sums_within_a_bucket(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 200.0, 2000)   #< same 15-minute bucket as 100.0
        self.repo.commit()

        rows = self.repo.getBucketedPlayTotals("alice")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bucketStartTs"], 0)
        self.assertEqual(rows[0]["plays"], 2)
        self.assertEqual(rows[0]["totalTimeListened"], 3000)

    def test_bucketed_play_totals_filtered_by_track_id(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 2000)
        self.repo.commit()

        rows = self.repo.getBucketedPlayTotals("alice", trackId="t1")

        self.assertEqual([(r["plays"], r["totalTimeListened"]) for r in rows], [(1, 1000)])

    def test_bucketed_play_totals_filtered_by_artist_id(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 2000)
        self.repo.commit()

        rows = self.repo.getBucketedPlayTotals("alice", artistId="a1")

        self.assertEqual([(r["plays"], r["totalTimeListened"]) for r in rows], [(1, 1000)])

    def test_bucketed_play_totals_filtered_by_album_id(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb2", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 2000)
        self.repo.commit()

        rows = self.repo.getBucketedPlayTotals("alice", albumId="alb1")

        self.assertEqual([(r["plays"], r["totalTimeListened"]) for r in rows], [(1, 1000)])

    def test_bucketed_artist_play_counts_yield_one_count_per_artist(self):
        """A play whose track has N artists counts once per artist, matching
        the per-(play, artist) increment the old Python loop did."""
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.commit()

        rows = self.repo.getBucketedArtistPlayCounts("alice")

        self.assertEqual(sorted((r["artistName"], r["plays"]) for r in rows),
                         [("Artist a1", 1), ("Artist a2", 1)])

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

    def test_play_at_boundary_belongs_to_exactly_one_adjacent_range(self):
        """The date-range clause implements the half-open interval
        [startTs, endTs) documented by app.py's _getDateRange - a play
        landing exactly on a shared boundary between two adjacent ranges
        must be counted in the later range only, not both."""
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 1000.0, 1000)
        self.repo.commit()

        earlierRange = self.repo.getPlayTotals("alice", startTs=0, endTs=1000.0)
        laterRange = self.repo.getPlayTotals("alice", startTs=1000.0, endTs=2000.0)

        self.assertEqual(earlierRange, (0, 0))
        self.assertEqual(laterRange, (1, 1000))

    def test_play_time_range_returns_first_and_last_played_at(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 500.0, 1000)
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 900.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getPlayTimeRange("alice"), (100.0, 900.0))

    def test_play_time_range_is_scoped_to_the_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("bob", "t1", 999.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getPlayTimeRange("alice"), (100.0, 100.0))

    def test_play_time_range_with_no_plays_is_none(self):
        self.assertIsNone(self.repo.getPlayTimeRange("alice"))


class TestSongsPage(RepositoryTestCase):
    """getSongsPage()/getSongsCount() replace the old N+1 getTrack()-per-row
    loop with a single batched query - these tests pin down the merged output
    shape, SQL-level ordering/tie-breaking, and LIMIT/OFFSET pagination."""

    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")

    def _track(self, trackId, albumId, *artistIds, name=None, albumName=None):
        track = makeTrack(trackId=trackId, name=name or f"Song {trackId}", albumId=albumId)
        track["artists"] = [
            {"id": aid, "name": f"Artist {aid}", "url": "u", "imageUrl": "", "imageId": aid}
            for aid in artistIds
        ]
        if albumName is not None:
            track["album"]["name"] = albumName
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

    def test_order_by_name_is_case_insensitive(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="apple"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1", name="Banana"))
        self.repo.upsertTrack(self._track("t3", "alb1", "a1", name="cherry"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t3", 300.0, 1000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", sortBy="name")

        self.assertEqual([s["name"] for s in songs], ["apple", "Banana", "cherry"])

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

    def test_search_query_matches_track_name(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="Bohemian Rhapsody"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2", name="Unrelated"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", searchQuery="bohemian")

        self.assertEqual([s["id"] for s in songs], ["t1"])

    def test_search_query_matches_artist_name(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", searchQuery="Artist a1")

        self.assertEqual([s["id"] for s in songs], ["t1"])

    def test_search_query_matches_album_name(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", albumName="A Night at the Opera"))
        self.repo.upsertTrack(self._track("t2", "alb2", "a1", albumName="Unrelated Album"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", searchQuery="night at the opera")

        self.assertEqual([s["id"] for s in songs], ["t1"])

    def test_search_query_paginates_with_getSongsCount(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="Bohemian Rhapsody"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a1", name="Bohemian Remix"))
        self.repo.upsertTrack(self._track("t3", "alb1", "a1", name="Unrelated"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t3", 300.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getSongsCount("alice", searchQuery="bohemian"), 2)
        page = self.repo.getSongsPage("alice", searchQuery="bohemian", limit=1, offset=0)
        self.assertEqual(len(page), 1)

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
            "disc_number": 1, "track_number": 1, "created_reason": None,
            "availability_reason": None,
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

    def test_filtered_by_track_id_returns_only_that_track(self):
        self._seedThreeSongs()

        songs = self.repo.getSongsPage("alice", trackId="t2")

        self.assertEqual([s["id"] for s in songs], ["t2"])

    def test_filtered_by_track_id_unknown_returns_empty(self):
        self._seedThreeSongs()

        self.assertEqual(self.repo.getSongsPage("alice", trackId="missing"), [])

    def test_filtered_by_artist_id_returns_only_that_artists_songs(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="Song One"))
        self.repo.upsertTrack(self._track("t2", "alb1", "a2", name="Song Two"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", artistId="a1")

        self.assertEqual([s["id"] for s in songs], ["t1"])

    def test_filtered_by_artist_id_does_not_duplicate_multi_artist_tracks(self):
        """A track credited to multiple artists must still yield exactly one
        row (not one per matching artist) when filtered by one of them."""
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 200.0, 1000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", artistId="a1")

        self.assertEqual(len(songs), 1)
        self.assertEqual(songs[0]["plays"], 2)

    def test_filtered_by_album_id_returns_only_that_albums_songs(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "a1", name="Song One"))
        self.repo.upsertTrack(self._track("t2", "alb2", "a1", name="Song Two"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        songs = self.repo.getSongsPage("alice", albumId="alb1")

        self.assertEqual([s["id"] for s in songs], ["t1"])


class TestAlbumsPage(RepositoryTestCase):
    """getAlbumsPage()/getAlbumsCount() aggregate plays by album, mirroring
    getSongsPage()'s batched sort/page/date-range pattern."""

    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")

    def _track(self, trackId, albumId, albumName, *artistIds):
        track = makeTrack(trackId=trackId, albumId=albumId)
        track["album"]["name"] = albumName
        track["artists"] = [
            {"id": aid, "name": f"Artist {aid}", "url": "u", "imageUrl": "", "imageId": aid}
            for aid in artistIds
        ]
        return track

    def test_returns_merged_shape_with_plays_and_album_metadata(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "Album One", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 200.0, 2000)
        self.repo.commit()

        albums = self.repo.getAlbumsPage("alice")

        self.assertEqual(len(albums), 1)
        album = albums[0]
        self.assertEqual(album["id"], "alb1")
        self.assertEqual(album["name"], "Album One")
        self.assertEqual(album["plays"], 2)
        self.assertEqual(album["totalTimeListened"], 3000)
        self.assertEqual(album["firstListenedAt"], 100.0)
        self.assertEqual(album["uniqueSongCount"], 1)
        self.assertEqual([a["id"] for a in album["artists"]], ["a1"])

    def test_plays_across_multiple_tracks_on_same_album_are_combined(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "Album One", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb1", "Album One", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 2000)
        self.repo.commit()

        album = self.repo.getAlbumsPage("alice")[0]

        self.assertEqual(album["plays"], 2)
        self.assertEqual(album["totalTimeListened"], 3000)
        self.assertEqual(album["uniqueSongCount"], 2)

    def test_artists_across_the_album_are_deduplicated(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "Album One", "a1", "a2"))
        self.repo.upsertTrack(self._track("t2", "alb1", "Album One", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        album = self.repo.getAlbumsPage("alice")[0]

        self.assertEqual(sorted(a["id"] for a in album["artists"]), ["a1", "a2"])

    def _seedThreeAlbums(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "Bravo", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb2", "Alpha", "a1"))
        self.repo.upsertTrack(self._track("t3", "alb3", "Charlie", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 5000)   # alb1: 1 play, 5000ms
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t2", 300.0, 1000)   # alb2: 2 plays, 2000ms
        self.repo.insertPlay("alice", "t3", 400.0, 9000)   # alb3: 1 play, 9000ms
        self.repo.commit()

    def test_order_by_plays_descending(self):
        self._seedThreeAlbums()

        albums = self.repo.getAlbumsPage("alice", sortBy="plays")

        # alb1 and alb3 tie on plays (1 each); tie-break is totalTimeListened desc.
        self.assertEqual([a["id"] for a in albums], ["alb2", "alb3", "alb1"])

    def test_order_by_total_time_listened_descending(self):
        self._seedThreeAlbums()

        albums = self.repo.getAlbumsPage("alice", sortBy="totalTimeListened")

        self.assertEqual([a["id"] for a in albums], ["alb3", "alb1", "alb2"])

    def test_order_by_name_ascending(self):
        self._seedThreeAlbums()

        albums = self.repo.getAlbumsPage("alice", sortBy="name")

        self.assertEqual([a["name"] for a in albums], ["Alpha", "Bravo", "Charlie"])

    def test_order_by_name_is_case_insensitive(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "apple", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb2", "Banana", "a1"))
        self.repo.upsertTrack(self._track("t3", "alb3", "cherry", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t3", 300.0, 1000)
        self.repo.commit()

        albums = self.repo.getAlbumsPage("alice", sortBy="name")

        self.assertEqual([a["name"] for a in albums], ["apple", "Banana", "cherry"])

    def test_invalid_sort_by_raises_value_error(self):
        self._seedThreeAlbums()

        with self.assertRaises(ValueError):
            self.repo.getAlbumsPage("alice", sortBy="; DROP TABLE plays;--")

    def test_limit_and_offset_paginate_default_order(self):
        self._seedThreeAlbums()

        firstPage = self.repo.getAlbumsPage("alice", sortBy="plays", limit=2, offset=0)
        secondPage = self.repo.getAlbumsPage("alice", sortBy="plays", limit=2, offset=2)

        self.assertEqual([a["id"] for a in firstPage], ["alb2", "alb3"])
        self.assertEqual([a["id"] for a in secondPage], ["alb1"])

    def test_limit_none_returns_everything(self):
        self._seedThreeAlbums()

        albums = self.repo.getAlbumsPage("alice", limit=None)

        self.assertEqual(len(albums), 3)

    def test_offset_past_end_returns_empty(self):
        self._seedThreeAlbums()

        albums = self.repo.getAlbumsPage("alice", limit=2, offset=10)

        self.assertEqual(albums, [])

    def test_no_plays_returns_empty(self):
        albums = self.repo.getAlbumsPage("alice")
        self.assertEqual(albums, [])

    def test_date_range_filtering(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "Album One", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 5000.0, 2000)
        self.repo.commit()

        albums = self.repo.getAlbumsPage("alice", startTs=0, endTs=1000)

        self.assertEqual(albums[0]["plays"], 1)
        self.assertEqual(albums[0]["totalTimeListened"], 1000)

    def test_albums_count_matches_distinct_album_count(self):
        self._seedThreeAlbums()
        self.assertEqual(self.repo.getAlbumsCount("alice"), 3)

    def test_albums_count_respects_date_range(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "Album One", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t1", 5000.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getAlbumsCount("alice", startTs=0, endTs=1000), 1)

    def test_albums_count_zero_when_no_plays(self):
        self.assertEqual(self.repo.getAlbumsCount("alice"), 0)

    def test_filtered_by_album_id_returns_only_that_album(self):
        self._seedThreeAlbums()

        albums = self.repo.getAlbumsPage("alice", albumId="alb2")

        self.assertEqual([a["id"] for a in albums], ["alb2"])

    def test_filtered_by_album_id_unknown_returns_empty(self):
        self._seedThreeAlbums()

        self.assertEqual(self.repo.getAlbumsPage("alice", albumId="missing"), [])

    def test_search_query_matches_album_name(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "A Night at the Opera", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb2", "Unrelated Album", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        albums = self.repo.getAlbumsPage("alice", searchQuery="night at the opera")

        self.assertEqual([a["id"] for a in albums], ["alb1"])

    def test_search_query_matches_artist_name(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "Album One", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb2", "Album Two", "a2"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.commit()

        albums = self.repo.getAlbumsPage("alice", searchQuery="Artist a1")

        self.assertEqual([a["id"] for a in albums], ["alb1"])

    def test_search_match_on_one_track_still_aggregates_the_whole_album(self):
        """A row-level filter (checking only the current play's own track)
        would silently shrink a matching album's totals down to just its
        matching track's plays - the search must be evaluated per-album so a
        match still returns the album's TRUE totals across every track on it,
        matching exactly what a non-search fetch of the same album returns."""
        self.repo.upsertTrack(self._track("t1", "alb1", "Album One", "a1"))     # matches "Artist a1"
        self.repo.upsertTrack(self._track("t2", "alb1", "Album One", "a2"))     # does not match, same album
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 2000)
        self.repo.commit()

        searched = self.repo.getAlbumsPage("alice", searchQuery="Artist a1")
        unfiltered = self.repo.getAlbumsPage("alice")

        self.assertEqual(len(searched), 1)
        self.assertEqual(searched[0]["plays"], unfiltered[0]["plays"])
        self.assertEqual(searched[0]["totalTimeListened"], unfiltered[0]["totalTimeListened"])
        self.assertEqual(searched[0]["uniqueSongCount"], unfiltered[0]["uniqueSongCount"])
        self.assertEqual(searched[0]["plays"], 2)
        self.assertEqual(searched[0]["totalTimeListened"], 3000)

    def test_search_query_paginates_with_getAlbumsCount(self):
        self.repo.upsertTrack(self._track("t1", "alb1", "Bohemian Album", "a1"))
        self.repo.upsertTrack(self._track("t2", "alb2", "Bohemian Remix", "a1"))
        self.repo.upsertTrack(self._track("t3", "alb3", "Unrelated", "a1"))
        self.repo.insertPlay("alice", "t1", 100.0, 1000)
        self.repo.insertPlay("alice", "t2", 200.0, 1000)
        self.repo.insertPlay("alice", "t3", 300.0, 1000)
        self.repo.commit()

        self.assertEqual(self.repo.getAlbumsCount("alice", searchQuery="bohemian"), 2)
        page = self.repo.getAlbumsPage("alice", searchQuery="bohemian", limit=1, offset=0)
        self.assertEqual(len(page), 1)


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

    def test_password_hash_defaults_to_none(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.assertIsNone(self.repo.getUserPasswordHash("alice"))

    def test_password_hash_roundtrip(self):
        self.repo.upsertUser("alice", "alice@example.com")

        self.repo.setUserPassword("alice", "hashed-value")

        self.assertEqual(self.repo.getUserPasswordHash("alice"), "hashed-value")

    def test_password_hash_can_be_updated(self):
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.setUserPassword("alice", "old-hash")
        self.repo.setUserPassword("alice", "new-hash")
        self.assertEqual(self.repo.getUserPasswordHash("alice"), "new-hash")

    def test_add_user_password_hash_column_if_missing_is_a_noop_when_present(self):
        """The column already exists via SCHEMA on a fresh test database -
        calling this again (as migrate1_8_0 does defensively) must not raise."""
        self.repo.upsertUser("alice", "alice@example.com")
        self.repo.addUserPasswordHashColumnIfMissing()
        self.assertIsNone(self.repo.getUserPasswordHash("alice"))


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


class TestUserSettings(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.repo.upsertUser("alice", "alice@example.com")

    def test_default_settings_returned_for_new_user(self):
        settings = self.repo.getUserSettings("alice")
        self.assertEqual(settings["default_dashboard_window"], "day")
        self.assertIsNone(settings["timezone"])

    def test_update_and_get_settings(self):
        self.repo.updateUserSettings("alice", "month", "Europe/London")
        settings = self.repo.getUserSettings("alice")
        self.assertEqual(settings["default_dashboard_window"], "month")
        self.assertEqual(settings["timezone"], "Europe/London")

    def test_settings_scoped_per_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.updateUserSettings("alice", "week", "Asia/Tokyo")
        
        bob_settings = self.repo.getUserSettings("bob")
        self.assertEqual(bob_settings["default_dashboard_window"], "day")
        self.assertIsNone(bob_settings["timezone"])


if __name__ == "__main__":
    unittest.main()
