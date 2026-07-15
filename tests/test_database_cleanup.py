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

        # Verify orphaned records are NOT deleted (feature removed)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tracks WHERE id='tr_orphan'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM track_artists WHERE track_id='tr_orphan'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM albums WHERE id='alb_orphan'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM artists WHERE id='art_orphan'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM images WHERE id='img_orphan'").fetchone()[0], 1)

        # Verify kept records remain intact
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tracks WHERE id='tr_kept'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM track_artists WHERE track_id='tr_kept'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM albums WHERE id='alb_kept'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM artists WHERE id='art_kept'").fetchone()[0], 1)

    def test_synthetic_track_preserves_created_reason(self):
        db = self._makeDb({}, [])
        
        # Staged synthetic track
        synthetic_track = {
            "name": "Arctic Future",
            "releaseDate": 0.0,
            "id": "uri_2s9mjCqeU26eivqPXY04V8",
            "url": "https://open.spotify.com/track/uri_2s9mjCqeU26eivqPXY04V8",
            "artists": [
                {
                    "name": "Mark Watson",
                    "url": "https://open.spotify.com/artist/art_synthetic",
                    "imageUrl": "",
                    "imageId": "art_synthetic",
                    "id": "art_synthetic",
                }
            ],
            "album": {
                "name": "Arctic Future",
                "url": "https://open.spotify.com/album/alb_synthetic",
                "id": "alb_synthetic",
                "imageId": "alb_synthetic",
                "imageUrl": "",
                "totalTracks": 1,
                "releaseDate": 0.0,
            },
            "imageUrl": "",
            "imageId": "alb_synthetic",
            "duration": 10354,
            "explicit": False,
            "isrc": "",
            "discNumber": 1,
            "trackNumber": 1,
            "created_reason": "synthetic_fallback",
        }
        
        db.repo.upsertTrack(synthetic_track, created_reason="history_import (user: testuser)")
        
        # Verify it is stored as "synthetic_fallback" in the DB, not overridden by "history_import"
        db_track = db.repo.getTrack("uri_2s9mjCqeU26eivqPXY04V8")
        self.assertIsNotNone(db_track)
        self.assertEqual(db_track["created_reason"], "synthetic_fallback")


if __name__ == "__main__":
    unittest.main()
