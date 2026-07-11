from __future__ import annotations
import sqlite3
import threading
from pathlib import Path

SQLITE_BUSY_TIMEOUT_MS = 5000   #< how long a writer waits for a lock before raising "database is locked"

# Database/Users/ is the directory the Docker volume mounts for persistence (see
# docker-compose.yml); the shared catalog/history DB and media cache live there
# too now, alongside (eventually replacing) the legacy per-user JSON folders.
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "Users" / "spotify_stats.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS artists (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    image_id    TEXT
);

CREATE TABLE IF NOT EXISTS albums (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    total_tracks    INTEGER NOT NULL DEFAULT 0,
    release_date    REAL,
    image_id        TEXT,
    image_url       TEXT
);

CREATE TABLE IF NOT EXISTS tracks (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    album_id        TEXT NOT NULL REFERENCES albums(id),
    image_id        TEXT,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    explicit        INTEGER NOT NULL DEFAULT 0,
    isrc            TEXT,
    disc_number     INTEGER,
    track_number    INTEGER
);

-- Ordered join table: a track can have multiple artists, and display order matters.
CREATE TABLE IF NOT EXISTS track_artists (
    track_id    TEXT NOT NULL REFERENCES tracks(id),
    artist_id   TEXT NOT NULL REFERENCES artists(id),
    position    INTEGER NOT NULL,
    PRIMARY KEY (track_id, position)
);
CREATE INDEX IF NOT EXISTS idx_track_artists_artist ON track_artists(artist_id);

-- Playlist/album id -> display name cache (global: Spotify playlist/album ids are
-- globally unique, so the same id always resolves to the same name for everyone).
CREATE TABLE IF NOT EXISTS playlists (
    id      TEXT NOT NULL,
    type    TEXT NOT NULL CHECK (type IN ('album', 'playlist')),
    name    TEXT,
    PRIMARY KEY (id, type)
);

-- Tracks which images have already been downloaded to the shared media cache,
-- replacing each user's own img/*/metadata.json dedup set.
CREATE TABLE IF NOT EXISTS images (
    id      TEXT NOT NULL,
    kind    TEXT NOT NULL CHECK (kind IN ('track', 'artist')),
    status  TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ok', 'failed')),
    PRIMARY KEY (id, kind)
);

-- email is nullable: a Database instance can be constructed for maintenance/
-- scripting purposes (e.g. the __main__ smoke test) before the email is known.
-- SQLite treats each NULL as distinct for UNIQUE, so multiple email-less users
-- can coexist without colliding.
CREATE TABLE IF NOT EXISTS users (
    username        TEXT PRIMARY KEY,
    email           TEXT UNIQUE,
    cookies_json    TEXT,
    created_at      REAL NOT NULL
);

-- Per-user play history. This is the only high-cardinality, per-user table -
-- everything else above is shared, global catalog data.
CREATE TABLE IF NOT EXISTS plays (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL REFERENCES users(username),
    track_id        TEXT NOT NULL REFERENCES tracks(id),
    played_at       REAL NOT NULL,
    time_played     INTEGER NOT NULL,
    played_from     TEXT,
    UNIQUE (username, track_id, played_at)
);
CREATE INDEX IF NOT EXISTS idx_plays_user_time ON plays(username, played_at);
CREATE INDEX IF NOT EXISTS idx_plays_user_track ON plays(username, track_id);

CREATE TABLE IF NOT EXISTS import_progress (
    username    TEXT PRIMARY KEY REFERENCES users(username),
    status      TEXT NOT NULL DEFAULT 'idle',
    current     INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT '',
    error       INTEGER NOT NULL DEFAULT 0
);
"""


class ConnectionManager:
    """Owns one SQLite connection per thread for a given database file.

    SQLite connections aren't safe to share across threads without external
    locking; thread-local connections sidestep that without needing a pool,
    since Waitress's worker thread count is small and bounded. WAL mode lets
    those threads read concurrently while a writer holds the lock.
    """

    def __init__(self, dbPath: Path | None = None):
        self.dbPath = Path(dbPath if dbPath is not None else DEFAULT_DB_PATH)
        self._local = threading.local()

    def _newConnection(self) -> sqlite3.Connection:
        self.dbPath.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.dbPath, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)   #< idempotent (IF NOT EXISTS), safe to run per connection
        conn.commit()
        return conn

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._newConnection()
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
