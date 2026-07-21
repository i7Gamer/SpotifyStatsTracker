import sys
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_28_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_28_0(unittest.TestCase):
    """1.28.0 -> 1.29.0 requeues albums whose Last.fm bio lookup was attempted
    before the album-bio fetch gained cleanLookupName's decoration-stripping
    retry: a decorated title whose verbatim album.getinfo found no bio stayed
    stuck (bio IS NULL, bio_attempted_at IS NOT NULL). bio_attempted_at is
    cleared on the decorated ones so the fixed lookup retries the undecorated
    title immediately instead of after the 30-day retry window."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.29.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.28.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _seedDatabase(self):
        repo = Repository(self.dbPath)
        conn = repo._conn()
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alDeluxe', 'Album D (Deluxe Edition)', '')")
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alPlain', 'Album P', '')")
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alDeluxeWithBio', 'Album W (Deluxe Edition)', '')")
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alNeverTried', 'Album N (Deluxe Edition)', '')")
        repo.setAlbumBio("alDeluxe", None)                 #< decorated, attempted, no bio -> requeue
        repo.setAlbumBio("alPlain", None)                  #< undecorated, attempted, no bio -> leave
        repo.setAlbumBio("alDeluxeWithBio", "Has a bio.")  #< decorated but has a bio -> leave
        repo.commit()
        repo.connectionManager.close()

    def _row(self, albumId):
        conn = sqlite3.connect(self.dbPath)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn.execute("SELECT bio, bio_attempted_at FROM albums WHERE id=?",
                            (albumId,)).fetchone()

    def test_requeues_only_the_stuck_decorated_albums_and_bumps_the_version(self):
        self._seedDatabase()

        migrateModule.Migrator("1.28.0", "1.29.0").migrate()

        deluxe = self._row("alDeluxe")
        self.assertIsNone(deluxe["bio"])
        self.assertIsNone(deluxe["bio_attempted_at"])   #< requeued

        plain = self._row("alPlain")
        self.assertIsNone(plain["bio"])
        self.assertIsNotNone(plain["bio_attempted_at"])   #< undecorated, left alone

        withBio = self._row("alDeluxeWithBio")
        self.assertEqual(withBio["bio"], "Has a bio.")
        self.assertIsNotNone(withBio["bio_attempted_at"])   #< already has a bio, left alone

        neverTried = self._row("alNeverTried")
        self.assertIsNone(neverTried["bio"])
        self.assertIsNone(neverTried["bio_attempted_at"])   #< never attempted, left alone

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.29.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.29.0")

    def test_migration_is_idempotent(self):
        self._seedDatabase()
        migrateModule.Migrator("1.28.0", "1.29.0").migrate()

        (self.dataDir / "VERSION").write_text("1.28.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.28.0")
        migrateModule.Migrator("1.28.0", "1.29.0").migrate()   #< must not raise

        self.assertEqual(self._row("alDeluxeWithBio")["bio"], "Has a bio.")
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.29.0")

    def test_empty_database_migrates_cleanly(self):
        Repository(self.dbPath).connectionManager.close()   #< schema only, no rows

        migrateModule.Migrator("1.28.0", "1.29.0").migrate()   #< must not raise

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.29.0")


if __name__ == "__main__":
    unittest.main()
