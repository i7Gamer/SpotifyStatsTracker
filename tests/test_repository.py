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
