import unittest
from pathlib import Path
from Database.repository import Repository
from Database.queries.tags import normalizeTag


def makeTrack(trackId="t1", name="Song One", albumId="alb1", artistId="art1"):
    return {
        "id": trackId,
        "name": name,
        "url": f"http://example.com/track/{trackId}",
        "artists": [
            {"id": artistId, "name": f"Artist {artistId}", "url": f"http://example.com/artist/{artistId}",
             "imageUrl": "", "imageId": artistId},
        ],
        "album": {
            "id": albumId, "name": f"Album {albumId}", "url": f"http://example.com/album/{albumId}",
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


class TestTagQueries(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(":memory:")
        self.repo = Repository(self.db_path)

        self.repo.upsertUser("alice", "alice@example.com")
        # Seed sample catalog and plays
        self.repo.upsertTrack(makeTrack(trackId="t1", albumId="alb1", artistId="art1"))
        self.repo.upsertTrack(makeTrack(trackId="t2", albumId="alb2", artistId="art2"))
        self.repo.insertPlay("alice", "t1", 1000.0, 200000)
        self.repo.insertPlay("alice", "t2", 2000.0, 180000)
        self.repo.commit()

    def tearDown(self):
        self.repo.connectionManager.close()

    def test_normalize_tag(self):
        self.assertEqual(normalizeTag(" #Chill  "), "chill")
        self.assertEqual(normalizeTag("##WORKOUT"), "workout")
        self.assertEqual(normalizeTag(""), "")
        self.assertEqual(normalizeTag(None), "")

    def test_add_and_get_tags(self):
        norm = self.repo.addTag("alice", "#Chill", "track", "t1")
        self.assertEqual(norm, "chill")

        tags = self.repo.getTagsForEntity("alice", "track", "t1")
        self.assertEqual(tags, ["chill"])

        user_tags = self.repo.getUserTags("alice")
        self.assertEqual(user_tags, [{"tag": "chill", "count": 1}])

    def test_remove_tag(self):
        self.repo.addTag("alice", "workout", "track", "t1")
        self.assertTrue(self.repo.removeTag("alice", "#workout", "track", "t1"))
        self.assertFalse(self.repo.removeTag("alice", "workout", "track", "t1"))
        self.assertEqual(self.repo.getTagsForEntity("alice", "track", "t1"), [])

    def test_inherited_tagging_query(self):
        # Tag artist 2
        self.repo.addTag("alice", "rock", "artist", "art2")
        # Tag track 1 directly
        self.repo.addTag("alice", "rock", "track", "t1")

        # Any match mode
        track_ids = self.repo.getTaggedTrackIds("alice", ["rock"], match_mode="any")
        self.assertCountEqual(track_ids, ["t1", "t2"])

    def test_inherited_tagging_matches_featured_not_just_primary_artist(self):
        # t1 already has a single primary artist (art1); add a featured
        # (non-primary, position 1) artist so tagging them still surfaces t1.
        track = makeTrack(trackId="t1", albumId="alb1", artistId="art1")
        track["artists"].append(
            {"id": "feat1", "name": "Artist feat1", "url": "http://example.com/artist/feat1",
             "imageUrl": "", "imageId": "feat1"}
        )
        self.repo.upsertTrack(track)
        self.repo.commit()

        self.repo.addTag("alice", "duet", "artist", "feat1")

        track_ids = self.repo.getTaggedTrackIds("alice", ["duet"], match_mode="any")
        self.assertEqual(track_ids, ["t1"])

    def test_match_mode_all(self):
        self.repo.addTag("alice", "chill", "track", "t1")
        self.repo.addTag("alice", "workout", "track", "t1")
        self.repo.addTag("alice", "workout", "track", "t2")

        track_ids = self.repo.getTaggedTrackIds("alice", ["chill", "workout"], match_mode="all")
        self.assertEqual(track_ids, ["t1"])

    def test_match_mode_any_unions_multiple_tags(self):
        # Exercises the single-query `tag IN (...)` union path across >1 tag.
        self.repo.addTag("alice", "chill", "track", "t1")
        self.repo.addTag("alice", "workout", "track", "t2")

        track_ids = self.repo.getTaggedTrackIds("alice", ["chill", "workout"], match_mode="any")
        self.assertCountEqual(track_ids, ["t1", "t2"])

    def test_duplicate_tags_are_deduped(self):
        # A repeated tag must not change the result in either mode (and in "all"
        # must not make the tag count require an impossible double-match).
        self.repo.addTag("alice", "chill", "track", "t1")

        self.assertEqual(
            self.repo.getTaggedTrackIds("alice", ["chill", "chill"], match_mode="all"), ["t1"])
        self.assertEqual(
            self.repo.getTaggedTrackIds("alice", ["chill", "#chill", " CHILL "], match_mode="any"), ["t1"])

    def test_get_tagged_artist_ids_matches_direct_tags_only(self):
        self.repo.addTag("alice", "favorite", "artist", "art1")
        # A track tagged (not its artist) must not leak into artist results -
        # unlike getTaggedTrackIds, this is a direct-tag-only lookup.
        self.repo.addTag("alice", "favorite", "track", "t2")

        artist_ids = self.repo.getTaggedArtistIds("alice", ["favorite"])
        self.assertEqual(artist_ids, ["art1"])

    def test_get_tagged_artist_ids_unions_multiple_tags(self):
        self.repo.addTag("alice", "rock", "artist", "art1")
        self.repo.addTag("alice", "pop", "artist", "art2")

        artist_ids = self.repo.getTaggedArtistIds("alice", ["rock", "pop"])
        self.assertCountEqual(artist_ids, ["art1", "art2"])

    def test_get_tagged_artist_ids_empty_when_no_tags(self):
        self.assertEqual(self.repo.getTaggedArtistIds("alice", []), [])
        self.assertEqual(self.repo.getTaggedArtistIds("alice", ["nonexistent"]), [])

    def test_get_tagged_album_ids_matches_direct_tags_only(self):
        self.repo.addTag("alice", "roadtrip", "album", "alb1")
        # A track tagged (not its album) must not leak into album results.
        self.repo.addTag("alice", "roadtrip", "track", "t2")

        album_ids = self.repo.getTaggedAlbumIds("alice", ["roadtrip"])
        self.assertEqual(album_ids, ["alb1"])

    def test_get_tagged_album_ids_unions_multiple_tags(self):
        self.repo.addTag("alice", "chill", "album", "alb1")
        self.repo.addTag("alice", "workout", "album", "alb2")

        album_ids = self.repo.getTaggedAlbumIds("alice", ["chill", "workout"])
        self.assertCountEqual(album_ids, ["alb1", "alb2"])

    def test_get_tagged_album_ids_empty_when_no_tags(self):
        self.assertEqual(self.repo.getTaggedAlbumIds("alice", []), [])
        self.assertEqual(self.repo.getTaggedAlbumIds("alice", ["nonexistent"]), [])

    def test_rename_tag(self):
        self.repo.addTag("alice", "oldname", "track", "t1")
        self.repo.addTag("alice", "oldname", "artist", "art1")

        renamed_cnt = self.repo.renameTag("alice", "#oldname", "newname")
        self.assertGreater(renamed_cnt, 0)
        self.assertEqual(self.repo.getTagsForEntity("alice", "track", "t1"), ["newname"])

    def test_delete_tag(self):
        self.repo.addTag("alice", "temp", "track", "t1")
        deleted_cnt = self.repo.deleteTag("alice", "#temp")
        self.assertEqual(deleted_cnt, 1)
        self.assertEqual(self.repo.getTagsForEntity("alice", "track", "t1"), [])


class TestTagsAreScopedPerUser(unittest.TestCase):
    """Every TagQueries method scopes by `username = ?`, but the rest of this
    file only ever seeds one user - so a dropped clause in a rewrite would leak
    (or destroy) other people's tags on a real multi-user instance with nothing
    failing. Two users tag the SAME tracks with the SAME labels here, so any
    method that forgets its scope shows up immediately."""

    def setUp(self):
        self.repo = Repository(Path(":memory:"))
        for username in ("alice", "bob"):
            self.repo.upsertUser(username, f"{username}@example.com")
        self.repo.upsertTrack(makeTrack(trackId="t1", albumId="alb1", artistId="art1"))
        self.repo.upsertTrack(makeTrack(trackId="t2", albumId="alb2", artistId="art2"))
        for username in ("alice", "bob"):
            self.repo.insertPlay(username, "t1", 1000.0, 200000)
            self.repo.insertPlay(username, "t2", 2000.0, 180000)
        self.repo.commit()

    def tearDown(self):
        self.repo.connectionManager.close()

    def test_one_users_tag_is_invisible_to_another(self):
        self.repo.addTag("alice", "chill", "track", "t1")

        self.assertEqual(self.repo.getTagsForEntity("alice", "track", "t1"), ["chill"])
        self.assertEqual(self.repo.getTagsForEntity("bob", "track", "t1"), [])

    def test_the_tag_list_is_per_user(self):
        self.repo.addTag("alice", "chill", "track", "t1")
        self.repo.addTag("bob", "workout", "track", "t1")

        self.assertEqual([t["tag"] for t in self.repo.getUserTags("alice")], ["chill"])
        self.assertEqual([t["tag"] for t in self.repo.getUserTags("bob")], ["workout"])

    def test_identical_tags_on_the_same_track_stay_separate(self):
        self.repo.addTag("alice", "chill", "track", "t1")
        self.repo.addTag("bob", "chill", "track", "t1")

        self.assertEqual([t["count"] for t in self.repo.getUserTags("alice")], [1])
        self.assertEqual([t["count"] for t in self.repo.getUserTags("bob")], [1])

    def test_removing_a_tag_leaves_the_other_users_copy(self):
        self.repo.addTag("alice", "chill", "track", "t1")
        self.repo.addTag("bob", "chill", "track", "t1")

        self.repo.removeTag("alice", "chill", "track", "t1")

        self.assertEqual(self.repo.getTagsForEntity("alice", "track", "t1"), [])
        self.assertEqual(self.repo.getTagsForEntity("bob", "track", "t1"), ["chill"])

    def test_deleting_a_tag_everywhere_only_touches_the_callers_rows(self):
        self.repo.addTag("alice", "chill", "track", "t1")
        self.repo.addTag("alice", "chill", "track", "t2")
        self.repo.addTag("bob", "chill", "track", "t1")

        deleted = self.repo.deleteTag("alice", "chill")

        self.assertEqual(deleted, 2)
        self.assertEqual(self.repo.getTagsForEntity("bob", "track", "t1"), ["chill"])

    def test_renaming_a_tag_only_touches_the_callers_rows(self):
        self.repo.addTag("alice", "chill", "track", "t1")
        self.repo.addTag("bob", "chill", "track", "t1")

        self.repo.renameTag("alice", "chill", "relaxed")

        self.assertEqual(self.repo.getTagsForEntity("alice", "track", "t1"), ["relaxed"])
        self.assertEqual(self.repo.getTagsForEntity("bob", "track", "t1"), ["chill"])

    def test_tagged_id_lookups_are_per_user(self):
        self.repo.addTag("alice", "chill", "track", "t1")
        self.repo.addTag("bob", "chill", "track", "t2")
        self.repo.addTag("alice", "chill", "artist", "art1")
        self.repo.addTag("bob", "chill", "artist", "art2")
        self.repo.addTag("alice", "chill", "album", "alb1")
        self.repo.addTag("bob", "chill", "album", "alb2")

        self.assertEqual(self.repo.getTaggedTrackIds("alice", ["chill"]), ["t1"])
        self.assertEqual(self.repo.getTaggedTrackIds("bob", ["chill"]), ["t2"])
        self.assertEqual(self.repo.getTaggedArtistIds("alice", ["chill"]), ["art1"])
        self.assertEqual(self.repo.getTaggedArtistIds("bob", ["chill"]), ["art2"])
        self.assertEqual(self.repo.getTaggedAlbumIds("alice", ["chill"]), ["alb1"])
        self.assertEqual(self.repo.getTaggedAlbumIds("bob", ["chill"]), ["alb2"])


if __name__ == "__main__":
    unittest.main()
