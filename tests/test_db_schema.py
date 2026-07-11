import unittest
import sqlite3
import sys
import os
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.db import ConnectionManager

EXPECTED_TABLES = {
    "artists", "albums", "tracks", "track_artists", "playlists",
    "images", "users", "plays", "import_progress",
}


class TestConnectionManagerSchema(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.dbPath = Path(self._tmpdir.name) / "test.db"
        self.manager = ConnectionManager(self.dbPath)

    def tearDown(self):
        self.manager.close()
        self._tmpdir.cleanup()

    def test_creates_db_file_and_parent_dirs(self):
        nestedPath = Path(self._tmpdir.name) / "nested" / "dir" / "test.db"
        manager = ConnectionManager(nestedPath)
        manager.connection()
        self.assertTrue(nestedPath.exists())
        manager.close()

    def test_all_expected_tables_exist(self):
        conn = self.manager.connection()
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tableNames = {row["name"] for row in rows}
        self.assertTrue(EXPECTED_TABLES.issubset(tableNames))

    def test_wal_mode_enabled(self):
        conn = self.manager.connection()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_foreign_keys_enforced(self):
        conn = self.manager.connection()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tracks (id, name, url, album_id) VALUES (?, ?, ?, ?)",
                ("t1", "Song", "http://example.com", "missing-album"),
            )

    def test_plays_unique_constraint_blocks_exact_duplicate(self):
        conn = self.manager.connection()
        conn.execute("INSERT INTO users (username, email, created_at) VALUES (?, ?, ?)",
                     ("alice", "alice@example.com", 0))
        conn.execute("INSERT INTO albums (id, name, url) VALUES (?, ?, ?)",
                     ("alb1", "Album", "http://example.com"))
        conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES (?, ?, ?, ?)",
                     ("t1", "Song", "http://example.com", "alb1"))
        conn.execute(
            "INSERT INTO plays (username, track_id, played_at, time_played) VALUES (?, ?, ?, ?)",
            ("alice", "t1", 1000.0, 5000),
        )
        conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO plays (username, track_id, played_at, time_played) VALUES (?, ?, ?, ?)",
                ("alice", "t1", 1000.0, 5000),
            )

    def test_same_thread_reuses_connection(self):
        first = self.manager.connection()
        second = self.manager.connection()
        self.assertIs(first, second)

    def test_different_threads_share_underlying_data(self):
        conn = self.manager.connection()
        conn.execute("INSERT INTO users (username, email, created_at) VALUES (?, ?, ?)",
                     ("bob", "bob@example.com", 0))
        conn.commit()

        resultHolder = {}

        def readFromOtherThread():
            threadConn = self.manager.connection()
            row = threadConn.execute("SELECT email FROM users WHERE username=?", ("bob",)).fetchone()
            resultHolder["email"] = row["email"] if row else None
            self.manager.close()

        thread = threading.Thread(target=readFromOtherThread)
        thread.start()
        thread.join()

        self.assertEqual(resultHolder["email"], "bob@example.com")


if __name__ == "__main__":
    unittest.main()
