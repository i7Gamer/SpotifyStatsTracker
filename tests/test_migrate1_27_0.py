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
    """1.27.0 -> 1.28.0 adds albums.bio and albums.bio_attempted_at for the
    album-bio feature (lazily fetched from Last.fm's album.getinfo wiki
    field), mirroring migrate1_25_0's artists.bio columns."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.28.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.27.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _columns(self, table):
        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def test_adds_album_bio_columns_and_bumps_the_version(self):
        Repository(self.dbPath).connectionManager.close()   #< schema only, no rows

        migrateModule.Migrator("1.27.0", "1.28.0").migrate()

        columns = self._columns("albums")
        self.assertIn("bio", columns)
        self.assertIn("bio_attempted_at", columns)

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.28.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.28.0")

    def test_existing_album_rows_are_preserved(self):
        repo = Repository(self.dbPath)
        conn = repo._conn()
        with conn:
            conn.execute(
                "INSERT INTO albums (id, name, url) VALUES ('alExisting', 'Existing Album', '')"
            )
        repo.commit()
        repo.connectionManager.close()

        migrateModule.Migrator("1.27.0", "1.28.0").migrate()

        conn = sqlite3.connect(self.dbPath)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        row = conn.execute("SELECT name, bio, bio_attempted_at FROM albums WHERE id='alExisting'").fetchone()
        self.assertEqual(row["name"], "Existing Album")
        self.assertIsNone(row["bio"])
        self.assertIsNone(row["bio_attempted_at"])

    def test_migration_is_idempotent(self):
        Repository(self.dbPath).connectionManager.close()
        migrateModule.Migrator("1.27.0", "1.28.0").migrate()

        (self.dataDir / "VERSION").write_text("1.27.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.27.0")
        migrateModule.Migrator("1.27.0", "1.28.0").migrate()   #< must not raise

        self.assertIn("bio", self._columns("albums"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.28.0")

    def test_empty_database_migrates_cleanly(self):
        Repository(self.dbPath).connectionManager.close()   #< schema only, no rows

        migrateModule.Migrator("1.27.0", "1.28.0").migrate()   #< must not raise

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.28.0")


if __name__ == "__main__":
    unittest.main()
