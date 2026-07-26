"""1.43.0 -> 1.44.0: repair the dangling foreign-key rows the boot probe finds.

Repository.checkIntegrity has counted them at every startup since 3434381 and
nothing has ever acted on them - the live database carried the same 201 for over
a week. They come in two shapes and they are NOT the same problem:

  plays -> tracks          real listening history that every track-joined query
                           silently drops, so raw totals and per-page totals
                           disagree. Deleting those rows would destroy the only
                           record of it; the repair is a placeholder track that
                           makes the play visible and lets real metadata replace
                           it later.

  track_artists -> tracks  an artist credit for a track that no longer exists.
                           Nothing can render it. Once the placeholders above
                           exist, whatever is still orphaned is genuinely
                           unreferenced and is deleted.

Order matters: placeholders first, so an orphan credit belonging to a track that
still has plays is revived rather than swept.
"""
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_43_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository
from Database.db import (RESTRICTED_FALLBACK_REASON, SYNTHETIC_FALLBACK_REASON,
                         UNKNOWN_TRACK_NAME, UNKNOWN_ALBUM_NAME)


def _track(trackId):
    return {
        "id": trackId,
        "name": f"Track {trackId}",
        "url": f"https://open.spotify.com/track/{trackId}",
        "artists": [{"id": "a1", "name": "Artist One", "url": "http://example.com/artist/a1",
                     "imageUrl": "", "imageId": "a1"}],
        "album": {
            "id": "al1", "name": "Album", "url": "http://example.com/album/al1",
            "imageId": "al1", "imageUrl": "", "totalTracks": 10, "releaseDate": 0.0,
        },
        "imageUrl": "", "imageId": "al1", "duration": 200_000, "explicit": False,
        "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0.0,
    }


