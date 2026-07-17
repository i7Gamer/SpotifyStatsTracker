import sys
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_19_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_19_0(unittest.TestCase):
    """1.19.0 -> 1.20.0 requeues the Last.fm genre backlog: entities without
    OWN (non-inherited) genre rows get their lastfm_attempted_at cleared so
    the improved lookups (tag aliases, cleaned-name retry, album-first
    inheritance, repaired track artists) re-run across them immediately
    instead of after the 30-day retry window. Entities holding own tags keep
    their stamp; inherited rows stay in place so stats keep working until
    the re-run replaces them."""

    STAMP = 1000.0

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.20.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.19.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _seedDatabase(self):
        repo = Repository(self.dbPath)
        conn = repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alShared', 'Shared', '')")
            for trackId in ("tOwn", "tInherited", "tBare", "tNever"):
                conn.execute(
                    "INSERT INTO tracks (id, name, url, album_id, lastfm_attempted_at) VALUES (?, ?, '', 'alShared', ?)",
                    (trackId, trackId, None if trackId == "tNever" else self.STAMP))
            for albumId in ("alOwn", "alInherited"):
                conn.execute(
                    "INSERT INTO albums (id, name, url, lastfm_attempted_at) VALUES (?, ?, '', ?)",
                    (albumId, albumId, self.STAMP))
            for artistId in ("arTagged", "arBare"):
                conn.execute(
                    "INSERT INTO artists (id, name, url, lastfm_attempted_at) VALUES (?, ?, '', ?)",
                    (artistId, artistId, self.STAMP))
        repo.replaceTrackGenres("tOwn", ["rock"], inherited=False)
        repo.replaceTrackGenres("tInherited", ["rock"], inherited=True)
        repo.replaceAlbumGenres("alOwn", ["rock"], inherited=False)
        repo.replaceAlbumGenres("alInherited", ["rock"], inherited=True)
        repo.replaceArtistGenres("arTagged", ["rock"])
        repo.commit()
        repo.connectionManager.close()

    def _stamp(self, table, entityId):
        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        return conn.execute(f"SELECT lastfm_attempted_at FROM {table} WHERE id=?",
                            (entityId,)).fetchone()[0]

    def test_requeues_entities_without_own_tags_and_bumps_the_version(self):
        self._seedDatabase()

        migrateModule.Migrator("1.19.0", "1.20.0").migrate()

        self.assertEqual(self._stamp("tracks", "tOwn"), self.STAMP)         #< own tags: untouched
        self.assertIsNone(self._stamp("tracks", "tInherited"))              #< requeued
        self.assertIsNone(self._stamp("tracks", "tBare"))                   #< requeued
        self.assertIsNone(self._stamp("tracks", "tNever"))                  #< never attempted: still NULL
        self.assertEqual(self._stamp("albums", "alOwn"), self.STAMP)
        self.assertIsNone(self._stamp("albums", "alInherited"))
        self.assertEqual(self._stamp("artists", "arTagged"), self.STAMP)
        self.assertIsNone(self._stamp("artists", "arBare"))

        # Inherited rows survive so genre stats keep working until the re-run.
        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM track_genres WHERE track_id='tInherited'").fetchone()[0], 1)

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.20.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.20.0")

    def test_migration_is_idempotent(self):
        self._seedDatabase()
        migrateModule.Migrator("1.19.0", "1.20.0").migrate()

        (self.dataDir / "VERSION").write_text("1.19.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.19.0")
        migrateModule.Migrator("1.19.0", "1.20.0").migrate()   #< must not raise

        self.assertEqual(self._stamp("tracks", "tOwn"), self.STAMP)
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.20.0")

    def test_empty_database_migrates_cleanly(self):
        Repository(self.dbPath).connectionManager.close()   #< schema only, no rows

        migrateModule.Migrator("1.19.0", "1.20.0").migrate()   #< must not raise

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.20.0")


if __name__ == "__main__":
    unittest.main()
