import sys
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_24_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_24_0(unittest.TestCase):
    """1.24.0 -> 1.25.0 requeues albums without own Last.fm tags so the
    album.getinfo fallback fix (album.gettoptags was found to miss real tag
    data for ~46% of tag-less albums) re-runs across the existing backlog
    immediately instead of after the 30-day retry window. Scoped to albums
    only - artists and tracks are untouched since the underlying fix doesn't
    change their lookups."""

    STAMP = 1000.0

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.25.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.24.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _seedDatabase(self):
        repo = Repository(self.dbPath)
        conn = repo._conn()
        with conn:
            for albumId in ("alOwn", "alInherited", "alBare", "alNever"):
                conn.execute(
                    "INSERT INTO albums (id, name, url, lastfm_attempted_at) VALUES (?, ?, '', ?)",
                    (albumId, albumId, None if albumId == "alNever" else self.STAMP))
            conn.execute("INSERT INTO artists (id, name, url, lastfm_attempted_at) VALUES ('arBare', 'arBare', '', ?)",
                        (self.STAMP,))
            conn.execute(
                "INSERT INTO tracks (id, name, url, album_id, lastfm_attempted_at) VALUES ('tBare', 'tBare', '', 'alBare', ?)",
                (self.STAMP,))
        repo.replaceAlbumGenres("alOwn", ["rock"], inherited=False)
        repo.replaceAlbumGenres("alInherited", ["rock"], inherited=True)
        repo.commit()
        repo.connectionManager.close()

    def _stamp(self, table, entityId):
        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        return conn.execute(f"SELECT lastfm_attempted_at FROM {table} WHERE id=?",
                            (entityId,)).fetchone()[0]

    def test_requeues_tagless_albums_only_and_bumps_the_version(self):
        self._seedDatabase()

        migrateModule.Migrator("1.24.0", "1.25.0").migrate()

        self.assertEqual(self._stamp("albums", "alOwn"), self.STAMP)      #< own tags: untouched
        self.assertIsNone(self._stamp("albums", "alInherited"))           #< requeued
        self.assertIsNone(self._stamp("albums", "alBare"))                #< requeued
        self.assertIsNone(self._stamp("albums", "alNever"))               #< never attempted: still NULL

        # Artists and tracks are untouched - this fix doesn't change their lookups.
        self.assertEqual(self._stamp("artists", "arBare"), self.STAMP)
        self.assertEqual(self._stamp("tracks", "tBare"), self.STAMP)

        # Inherited rows survive so genre stats keep working until the re-run.
        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM album_genres WHERE album_id='alInherited'").fetchone()[0], 1)

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.25.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.25.0")

    def test_migration_is_idempotent(self):
        self._seedDatabase()
        migrateModule.Migrator("1.24.0", "1.25.0").migrate()

        (self.dataDir / "VERSION").write_text("1.24.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.24.0")
        migrateModule.Migrator("1.24.0", "1.25.0").migrate()   #< must not raise

        self.assertEqual(self._stamp("albums", "alOwn"), self.STAMP)
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.25.0")

    def test_empty_database_migrates_cleanly(self):
        Repository(self.dbPath).connectionManager.close()   #< schema only, no rows

        migrateModule.Migrator("1.24.0", "1.25.0").migrate()   #< must not raise

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.25.0")


if __name__ == "__main__":
    unittest.main()
