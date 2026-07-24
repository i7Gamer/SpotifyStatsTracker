from __future__ import annotations
import sqlite3
import threading
from pathlib import Path

SQLITE_BUSY_TIMEOUT_MS = 5000   #< how long a writer waits for a lock before raising "database is locked"

# tracks.created_reason value marking catalog rows the importer fabricated because a
# Spotify lookup definitively failed (deleted/unavailable track). The UI badges these
# instead of linking to Spotify, and Repository.upsertTrack() lets later real metadata
# (e.g. a listener fetch of the same id) overwrite the marker.
SYNTHETIC_FALLBACK_REASON = "synthetic_fallback"

# tracks.created_reason value for tracks whose lookup succeeded but came back blanked
# (Spotify returns empty name/duration and a generic "Various Artists" profile for
# region-restricted tracks, playability reason COUNTRY_RESTRICTED). The real track and
# album ids/links are kept, the blanked fields are filled from the user's own export
# data, and the UI shows a "May be unavailable" badge. Like SYNTHETIC_FALLBACK_REASON,
# real metadata arriving later overwrites the marker.
RESTRICTED_FALLBACK_REASON = "restricted_fallback"

# The fixed lower floor (5s) below which an imported/listened event is treated
# as a non-play for DEDUP purposes only: such events bypass the importer's
# near-time play-matching (they must never claim/correct a real play row). This
# is deliberately NOT the (admin-tunable) stats skip threshold - that lives in
# app_settings and is materialized per row into plays.is_skip (see
# SKIP_THRESHOLD_* and computeIsSkip in Database/queries). Kept fixed so moving
# the admin slider never changes how data is recorded/deduplicated; also the
# percent-mode fallback for tracks whose duration is unknown. Shared by the
# importer and the live listener so both dedup identically.
SKIP_THRESHOLD_MS = 5000

# Per-play behavioral metadata from Spotify's extended export, stored as
# nullable columns on both plays and play_skips (NULL = source didn't carry
# it, e.g. listener-recorded plays or pre-1.23.0 imports). ip_addr is
# deliberately never stored. Order matters: SQL builders zip these with values.
BEHAVIORAL_COLUMNS = (
    "platform", "conn_country", "reason_start", "reason_end",
    "shuffle", "skipped", "offline", "incognito",
)

# Database/Data/ is the directory the Docker volume mounts for persistence (see
# docker-compose.yml). Named "Data" rather than "Users" since its main contents
# (this database, the shared media cache) aren't per-user - migrate1_6_0.py
# renames the old Users/ directory here on upgrade.
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "Data" / "spotify_stats.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS artists (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    lastfm_attempted_at REAL,
    image_id    TEXT,
    bio         TEXT,
    bio_attempted_at REAL
);

CREATE TABLE IF NOT EXISTS albums (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    total_tracks    INTEGER NOT NULL DEFAULT 0,
    release_date    REAL,
    image_id        TEXT,
    image_url       TEXT,
    lastfm_attempted_at REAL,
    backfill_attempted_at REAL,
    bio             TEXT,
    bio_attempted_at REAL
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
    track_number    INTEGER,
    created_at      REAL,
    created_reason  TEXT,
    lastfm_attempted_at REAL,
    availability_reason TEXT
);

-- Ordered join table: a track can have multiple artists, and display order matters.
CREATE TABLE IF NOT EXISTS track_artists (
    track_id    TEXT NOT NULL REFERENCES tracks(id),
    artist_id   TEXT NOT NULL REFERENCES artists(id),
    position    INTEGER NOT NULL,
    PRIMARY KEY (track_id, position)
);
CREATE INDEX IF NOT EXISTS idx_track_artists_artist ON track_artists(artist_id);

-- Last.fm genre join tables, ordered like track_artists: position preserves
-- the tag ranking (by count) after whitelist filtering. inherited=1 rows are
-- materialized copies of a closer entity's genres, written only when the
-- entity's own Last.fm lookup came back empty/not-found: tracks inherit
-- their album's own genres first, then (like albums) the PRIMARY artist's -
-- an instance-wide app_settings toggle controls whether they count in stats.
-- Artists have nothing to inherit from, so artist_genres carries no flag.
CREATE TABLE IF NOT EXISTS artist_genres (
    artist_id   TEXT NOT NULL REFERENCES artists(id),
    genre       TEXT NOT NULL,
    position    INTEGER NOT NULL,
    PRIMARY KEY (artist_id, position)
);
CREATE INDEX IF NOT EXISTS idx_artist_genres_genre ON artist_genres(genre);