class TestMigrate1_43_0(unittest.TestCase):
    USER = "someone"
    # Real 22-character Spotify ids, not readable stand-ins: the migration reads
    # the id's SHAPE to decide whether a placeholder gets a working Spotify link
    # (see _looksLikeARealSpotifyId), so a short fixture id would silently
    # exercise the fabricated-id branch instead.
    KEPT = "4cOdK2wGLETKBW3PvgPWqT"                    #< healthy, must be left alone
    ORPHANED_WITH_PLAYS = "02fKZbUiVfqrRl4Eg1YIUz"
    ORPHANED_UNREFERENCED = "6f1oG9hTx3NETgV6q4rkw5"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.44.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.43.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _seedDatabaseAt(self, version):
        """Three tracks, then two of them deleted out from under their rows -
        the state the live database is actually in."""
        repo = Repository(self.dbPath)
        repo.upsertUser(self.USER, "someone@example.com", createdAt=100.0)
        for trackId in (self.KEPT, self.ORPHANED_WITH_PLAYS, self.ORPHANED_UNREFERENCED):
            repo.upsertTrack(_track(trackId))
        repo.insertPlay(self.USER, self.KEPT, 1000.0, 120_000)
        repo.insertPlay(self.USER, self.ORPHANED_WITH_PLAYS, 2000.0, 224_037)
        repo.insertPlay(self.USER, self.ORPHANED_WITH_PLAYS, 3000.0, 177_136)
        repo.commit()
        # Foreign keys are enforced on normal connections, so the corruption is
        # written the way it happened: the tracks vanished, their dependent rows
        # did not.
        conn = repo._conn()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM tracks WHERE id IN (?, ?)",
                     (self.ORPHANED_WITH_PLAYS, self.ORPHANED_UNREFERENCED))
        repo.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        repo.connectionManager.close()
        dbversion.writeDbVersion(self.dbPath, version)

    def _repo(self):
        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        return repo

    def _violations(self):
        return self._repo().checkIntegrity()["foreignKeyViolations"]

    def _migrate(self):
        migrateModule.Migrator("1.43.0", "1.44.0").migrate()

    def test_the_probe_comes_back_clean(self):
        self._seedDatabaseAt("1.43.0")
        self.assertTrue(self._violations(), "fixture did not reproduce the corruption")

        self._migrate()

        self.assertEqual(self._violations(), {})

    def test_the_orphaned_plays_survive_and_become_visible(self):
        """The whole point: this is real listening history. It must still be
        there afterwards, and it must now join to a track."""
        self._seedDatabaseAt("1.43.0")

        self._migrate()

        rows = self._repo()._conn().execute(
            "SELECT p.time_played FROM plays p JOIN tracks t ON t.id = p.track_id "
            "WHERE p.track_id = ? ORDER BY p.played_at", (self.ORPHANED_WITH_PLAYS,)).fetchall()
        self.assertEqual([r["time_played"] for r in rows], [224_037, 177_136])

    def test_the_placeholder_is_marked_so_real_metadata_can_replace_it(self):
        self._seedDatabaseAt("1.43.0")

        self._migrate()

        row = self._repo()._conn().execute(
            "SELECT name, url, created_reason FROM tracks WHERE id = ?",
            (self.ORPHANED_WITH_PLAYS,)).fetchone()
        self.assertEqual(row["created_reason"], RESTRICTED_FALLBACK_REASON)
        self.assertEqual(row["name"], UNKNOWN_TRACK_NAME)
        #< the id is real, so the Spotify link is real - only fabricated ids carry an empty url
        self.assertIn(self.ORPHANED_WITH_PLAYS, row["url"])

    def test_a_fabricated_track_id_gets_no_link_and_the_synthetic_marker(self):
        """The importer's surrogate for a track it could not resolve is a bare
        md5 digest, not a Spotify id - pointing open.spotify.com at one gives a
        404. "Only fabricated ids carry an empty url" is the rule everywhere
        else here, and it decides the marker too: nothing can ever repair a
        fabricated id, which is what SYNTHETIC means as against RESTRICTED."""
        fabricated = "0123456789abcdef0123456789abcdef"   #< 32-char md5, as _createSyntheticTrack emits
        repo = Repository(self.dbPath)
        repo.upsertUser(self.USER, "someone@example.com", createdAt=100.0)
        repo.upsertTrack(_track(fabricated))
        repo.insertPlay(self.USER, fabricated, 1000.0, 120_000)
        repo.commit()
        conn = repo._conn()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM tracks WHERE id = ?", (fabricated,))
        repo.commit()
        repo.connectionManager.close()
        dbversion.writeDbVersion(self.dbPath, "1.43.0")

        self._migrate()

        row = self._repo()._conn().execute(
            "SELECT url, created_reason FROM tracks WHERE id = ?", (fabricated,)).fetchone()
        self.assertEqual(row["url"], "")
        self.assertEqual(row["created_reason"], SYNTHETIC_FALLBACK_REASON)

    def test_the_placeholders_album_is_named_as_an_album(self):
        """upsertTrack synthesizes a per-track album when none is supplied and
        names it after the TRACK, so the placeholder's album read "Unknown
        Track" on the detail page and in every album link. Verified against a
        copy of the real database, where all five revived tracks came back with
        an album called "Unknown Track"."""
        self._seedDatabaseAt("1.43.0")

        self._migrate()

        albumName = self._repo()._conn().execute(
            "SELECT al.name FROM tracks t JOIN albums al ON al.id = t.album_id WHERE t.id = ?",
            (self.ORPHANED_WITH_PLAYS,)).fetchone()["name"]
        self.assertEqual(albumName, UNKNOWN_ALBUM_NAME)

    def test_the_placeholders_album_id_stays_out_of_the_backfill_queue(self):
        """The `album_` prefix is what getAlbumsMissingMetadata excludes - this
        album never existed on Spotify, so asking for it would 404 every cycle
        forever."""
        self._seedDatabaseAt("1.43.0")

        self._migrate()

        albumId = self._repo()._conn().execute(
            "SELECT album_id FROM tracks WHERE id = ?", (self.ORPHANED_WITH_PLAYS,)).fetchone()["album_id"]
        self.assertTrue(albumId.startswith("album_"), albumId)

    def test_a_revived_tracks_artist_credits_are_kept_not_swept(self):
        """Those credits point at artists that still exist, so reviving the
        track restores the play's real artist rather than leaving it blank."""
        self._seedDatabaseAt("1.43.0")

        self._migrate()

        credits = self._repo()._conn().execute(
            "SELECT artist_id FROM track_artists WHERE track_id = ?",
            (self.ORPHANED_WITH_PLAYS,)).fetchall()
        self.assertEqual([r["artist_id"] for r in credits], ["a1"])

    def test_credits_for_a_track_nothing_references_are_deleted(self):
        self._seedDatabaseAt("1.43.0")

        self._migrate()

        remaining = self._repo()._conn().execute(
            "SELECT COUNT(*) AS n FROM track_artists WHERE track_id = ?",
            (self.ORPHANED_UNREFERENCED,)).fetchone()["n"]
        self.assertEqual(remaining, 0)

    def test_a_track_with_no_plays_is_not_resurrected(self):
        """Only plays justify a placeholder - inventing catalog rows for
        credits nobody listened to would grow the catalog with noise."""
        self._seedDatabaseAt("1.43.0")

        self._migrate()

        self.assertIsNone(self._repo().getTrack(self.ORPHANED_UNREFERENCED))

    def test_healthy_rows_are_untouched(self):
        self._seedDatabaseAt("1.43.0")

        self._migrate()

        repo = self._repo()
        kept = repo.getTrack(self.KEPT)
        self.assertEqual(kept["name"], f"Track {self.KEPT}")
        self.assertIsNone(kept["created_reason"])
        self.assertEqual(
            repo._conn().execute("SELECT COUNT(*) AS n FROM plays").fetchone()["n"], 3)

    def test_bumps_both_version_markers(self):
        self._seedDatabaseAt("1.43.0")

        self._migrate()

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.44.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.44.0")

    def test_rejects_a_database_not_on_the_from_version(self):
        self._seedDatabaseAt("1.42.0")
        with self.assertRaisesRegex(Exception, "does not match migrator's expected from-version"):
            self._migrate()

    def test_migration_is_idempotent_on_retry(self):
        self._seedDatabaseAt("1.43.0")
        self._migrate()

        (self.dataDir / "VERSION").write_text("1.43.0", encoding="utf-8")
        dbversion.writeDbVersion(self.dbPath, "1.43.0")
        self._migrate()   #< must not raise

        self.assertEqual(self._violations(), {})
        self.assertEqual(
            self._repo()._conn().execute("SELECT COUNT(*) AS n FROM plays").fetchone()["n"], 3)

    def test_a_healthy_database_migrates_without_changing_anything(self):
        """Most instances have no orphans at all - the migration must be a
        version bump for them."""
        repo = Repository(self.dbPath)
        repo.upsertUser(self.USER, "someone@example.com", createdAt=100.0)
        repo.upsertTrack(_track(self.KEPT))
        repo.insertPlay(self.USER, self.KEPT, 1000.0, 120_000)
        repo.commit()
        repo.connectionManager.close()
        dbversion.writeDbVersion(self.dbPath, "1.43.0")

        self._migrate()

        repo = self._repo()
        self.assertEqual(
            repo._conn().execute("SELECT COUNT(*) AS n FROM tracks").fetchone()["n"], 1)
        self.assertEqual(self._violations(), {})


if __name__ == "__main__":
    unittest.main()
