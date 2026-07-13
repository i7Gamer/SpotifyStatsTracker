import json
import sqlite3
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_6_0 as migrateModule
from Database.repository import Repository, IMAGE_KIND_TRACK


def _track(trackId, albumId="alb1", artistId="art1", name=None):
    return {
        "id": trackId,
        "name": name or f"Song {trackId}",
        "url": f"http://example.com/track/{trackId}",
        "artists": [
            {"id": artistId, "name": f"Artist {artistId}", "url": "u", "imageUrl": "", "imageId": artistId},
        ],
        "album": {
            "id": albumId, "name": "Album", "url": "http://example.com/album",
            "imageId": albumId, "imageUrl": "http://img.example/a.jpg",
            "totalTracks": 1, "releaseDate": 12345.0,
        },
        "imageUrl": "http://img.example/a.jpg",
        "imageId": albumId,
        "duration": 200000,
        "explicit": False,
        "isrc": "",
        "discNumber": 1,
        "trackNumber": 1,
        "releaseDate": 12345.0,
    }


class MigratorTestCase(unittest.TestCase):
    """Builds a temp directory mirroring the real repo-root/Database/Migrators/
    layout (secretsDir and usersDir are computed relative to base.py's own
    __file__, which is what BaseMigrator.baseDir resolves against)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.usersDir = self.root / "Database" / "Users"
        self.usersDir.mkdir(parents=True)
        # Where the migrator renames Users/ -> at the end of a successful run.
        # Not created upfront - most tests assert against this after migrate().
        self.dataDir = self.root / "Database" / "Data"
        self.secretsDir = self.root / "secrets"
        self.secretsDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.7.0", encoding="utf-8")
        (self.usersDir / "VERSION").write_text("1.6.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

    def _writeUser(self, username, entries=None, tracks=None, playlists=None, images=None):
        userDir = self.usersDir / username
        userDir.mkdir(parents=True, exist_ok=True)
        (userDir / "entries.json").write_text(json.dumps(entries or []), encoding="utf-8")
        (userDir / "tracks.json").write_text(json.dumps(tracks or {}), encoding="utf-8")
        (userDir / "playlists.json").write_text(json.dumps(playlists or {"album": {}, "playlist": {}}), encoding="utf-8")
        if images:
            for kind, imgId, content in images:
                imgDir = userDir / "img" / kind
                imgDir.mkdir(parents=True, exist_ok=True)
                (imgDir / f"{imgId}.jpeg").write_bytes(content)
        return userDir

    def _repo(self, atDir=None):
        """Repository pointed at the given directory, defaulting to Data/ (the
        post-successful-migration location). Pass self.usersDir explicitly for
        scenarios where no rename happened (e.g. a failed migration)."""
        repo = Repository((atDir or self.dataDir) / "spotify_stats.db")
        self.addCleanup(repo.connectionManager.close)
        return repo


class TestSingleUserMigration(MigratorTestCase):
    def test_entries_tracks_and_playlists_migrate(self):
        self._writeUser(
            "alice",
            entries=[{"id": "t1", "playedAt": 100.0, "timePlayed": 5000, "playedFrom": "playlist:p1"}],
            tracks={"t1": _track("t1")},
            playlists={"album": {}, "playlist": {"p1": "My Playlist"}},
        )

        Migrator = migrateModule.Migrator
        Migrator("1.6.0", "1.7.0").migrate()

        self.assertFalse(self.usersDir.exists(), "Users/ must be renamed away, not left behind")
        repo = self._repo()
        self.assertEqual(repo.getPlaysCount("alice"), 1)
        self.assertIsNotNone(repo.getTrack("t1"))
        self.assertEqual(repo.getPlaylistName("p1", "playlist"), "My Playlist")
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.7.0")

    def test_images_moved_to_shared_media_dir(self):
        self._writeUser(
            "alice",
            tracks={"t1": _track("t1", albumId="alb1")},
            images=[("tracks", "alb1", b"fake-jpeg-bytes")],
        )

        migrateModule.Migrator("1.6.0", "1.7.0").migrate()

        mediaFile = self.dataDir / "Media" / "tracks" / "alb1.jpeg"
        self.assertTrue(mediaFile.exists())
        self.assertEqual(mediaFile.read_bytes(), b"fake-jpeg-bytes")
        self.assertFalse((self.dataDir / "alice" / "img" / "tracks" / "alb1.jpeg").exists())

        repo = self._repo()
        self.assertEqual(repo.imageStatus("alb1", IMAGE_KIND_TRACK), "ok")


class TestMultiUserSharedCatalog(MigratorTestCase):
    def test_same_track_across_users_creates_one_row_and_two_plays(self):
        self._writeUser("alice", entries=[{"id": "t1", "playedAt": 100.0, "timePlayed": 5000}],
                         tracks={"t1": _track("t1")})
        self._writeUser("bob", entries=[{"id": "t1", "playedAt": 200.0, "timePlayed": 3000}],
                         tracks={"t1": _track("t1")})

        migrateModule.Migrator("1.6.0", "1.7.0").migrate()

        repo = self._repo()
        conn = sqlite3.connect(self.dataDir / "spotify_stats.db")
        trackCount = conn.execute("SELECT COUNT(*) FROM tracks WHERE id='t1'").fetchone()[0]
        playCount = conn.execute("SELECT COUNT(*) FROM plays WHERE track_id='t1'").fetchone()[0]
        conn.close()

        self.assertEqual(trackCount, 1)
        self.assertEqual(playCount, 2)
        self.assertEqual(repo.getPlaysCount("alice"), 1)
        self.assertEqual(repo.getPlaysCount("bob"), 1)

    def test_same_image_across_users_is_not_duplicated(self):
        self._writeUser("alice", tracks={"t1": _track("t1", albumId="shared-alb")},
                         images=[("tracks", "shared-alb", b"cover-bytes")])
        self._writeUser("bob", tracks={"t2": _track("t2", albumId="shared-alb")},
                         images=[("tracks", "shared-alb", b"cover-bytes")])

        migrateModule.Migrator("1.6.0", "1.7.0").migrate()  #< must not raise on the second user's duplicate image

        mediaDir = self.dataDir / "Media" / "tracks"
        self.assertEqual(list(mediaDir.glob("shared-alb.jpeg")), [mediaDir / "shared-alb.jpeg"])
        # Bob's own copy must be cleaned up, not left behind as a duplicate.
        self.assertFalse((self.dataDir / "bob" / "img" / "tracks" / "shared-alb.jpeg").exists())


class TestCookiesAndUsersMapMigration(MigratorTestCase):
    def test_email_and_cookies_linked_to_username(self):
        (self.secretsDir / "users_map.json").write_text(
            json.dumps({"alice@example.com": "alice"}), encoding="utf-8")
        (self.secretsDir / "cookies.json").write_text(
            json.dumps([{"identifier": "alice@example.com", "cookies": {"sp_dc": "abc123"}}]), encoding="utf-8")
        self._writeUser("alice")

        migrateModule.Migrator("1.6.0", "1.7.0").migrate()

        repo = self._repo()
        self.assertEqual(repo.getUsernameForEmail("alice@example.com"), "alice")
        self.assertEqual(repo.getUserCookies("alice"), {"sp_dc": "abc123"})

    def test_user_without_mapped_email_still_migrates(self):
        self._writeUser("orphan", entries=[{"id": "t1", "playedAt": 1.0, "timePlayed": 1000}],
                         tracks={"t1": _track("t1")})

        migrateModule.Migrator("1.6.0", "1.7.0").migrate()  #< must not raise despite no users_map/cookies files

        repo = self._repo()
        self.assertEqual(repo.getPlaysCount("orphan"), 1)


class TestIdempotentRetry(MigratorTestCase):
    def test_running_twice_does_not_duplicate_or_error(self):
        self._writeUser("alice", entries=[{"id": "t1", "playedAt": 100.0, "timePlayed": 5000}],
                         tracks={"t1": _track("t1")})

        migrateModule.Migrator("1.6.0", "1.7.0").migrate()
        # Simulate a restart before the version bump was durably seen (e.g. a
        # crash right after migrate() returned): the rename to Data/ already
        # happened, so the "stale" marker lives there now, not under Users/.
        (self.dataDir / "VERSION").write_text("1.6.0", encoding="utf-8")
        migrateModule.Migrator("1.6.0", "1.7.0").migrate()

        self.assertFalse(self.usersDir.exists())
        repo = self._repo()
        self.assertEqual(repo.getPlaysCount("alice"), 1)


class TestNoUsersDirectory(MigratorTestCase):
    def test_no_user_subdirectories_yet_still_renames_and_bumps_version(self):
        """migrateIfNeeded() (Migrators/migrate.py) already guarantees Users/VERSION
        exists before any numbered Migrator runs - a fresh-ish install with no user
        subdirectories yet just means zero users to iterate, not a missing Users/."""
        migrateModule.Migrator("1.6.0", "1.7.0").migrate()

        self.assertFalse(self.usersDir.exists())
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.7.0")


class TestPartialFailure(MigratorTestCase):
    def test_one_bad_user_blocks_version_bump_and_rename_but_other_users_still_migrate(self):
        self._writeUser("alice", entries=[{"id": "t1", "playedAt": 100.0, "timePlayed": 5000}],
                         tracks={"t1": _track("t1")})
        badUserDir = self._writeUser("bob")
        # Missing required "playedAt" key -> KeyError while migrating bob's entries.
        (badUserDir / "entries.json").write_text(json.dumps([{"id": "t1"}]), encoding="utf-8")
        (badUserDir / "tracks.json").write_text(json.dumps({"t1": _track("t1")}), encoding="utf-8")

        with self.assertRaises(RuntimeError) as ctx:
            migrateModule.Migrator("1.6.0", "1.7.0").migrate()
        self.assertIn("bob", str(ctx.exception))

        # A failed migration must not rename Users/ away - the next retry needs
        # to find the same JSON files (including bob's) still there.
        self.assertTrue(self.usersDir.exists())
        self.assertFalse(self.dataDir.exists())

        # alice's data (migrated before bob failed) must still be there.
        repo = self._repo(atDir=self.usersDir)
        self.assertEqual(repo.getPlaysCount("alice"), 1)
        # Version must NOT have advanced, so the next startup retries.
        self.assertEqual((self.usersDir / "VERSION").read_text(encoding="utf-8").strip(), "1.6.0")


if __name__ == "__main__":
    unittest.main()