CREATE TABLE IF NOT EXISTS album_genres (
    album_id    TEXT NOT NULL REFERENCES albums(id),
    genre       TEXT NOT NULL,
    position    INTEGER NOT NULL,
    inherited   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (album_id, position)
);
CREATE INDEX IF NOT EXISTS idx_album_genres_genre ON album_genres(genre);

CREATE TABLE IF NOT EXISTS track_genres (
    track_id    TEXT NOT NULL REFERENCES tracks(id),
    genre       TEXT NOT NULL,
    position    INTEGER NOT NULL,
    inherited   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (track_id, position)
);
CREATE INDEX IF NOT EXISTS idx_track_genres_genre ON track_genres(genre);

-- Instance-wide key/value settings (first consumer: the admin's toggle for
-- counting inherited genres in genre stats and coverage).
CREATE TABLE IF NOT EXISTS app_settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

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
    username              TEXT PRIMARY KEY,
    email                 TEXT UNIQUE,
    cookies_json          TEXT,
    password_hash         TEXT,
    created_at            REAL NOT NULL,
    spotify_client_id     TEXT,
    spotify_client_secret TEXT,
    spotify_refresh_token TEXT,
    lastfm_api_key        TEXT,
    default_dashboard_window TEXT DEFAULT 'day',
    is_admin              INTEGER NOT NULL DEFAULT 0,
    timezone              TEXT,
    spotify_needs_reauth  INTEGER NOT NULL DEFAULT 0,
    milestones_baseline_at REAL
);

