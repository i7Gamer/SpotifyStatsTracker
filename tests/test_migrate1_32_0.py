"""1.32.0 -> 1.33.0: merge play_skips back into plays.

Rebuilds plays (adds is_skip, relaxes the time_played CHECK from >=1000 to >=0),
folds every play_skips row in as is_skip=1, drops play_skips, and seeds the
instance-wide skip threshold at its default (seconds/5). Existing plays are
classified is_skip = (time_played < 5000). Row counts must be conserved.
"""
import sqlite3
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
from Database.repository import SKIP_THRESHOLD_MODE_KEY, SKIP_THRESHOLD_VALUE_KEY

# The plays / play_skips tables exactly as they existed at 1.32.0 (behavioral
# columns present, plays CHECK >= 1000, no is_skip). The migration must rebuild
# this shape, not rely on SCHEMA (CREATE TABLE IF NOT EXISTS is a no-op here).
_BEHAVIORAL = "platform TEXT, conn_country TEXT, reason_start TEXT, reason_end TEXT, shuffle INTEGER, skipped INTEGER, offline INTEGER, incognito INTEGER"

OLD_PLAYS_TABLE_SQL = f"""
CREATE TABLE plays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    track_id TEXT NOT NULL,
    played_at REAL NOT NULL,
    time_played INTEGER NOT NULL CHECK (time_played >= 1000),
    played_from TEXT,
    created_at REAL,
    created_reason TEXT,
    {_BEHAVIORAL},
    UNIQUE (username, track_id, played_at)
)
"""

OLD_PLAY_SKIPS_TABLE_SQL = f"""
CREATE TABLE play_skips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    track_id TEXT NOT NULL,
    played_at REAL NOT NULL,
    time_played INTEGER NOT NULL CHECK (time_played >= 0),
    created_at REAL,
    created_reason TEXT,
    {_BEHAVIORAL},
    UNIQUE (username, track_id, played_at)
)
"""


class TestMigrate1_32_0(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.33.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.32.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"
        self._seedOldShapeDb()

    def _seedOldShapeDb(self):
        conn = sqlite3.connect(self.dbPath)   #< raw connect: FK enforcement off, so dangling u1/t1 refs are fine
        with conn:
            conn.execute(OLD_PLAYS_TABLE_SQL)
            conn.execute(OLD_PLAY_SKIPS_TABLE_SQL)
            # Two real plays (>=5s) and one former play that is now a skip (<5s).
            conn.execute("INSERT INTO plays (username, track_id, played_at, time_played, played_from, platform) "
                         "VALUES ('u1', 't1', 1000.0, 60000, 'album:al1', 'ios')")
            conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) VALUES ('u1', 't2', 2000.0, 40000)")
            conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) VALUES ('u1', 't3', 3000.0, 3000)")
            # Two skips (one 0ms - only valid because play_skips allowed >=0).
            conn.execute("INSERT INTO play_skips (username, track_id, played_at, time_played, reason_end) "
                         "VALUES ('u1', 't4', 4000.0, 400, 'fwdbtn')")
            conn.execute("INSERT INTO play_skips (username, track_id, played_at, time_played) VALUES ('u1', 't5', 5000.0, 0)")
        conn.close()

    def _migrate(self):
        import Database.Migrators.migrate1_32_0 as migrateModule
        migrateModule.Migrator("1.32.0", "1.33.0").migrate()

    def _tables(self, conn):
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    def _columns(self, conn, table):
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def test_merges_skips_into_plays_and_drops_table(self):
        self._migrate()

        conn = sqlite3.connect(self.dbPath)
        try:
            self.assertNotIn("play_skips", self._tables(conn))
            self.assertIn("is_skip", self._columns(conn, "plays"))

            # Counts conserved: 3 plays + 2 skips = 5 rows.
            total = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
            self.assertEqual(total, 5)

            # is_skip: the two >=5s plays are real; the 3000ms play and both
            # former skips are is_skip=1.
            byTrack = {r[0]: r[1] for r in conn.execute("SELECT track_id, is_skip FROM plays").fetchall()}
            self.assertEqual(byTrack, {"t1": 0, "t2": 0, "t3": 1, "t4": 1, "t5": 1})

            # Behavioral + played_from carry over; former skips have NULL played_from.
            t1 = conn.execute("SELECT played_from, platform FROM plays WHERE track_id='t1'").fetchone()
            self.assertEqual(t1, ("album:al1", "ios"))
            t4 = conn.execute("SELECT played_from, reason_end FROM plays WHERE track_id='t4'").fetchone()
            self.assertEqual(t4, (None, "fwdbtn"))

            # CHECK relaxed to >= 0: a 0ms row is now insertable into plays.
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("INSERT INTO plays (username, track_id, played_at, time_played, is_skip) VALUES ('u1', 't6', 6000.0, 0, 1)")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) VALUES ('u1', 't7', 7000.0, -1)")

            # Threshold seeded at its default.
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (SKIP_THRESHOLD_MODE_KEY,)).fetchone()
            self.assertEqual(row[0], "seconds")
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (SKIP_THRESHOLD_VALUE_KEY,)).fetchone()
            self.assertEqual(row[0], "5")
        finally:
            conn.close()

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.33.0")

    def test_noop_when_no_play_skips_table(self):
        # An old DB that migrated through 1.22.0 after play_skips was retired has
        # no play_skips table - the merge must still add is_skip and relax the CHECK.
        conn = sqlite3.connect(self.dbPath)
        with conn:
            conn.execute("DROP TABLE play_skips")
        conn.close()

        self._migrate()

        conn = sqlite3.connect(self.dbPath)
        try:
            self.assertIn("is_skip", self._columns(conn, "plays"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0], 3)
        finally:
            conn.close()

    def test_seeds_new_instance_settings(self):
        # Clean env so backup/email seeds land on their defaults.
        with patch.dict(os.environ, {}, clear=False):
            for key in ("BACKUP_INTERVAL_HOURS", "BACKUP_RETENTION_COUNT", "SKIP_EMAIL_VERIFICATION"):
                os.environ.pop(key, None)
            self._migrate()

        conn = sqlite3.connect(self.dbPath)
        try:
            settings = dict(conn.execute("SELECT key, value FROM app_settings").fetchall())
        finally:
            conn.close()

        self.assertEqual(settings.get("skip_threshold_mode"), "seconds")
        self.assertEqual(settings.get("skip_threshold_value"), "5")
        self.assertEqual(settings.get("completion_complete_percent"), "80")
        self.assertEqual(settings.get("genre_backfill_retry_days"), "30")
        self.assertEqual(settings.get("bio_backfill_retry_days"), "30")
        self.assertEqual(settings.get("backup_interval_hours"), "24")
        self.assertEqual(settings.get("backup_retention_count"), "7")
        self.assertEqual(settings.get("email_verification_enabled"), "1")

    def test_seeding_does_not_clobber_existing_values(self):
        # A prior admin choice (row already present) must survive the migration.
        conn = sqlite3.connect(self.dbPath)
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO app_settings (key, value) VALUES ('completion_complete_percent', '95')")
        conn.close()

        self._migrate()

        conn = sqlite3.connect(self.dbPath)
        try:
            value = conn.execute(
                "SELECT value FROM app_settings WHERE key='completion_complete_percent'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(value, "95")   #< not reset to the default 80


if __name__ == "__main__":
    unittest.main()
