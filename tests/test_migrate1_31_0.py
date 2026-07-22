import sys
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_31_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_31_0(unittest.TestCase):
    """1.31.0 -> 1.32.0 requeues artists whose Last.fm genre lookup was
    attempted before getArtistTopTags gained normalizeArtistLookupName's
    slash/plus/credit-joiner retry: a name like "Axwell /\\ Ingrosso" whose
    verbatim artist.gettoptags found no tags stayed stuck (no artist_genres
    rows, lastfm_attempted_at IS NOT NULL). lastfm_attempted_at is cleared on
    the transformable ones so the fixed lookup retries the transformed name
    immediately instead of after the 30-day retry window."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.32.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.31.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _seedDatabase(self):
        repo = Repository(self.dbPath)
        conn = repo._conn()
        with conn:
            conn.execute("INSERT INTO artists (id, name, url) VALUES "
                         "('arTransformable', 'Axwell /\\ Ingrosso', '')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('arPlain', 'Pikayzo', '')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES "
                         "('arTransformableWithGenre', 'Florence + The Machine', '')")
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('arNeverTried', 'Above & Beyond', '')")
        repo.markArtistsLastfmAttempted(["arTransformable", "arPlain", "arTransformableWithGenre"])
        repo.replaceArtistGenres("arTransformableWithGenre", ["pop"])
        repo.commit()
        repo.connectionManager.close()

    def _row(self, artistId):
        conn = sqlite3.connect(self.dbPath)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn.execute("SELECT lastfm_attempted_at FROM artists WHERE id=?",
                            (artistId,)).fetchone()

    def test_requeues_only_the_stuck_transformable_artists_and_bumps_the_version(self):
        self._seedDatabase()

        migrateModule.Migrator("1.31.0", "1.32.0").migrate()

        transformable = self._row("arTransformable")
        self.assertIsNone(transformable["lastfm_attempted_at"])   #< requeued

        plain = self._row("arPlain")
        self.assertIsNotNone(plain["lastfm_attempted_at"])   #< not transformable, left alone

        withGenre = self._row("arTransformableWithGenre")
        self.assertIsNotNone(withGenre["lastfm_attempted_at"])   #< already has a genre, left alone

        neverTried = self._row("arNeverTried")
        self.assertIsNone(neverTried["lastfm_attempted_at"])   #< never attempted, left alone

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.32.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.32.0")

    def test_migration_is_idempotent(self):
        self._seedDatabase()
        migrateModule.Migrator("1.31.0", "1.32.0").migrate()

        (self.dataDir / "VERSION").write_text("1.31.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.31.0")
        migrateModule.Migrator("1.31.0", "1.32.0").migrate()   #< must not raise

        self.assertIsNotNone(self._row("arTransformableWithGenre")["lastfm_attempted_at"])
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.32.0")

    def test_empty_database_migrates_cleanly(self):
        Repository(self.dbPath).connectionManager.close()   #< schema only, no rows

        migrateModule.Migrator("1.31.0", "1.32.0").migrate()   #< must not raise

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.32.0")


if __name__ == "__main__":
    unittest.main()