-- Per-user play history. This is the only high-cardinality, per-user table -
-- everything else above is shared, global catalog data. Skips live here too
-- (they used to be a separate play_skips table): is_skip=1 marks a sub-threshold
-- event, materialized from the admin-tunable skip threshold at write time and by
-- recomputeSkipFlags() when the threshold changes. Every "real plays" aggregate
-- filters is_skip=0 (a cheap residual on idx_plays_user_time, since skips are a
-- small fraction); skip analytics reads is_skip=1. time_played allows 0 (skips
-- can be 0ms) - it was CHECK >= 1000 while skips were quarantined elsewhere.
-- NOTE: is_skip must not appear in any SCHEMA index - SCHEMA is re-stamped onto
-- pre-1.32.0 databases (whose plays has no is_skip yet) before migrate1_32_0
-- rebuilds the table, and a CREATE INDEX referencing is_skip would fail there.
CREATE TABLE IF NOT EXISTS plays (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL REFERENCES users(username),
    track_id        TEXT NOT NULL REFERENCES tracks(id),
    played_at       REAL NOT NULL,
    time_played     INTEGER NOT NULL CHECK (time_played >= 0),
    played_from     TEXT,
    created_at      REAL,
    created_reason  TEXT,
    platform        TEXT,
    conn_country    TEXT,
    reason_start    TEXT,
    reason_end      TEXT,
    shuffle         INTEGER,
    skipped         INTEGER,
    offline         INTEGER,
    incognito       INTEGER,
    is_skip         INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS user_wrapped (
    username        TEXT NOT NULL REFERENCES users(username),
    year            INTEGER NOT NULL,
    calculated_at   REAL NOT NULL,
    max_played_at   REAL NOT NULL,
    total_plays     INTEGER NOT NULL,
    total_ms        INTEGER NOT NULL,
    longest_streak  INTEGER NOT NULL,
    peak_day        TEXT,
    peak_plays      INTEGER,
    unique_songs    INTEGER NOT NULL,
    unique_artists  INTEGER NOT NULL,
    discovered_songs INTEGER NOT NULL,
    discovered_artists INTEGER NOT NULL,
    time_series_day   TEXT NOT NULL,
    time_series_week  TEXT NOT NULL,
    time_series_month TEXT NOT NULL,
    top_songs        TEXT NOT NULL,
    top_artists      TEXT NOT NULL,
    top_albums       TEXT NOT NULL,
    discovered_songs_list TEXT NOT NULL,
    discovered_artists_list TEXT NOT NULL,
    discovered_albums_list TEXT NOT NULL,
    PRIMARY KEY (username, year)
);

CREATE TABLE IF NOT EXISTS imported_files (
    username    TEXT NOT NULL REFERENCES users(username),
    file_hash   TEXT NOT NULL,
    PRIMARY KEY (username, file_hash)
);

-- Mutual data-sharing: a row starts 'pending' (requester -> recipient), and
-- becomes 'accepted' only once the recipient agrees - at which point access
-- is bidirectional (either side can compare against the other), not just
-- requester-can-view-recipient. Either side can revoke an accepted share;
-- the recipient can decline (or the requester cancel) a pending one. Both
-- of those just delete the row, so re-requesting later starts clean.
CREATE TABLE IF NOT EXISTS user_shares (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_username  TEXT NOT NULL REFERENCES users(username),
    recipient_username  TEXT NOT NULL REFERENCES users(username),
    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted')),
    created_at          REAL NOT NULL,
    responded_at        REAL,
    -- Only the requester side needs an unseen/seen flag for the "your
    -- request was accepted" topbar notification - the recipient's own
    -- accept click is already their acknowledgment. Defaults to 0/unseen at
    -- row creation (while still pending) and is left untouched by the
    -- accept transition, so an accepted share always starts unseen.
    requester_seen_accepted INTEGER NOT NULL DEFAULT 0,
    UNIQUE (requester_username, recipient_username),
    CHECK (requester_username != recipient_username)
);
CREATE INDEX IF NOT EXISTS idx_user_shares_recipient ON user_shares(recipient_username, status);
CREATE INDEX IF NOT EXISTS idx_user_shares_requester ON user_shares(requester_username, status);

-- Public, tokenized read-only links to a user's own Wrapped recap - no login
-- required to view. token is stored in plaintext (not hashed): knowing the
-- token IS the access grant, like a "anyone with the link" URL, so hashing
-- would only protect against DB-read access, which already exposes
-- everything else in this database too. expires_at is nullable ('never'
-- expires); an expired row is lazily deleted on lookup rather than swept by
-- a background job - see Repository.getShareLink(). year is also nullable:
-- NULL means "all years" (a single link that covers every year the owner
-- has data for), not tied to one year at creation like every other row -
-- see migrate1_23_0.py for the migration that relaxed this column.
CREATE TABLE IF NOT EXISTS share_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT NOT NULL UNIQUE,
    username    TEXT NOT NULL REFERENCES users(username),
    kind        TEXT NOT NULL CHECK (kind IN ('wrapped')),
    year        INTEGER,
    created_at  REAL NOT NULL,
    expires_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_share_links_username ON share_links(username);

-- Per-user achievement milestones: lifetime play-count and listen-time
-- thresholds, listening-streak thresholds, and each change of the all-time #1
-- artist. A row is written once its milestone is reached; seen=0 drives the
-- topbar "new milestone" badge until the user opens the Milestones section on
-- /profile (markMilestonesSeen). The user's FIRST detection pass seeds every
-- already-achieved milestone as seen=1 (see users.milestones_baseline_at and
-- services/milestones.py) so shipping this never floods an existing account
-- with notifications for milestones it passed long ago.
-- kind is 'plays' | 'listen_time' | 'streak' | 'top_artist'. threshold holds
-- the numeric level crossed (plays / hours / streak-days); top_artist rows
-- leave it 0 and carry the artist in detail (JSON {"id","name"}).
CREATE TABLE IF NOT EXISTS user_milestones (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT NOT NULL REFERENCES users(username),
    kind         TEXT NOT NULL,
    threshold    INTEGER NOT NULL DEFAULT 0,
    detail       TEXT,
    achieved_at  REAL NOT NULL,
    seen         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_user_milestones_user ON user_milestones(username, seen);
"""


_schemaTemplateLock = threading.Lock()
_schemaTemplateConn: sqlite3.Connection | None = None
_schemaTemplateSchema: str | None = None   #< SCHEMA the cached template was built from

# Tracks, per resolved db file path, which SCHEMA string value has already
# been stamped onto it during this process's lifetime. executescript(SCHEMA)
# is a ~260-line DDL script; even though every CREATE TABLE/INDEX IF NOT
# EXISTS statement in it is a no-op once its target already exists, SQLite
# still takes a write lock to check that - which can collide with a
# concurrent writer thread and raise "database is locked". Re-running it
# once per (process, path) rather than once per thread/ConnectionManager
# instance still preserves the lazy-migration behavior some migrators rely
# on (a table appearing on the next connection without an explicit ALTER -
# see migrate1_12_0/migrate1_14_0) since a fresh process still stamps
# anything new, while skipping the redundant, lock-risking re-runs that
# would otherwise happen for every other thread/Repository touching the
# same file within that process.
_stampedSchemaLock = threading.Lock()
_stampedSchemaByPath: dict[Path, str] = {}


def _getSchemaTemplate() -> sqlite3.Connection:
    """A schema-initialized :memory: connection, cached and reused via
    .backup() by _newConnection() below. executescript(SCHEMA) - a ~260-line
    DDL script - is the dominant cost of opening a brand-new database file
    (measured ~16ms vs ~7ms for the backup() copy); every test that builds
    its own temp-file Repository/Database pays that cost once, so across a
    suite with hundreds of them it adds up. Rebuilds whenever SCHEMA itself
    has changed since the cached copy was made (compared by value, not just
    "already built") - migration tests patch.object(dbModule, "SCHEMA", ...)
    to simulate a pre-migration DB, and a stale cache would silently stamp
    the real current schema onto what's meant to be a legacy one. Lazy +
    lock-guarded since tests construct Repository/Database instances from
    several threads."""
    global _schemaTemplateConn, _schemaTemplateSchema
    if _schemaTemplateConn is None or _schemaTemplateSchema != SCHEMA:
        with _schemaTemplateLock:
            if _schemaTemplateConn is None or _schemaTemplateSchema != SCHEMA:
                # check_same_thread=False: this one cached connection is the
                # backup() SOURCE for every thread that opens a fresh db file,
                # and .backup() is invoked from whichever thread got there -
                # not necessarily the one that built the template. With the
                # default (True), that cross-thread use raises ProgrammingError.
                # Concurrent use of the single source is serialized by
                # _schemaTemplateLock at each backup() call site.
                conn = sqlite3.connect(":memory:", check_same_thread=False)
                conn.executescript(SCHEMA)
                conn.commit()
                _schemaTemplateConn = conn
                _schemaTemplateSchema = SCHEMA
    return _schemaTemplateConn


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
        # Emptiness is checked via this connection's own sqlite_master, not
        # self.dbPath.exists() - Path.exists is patched globally by some test
        # helpers (e.g. _app_factory.py's patch("app.Path.exists", ...),
        # which patches the pathlib.Path class itself, not just app's
        # reference to it) for unrelated reasons, which would make an
        # existing, populated file look "new" and get wiped by backup()
        # below. Querying the connection's actual contents is unaffected by
        # that and is exact rather than a proxy.
        isEmpty = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
        resolvedPath = self.dbPath.resolve()
        if isEmpty:
            # .backup() copies the template's pages wholesale - much cheaper
            # than re-parsing+executing the DDL, but it OVERWRITES the
            # destination, so it's only safe while the file is still empty.
            # Build the template first (its own locking), then serialize the
            # copy under _schemaTemplateLock: that both prevents concurrent use
            # of the single shared source connection AND closes a
            # check-then-backup race - a second thread that saw the file empty
            # a moment ago must re-confirm it is STILL empty before overwriting,
            # or it would wipe rows a first thread already stamped and committed.
            template = _getSchemaTemplate()
            with _schemaTemplateLock:
                stillEmpty = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
                if stillEmpty:
                    template.backup(conn)
                else:
                    conn.executescript(SCHEMA)   #< idempotent (IF NOT EXISTS)
            with _stampedSchemaLock:
                _stampedSchemaByPath[resolvedPath] = SCHEMA
        else:
            with _stampedSchemaLock:
                alreadyStamped = _stampedSchemaByPath.get(resolvedPath) == SCHEMA
            if not alreadyStamped:
                conn.executescript(SCHEMA)   #< idempotent (IF NOT EXISTS); see _stampedSchemaByPath above for why this only runs once per (process, path)
                with _stampedSchemaLock:
                    _stampedSchemaByPath[resolvedPath] = SCHEMA
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
