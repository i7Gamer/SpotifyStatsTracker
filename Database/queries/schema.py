from __future__ import annotations

import logging

from Database.queries._base import *  # noqa: F401,F403 - shared constants/db helpers

logger = logging.getLogger(__name__)


class SchemaQueries:
    """SchemaQueries: schema data-access methods, mixed into Repository."""

    def addUserIsAdminColumnIfMissing(self) -> None:
        """SCHEMA's CREATE TABLE IF NOT EXISTS only shapes brand-new databases -
        a users table that already existed before is_admin was added needs an
        explicit ALTER TABLE (migrate1_17_0). Guarded so re-running the
        migration against an already-migrated database doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "is_admin" not in columns:
            with conn:
                conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")

    def addUserPasswordHashColumnIfMissing(self) -> None:
        """SCHEMA's CREATE TABLE IF NOT EXISTS only shapes brand-new databases -
        a users table that already existed before password_hash was added needs
        an explicit ALTER TABLE (migrate1_8_0). Guarded so re-running the
        migration against an already-migrated database doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "password_hash" not in columns:
            with conn:
                conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")

    def addSpotifyApiColumnsToUsersIfMissing(self) -> None:
        """Add Spotify API columns to users table if missing."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        with conn:
            if "spotify_client_id" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN spotify_client_id TEXT")
            if "spotify_client_secret" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN spotify_client_secret TEXT")
            if "spotify_refresh_token" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN spotify_refresh_token TEXT")

    def addTrackMetadataColumnsIfMissing(self) -> None:
        """Add created_at and created_reason columns to tracks table if missing.
        Guarded so re-running the migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}
        with conn:
            if "created_at" not in columns:
                conn.execute("ALTER TABLE tracks ADD COLUMN created_at REAL")
            if "created_reason" not in columns:
                conn.execute("ALTER TABLE tracks ADD COLUMN created_reason TEXT")

    def addPlayMetadataColumnsIfMissing(self) -> None:
        """Add created_at and created_reason columns to plays table if missing.
        Guarded so re-running the migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(plays)").fetchall()}
        with conn:
            if "created_at" not in columns:
                conn.execute("ALTER TABLE plays ADD COLUMN created_at REAL")
            if "created_reason" not in columns:
                conn.execute("ALTER TABLE plays ADD COLUMN created_reason TEXT")

    def addPlayBehavioralColumnsIfMissing(self) -> None:
        """Add the behavioral metadata columns (BEHAVIORAL_COLUMNS) to plays if
        missing (migrate1_22_0). Only the pre-existing plays table needs ALTERs.
        (Historically this migration also relied on SCHEMA creating a separate
        play_skips table; that table was later merged back into plays and
        removed from SCHEMA - see mergePlaySkipsIntoPlays / migrate1_32_0 - so an
        old DB migrating through 1.22.0 no longer gets one, and the merge is a
        no-op for the absent table.) Guarded so re-running doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(plays)").fetchall()}
        columnTypes = {"shuffle": "INTEGER", "skipped": "INTEGER", "offline": "INTEGER", "incognito": "INTEGER"}
        with conn:
            for column in BEHAVIORAL_COLUMNS:
                if column not in columns:
                    conn.execute(f"ALTER TABLE plays ADD COLUMN {column} {columnTypes.get(column, 'TEXT')}")

    # plays table shape as of migrate1_32_0 (is_skip added, time_played CHECK
    # relaxed to >=0). Pinned here rather than derived from SCHEMA because the
    # rebuild below must reproduce this exact shape regardless of how SCHEMA
    # later evolves. Indexes are recreated separately after the RENAME.
    _PLAYS_NEW_TABLE_SQL = """
    CREATE TABLE plays_new (
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
    )
    """

    def mergePlaySkipsIntoPlays(self) -> dict:
        """One-time rebuild for the play_skips -> plays merge (migrate1_32_0):
        add plays.is_skip, relax the time_played CHECK to >= 0, and fold every
        play_skips row back into plays as is_skip=1. Existing plays are seeded
        with is_skip = (time_played < SKIP_THRESHOLD_MS), matching the default
        seconds/5 threshold the migration also seeds. A full table rebuild is
        required because SQLite can't ALTER a CHECK constraint.

        No-op if plays already has is_skip (already migrated, or a fresh SCHEMA
        db). Robust to play_skips being absent (an old DB that migrated through
        1.22.0 after play_skips was removed from SCHEMA). Returns fold-in counts.
        Does NOT rely on the caller's transaction - it toggles foreign_keys and
        commits its own rebuild, mirroring SQLite's standard table-redefinition
        procedure."""
        conn = self._conn()
        playColumns = {row["name"] for row in conn.execute("PRAGMA table_info(plays)").fetchall()}
        if "is_skip" in playColumns:
            return {"plays": 0, "skips": 0, "noop": True}

        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        hasSkips = "play_skips" in tables

        playsBefore = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        skipsBefore = conn.execute("SELECT COUNT(*) FROM play_skips").fetchone()[0] if hasSkips else 0

        conn.commit()   #< settle any pending state so the PRAGMA below is honored (ignored inside a tx)
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with conn:
                conn.execute(self._PLAYS_NEW_TABLE_SQL)
                # Existing plays keep played_from and get a computed is_skip.
                conn.execute(
                    """
                    INSERT INTO plays_new
                        (username, track_id, played_at, time_played, played_from, created_at, created_reason,
                         platform, conn_country, reason_start, reason_end, shuffle, skipped, offline, incognito, is_skip)
                    SELECT username, track_id, played_at, time_played, played_from, created_at, created_reason,
                           platform, conn_country, reason_start, reason_end, shuffle, skipped, offline, incognito,
                           CASE WHEN time_played < ? THEN 1 ELSE 0 END
                    FROM plays
                    """,
                    (db.SKIP_THRESHOLD_MS,),
                )
                skipsFolded = 0
                if hasSkips:
                    # play_skips has no played_from (-> NULL); all rows are skips.
                    # INSERT OR IGNORE so a skip colliding with an existing play on
                    # (username, track_id, played_at) yields to the play.
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO plays_new
                            (username, track_id, played_at, time_played, created_at, created_reason,
                             platform, conn_country, reason_start, reason_end, shuffle, skipped, offline, incognito, is_skip)
                        SELECT username, track_id, played_at, time_played, created_at, created_reason,
                               platform, conn_country, reason_start, reason_end, shuffle, skipped, offline, incognito, 1
                        FROM play_skips
                        """
                    )
                    skipsFolded = cur.rowcount
                    conn.execute("DROP TABLE play_skips")
                conn.execute("DROP TABLE plays")
                conn.execute("ALTER TABLE plays_new RENAME TO plays")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_user_time ON plays(username, played_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_user_track ON plays(username, track_id)")
            # foreign_key_check reports violations as result rows (it never
            # raises), so it must be fetched to mean anything. The rebuild ran
            # with foreign_keys=OFF, so this is exactly where an orphaned
            # plays row (e.g. a play_skips row referencing a since-deleted
            # track) would slip through - surface it loudly rather than keep it
            # silently. Not raised: a dangling track_id FK is invisible in normal
            # queries (JOINs drop it), so it must not brick an otherwise-good
            # one-time upgrade.
            fkViolations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fkViolations:
                logger.error(
                    "mergePlaySkipsIntoPlays: %d foreign-key violation(s) after the plays rebuild: %s",
                    len(fkViolations), fkViolations[:10],
                )
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

        return {"plays": playsBefore, "skips": skipsBefore, "folded": skipsFolded}

    def addUserSettingsColumnsIfMissing(self) -> None:
        """Add default_dashboard_window and timezone columns to users table if missing."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        with conn:
            if "default_dashboard_window" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN default_dashboard_window TEXT DEFAULT 'day'")
            if "timezone" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN timezone TEXT")

    def addAvailabilityColumnsIfMissing(self) -> None:
        """Add tracks.availability_reason (Spotify playability restriction, e.g.
        COUNTRY_RESTRICTED) and albums.backfill_attempted_at (backfill retry
        rate-limiting) if missing. Guarded so re-running doesn't fail."""
        conn = self._conn()
        trackColumns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}
        albumColumns = {row["name"] for row in conn.execute("PRAGMA table_info(albums)").fetchall()}
        with conn:
            if "availability_reason" not in trackColumns:
                conn.execute("ALTER TABLE tracks ADD COLUMN availability_reason TEXT")
            if "backfill_attempted_at" not in albumColumns:
                conn.execute("ALTER TABLE albums ADD COLUMN backfill_attempted_at REAL")

    def addLastfmColumnsIfMissing(self) -> None:
        """Add users.lastfm_api_key and the lastfm_attempted_at queue columns on
        artists/albums/tracks (migrate1_18_0) if missing. The genre join tables
        and app_settings are plain CREATE TABLE IF NOT EXISTS in SCHEMA, so only
        these columns on pre-existing tables need an ALTER. Guarded so re-running
        the migration doesn't fail."""
        conn = self._conn()
        userColumns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        with conn:
            if "lastfm_api_key" not in userColumns:
                conn.execute("ALTER TABLE users ADD COLUMN lastfm_api_key TEXT")
            for table in ("artists", "albums", "tracks"):
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if "lastfm_attempted_at" not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN lastfm_attempted_at REAL")

    def addArtistBioColumnsIfMissing(self) -> None:
        """Add artists.bio and artists.bio_attempted_at (migrate1_25_0) if
        missing. Guarded so re-running the migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(artists)").fetchall()}
        with conn:
            if "bio" not in columns:
                conn.execute("ALTER TABLE artists ADD COLUMN bio TEXT")
            if "bio_attempted_at" not in columns:
                conn.execute("ALTER TABLE artists ADD COLUMN bio_attempted_at REAL")

    def addAlbumBioColumnsIfMissing(self) -> None:
        """Add albums.bio and albums.bio_attempted_at (migrate1_27_0) if
        missing, mirroring addArtistBioColumnsIfMissing for the album-bio
        feature. Guarded so re-running the migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(albums)").fetchall()}
        with conn:
            if "bio" not in columns:
                conn.execute("ALTER TABLE albums ADD COLUMN bio TEXT")
            if "bio_attempted_at" not in columns:
                conn.execute("ALTER TABLE albums ADD COLUMN bio_attempted_at REAL")

    def addRequesterSeenAcceptedColumnIfMissing(self) -> None:
        """Add user_shares.requester_seen_accepted (the "your share request
        was accepted" topbar notification's dismissal flag) if missing.
        Guarded so re-running doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_shares)").fetchall()}
        if "requester_seen_accepted" not in columns:
            with conn:
                conn.execute("ALTER TABLE user_shares ADD COLUMN requester_seen_accepted INTEGER NOT NULL DEFAULT 0")

    def addSpotifyNeedsReauthColumnIfMissing(self) -> None:
        """Add users.spotify_needs_reauth (migrate1_30_0) if missing - flags
        an account whose stored refresh token was rejected by the Web API
        recently-played backfill for lacking the user-read-recently-played
        scope, so Profile can surface "re-authorize with Spotify" instead of
        the listener silently failing every poll. Guarded so re-running the
        migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "spotify_needs_reauth" not in columns:
            with conn:
                conn.execute("ALTER TABLE users ADD COLUMN spotify_needs_reauth INTEGER NOT NULL DEFAULT 0")

    def addMilestonesBaselineColumnIfMissing(self) -> None:
        """Add users.milestones_baseline_at (migrate1_33_0) if missing - the
        timestamp of a user's first milestone-detection pass, which marks
        everything they'd already achieved by then as seen (no notification)
        so the feature shipping doesn't flood existing accounts. The
        user_milestones table itself is a plain CREATE TABLE IF NOT EXISTS in
        SCHEMA (auto-created on the next connect), so only this column on the
        pre-existing users table needs an ALTER. Guarded so re-running the
        migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "milestones_baseline_at" not in columns:
            with conn:
                conn.execute("ALTER TABLE users ADD COLUMN milestones_baseline_at REAL")
