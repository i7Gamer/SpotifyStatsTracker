from __future__ import annotations

from Database.queries._base import *  # noqa: F401,F403 - shared constants/db helpers


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
        missing (migrate1_22_0). play_skips is a plain CREATE TABLE IF NOT
        EXISTS in SCHEMA, so only the pre-existing plays table needs ALTERs.
        Guarded so re-running the migration doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(plays)").fetchall()}
        columnTypes = {"shuffle": "INTEGER", "skipped": "INTEGER", "offline": "INTEGER", "incognito": "INTEGER"}
        with conn:
            for column in BEHAVIORAL_COLUMNS:
                if column not in columns:
                    conn.execute(f"ALTER TABLE plays ADD COLUMN {column} {columnTypes.get(column, 'TEXT')}")

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
