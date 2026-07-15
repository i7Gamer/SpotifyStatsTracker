import sqlite3
import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
from Database.database import Database


class TestDatabaseCleanup(DatabaseTestCase):
    def test_cleanup_orphans_on_startup(self):
        # Initialize initial database with default user "testuser"
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            # Insert artist, album, track, track_artist, and image which are orphans
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('art_orphan', 'Orphan Artist', '')")
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb_orphan', 'Orphan Album', '')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr_orphan', 'Orphan Track', '', 'alb_orphan')")
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES ('tr_orphan', 'art_orphan', 1)")
            conn.execute("INSERT INTO images (id, kind, status) VALUES ('img_orphan', 'track', 'ok')")

            # Insert artist, album, track, track_artist which have plays and must NOT be deleted
            conn.execute("INSERT INTO artists (id, name, url) VALUES ('art_kept', 'Kept Artist', '')")
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb_kept', 'Kept Album', '')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) VALUES ('tr_kept', 'Kept Track', '', 'alb_kept')")
            conn.execute("INSERT INTO track_artists (track_id, artist_id, position) VALUES ('tr_kept', 'art_kept', 1)")
            conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) VALUES ('testuser', 'tr_kept', 1000.0, 5000)")

        # Verify they are all in the database before startup
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tracks WHERE id='tr_orphan'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tracks WHERE id='tr_kept'").fetchone()[0], 1)

        # Reset class-level cleanup flag to ensure it runs again on this initialization
        Database._cleanup_done = False
        db_new = Database(user="testuser", dbPath=db.repo.connectionManager.dbPath)
        self.addCleanup(db_new.repo.connectionManager.close)

        # Verify orphaned records are deleted
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tracks WHERE id='tr_orphan'").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM track_artists WHERE track_id='tr_orphan'").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM albums WHERE id='alb_orphan'").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM artists WHERE id='art_orphan'").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM images WHERE id='img_orphan'").fetchone()[0], 0)

        # Verify kept records remain intact
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tracks WHERE id='tr_kept'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM track_artists WHERE track_id='tr_kept'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM albums WHERE id='alb_kept'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM artists WHERE id='art_kept'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
