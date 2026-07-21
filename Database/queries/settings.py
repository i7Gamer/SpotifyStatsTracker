from __future__ import annotations

from Database.queries._base import *  # noqa: F401,F403 - shared constants/db helpers
from config import MEDIA_FOLDER_SIZE_CACHE_TTL_SECONDS

# The media cache directory is shared across every user (Database/db.py's
# `images` table dedups downloads instance-wide), so its on-disk size is
# cached at module level - keyed by path, not per-Repository-instance, since
# every Repository points at the same MEDIA_DIR. Recomputing it walks/
# subprocess-scans the whole directory (thousands of files on a real
# instance, ~1s measured), too expensive to pay on every
# getGlobalDatabaseStats() call from the public, unauthenticated /overview
# page.
_folderSizeCacheLock = threading.Lock()
_folderSizeCache: dict[Path, tuple[int, float]] = {}   #< folder_path -> (size_bytes, expiry monotonic ts)


class SettingQueries:
    """SettingQueries: settings data-access methods, mixed into Repository."""

    def _calculateFolderSize(self, folder_path: Path) -> int:
        """Cached (see MEDIA_FOLDER_SIZE_CACHE_TTL_SECONDS above) wrapper
        around _calculateFolderSizeUncached()."""
        now_ts = time.monotonic()
        with _folderSizeCacheLock:
            cached = _folderSizeCache.get(folder_path)
            if cached is not None and cached[1] > now_ts:
                return cached[0]

        size = self._calculateFolderSizeUncached(folder_path)

        with _folderSizeCacheLock:
            _folderSizeCache[folder_path] = (size, now_ts + MEDIA_FOLDER_SIZE_CACHE_TTL_SECONDS)
        return size

    def _calculateFolderSizeUncached(self, folder_path: Path) -> int:
        """Get folder size using OS-level commands (fast on both Windows and Docker)."""
        if not folder_path.exists():
            return 0

        try:
            import subprocess
            import platform

            # Try 'du' first - works on both local Unix and Docker containers
            if platform.system() != "Windows":
                result = subprocess.run(
                    ["du", "-sb", str(folder_path)],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                if result.returncode == 0 and result.stdout.strip():
                    return int(result.stdout.split()[0])

            # Windows fallback (PowerShell)
            if platform.system() == "Windows":
                result = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        f"(Get-ChildItem -Path '{folder_path}' -Recurse -File | Measure-Object -Sum -Property Length).Sum"
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                if result.stdout.strip():
                    return int(result.stdout.strip())
        except Exception:
            pass

        # Fallback to Python recursive method (slow but always works)
        total_size = 0
        try:
            for file in folder_path.rglob("*"):
                if file.is_file():
                    total_size += file.stat().st_size
        except Exception:
            pass
        return total_size

    def getGlobalDatabaseStats(self) -> dict:
        conn = self._conn()
        tracks_count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        artists_count = conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0]
        albums_count = conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
        plays_count = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        total_time_ms = conn.execute("SELECT SUM(time_played) FROM plays").fetchone()[0] or 0

        try:
            db_size = self.connectionManager.dbPath.stat().st_size
        except Exception:
            db_size = 0

        try:
            from Database.database import MEDIA_DIR
            media_size = self._calculateFolderSize(MEDIA_DIR)
        except Exception:
            media_size = 0

        total_storage_bytes = db_size + media_size

        return {
            "tracks": tracks_count,
            "artists": artists_count,
            "albums": albums_count,
            "plays": plays_count,
            "total_time_ms": total_time_ms,
            "db_size_bytes": total_storage_bytes,
        }

    # ---- Instance-wide app settings -------------------------------------------

    def getAppSetting(self, key: str, default: str | None = None) -> str | None:
        row = self._conn().execute(
            "SELECT value FROM app_settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def setAppSetting(self, key: str, value: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                """
                INSERT INTO app_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )

    def isInheritedGenresEnabled(self) -> bool:
        return self.getAppSetting(INHERITED_GENRES_SETTING_KEY, APP_SETTING_TRUE) != APP_SETTING_FALSE

    def setInheritedGenresEnabled(self, enabled: bool) -> None:
        self.setAppSetting(INHERITED_GENRES_SETTING_KEY,
                           APP_SETTING_TRUE if enabled else APP_SETTING_FALSE)

    def _isFeatureEnabled(self, key: str) -> bool:
        return self.getAppSetting(key, APP_SETTING_TRUE) != APP_SETTING_FALSE

    def _setFeatureEnabled(self, key: str, enabled: bool) -> None:
        self.setAppSetting(key, APP_SETTING_TRUE if enabled else APP_SETTING_FALSE)

    def isSpotifyApiBackfillEnabled(self) -> bool:
        return self._isFeatureEnabled(SPOTIFY_BACKFILL_SETTING_KEY)

    def setSpotifyApiBackfillEnabled(self, enabled: bool) -> None:
        self._setFeatureEnabled(SPOTIFY_BACKFILL_SETTING_KEY, enabled)

    def isLastfmGenreBackfillEnabled(self) -> bool:
        return self._isFeatureEnabled(LASTFM_BACKFILL_SETTING_KEY)

    def setLastfmGenreBackfillEnabled(self, enabled: bool) -> None:
        self._setFeatureEnabled(LASTFM_BACKFILL_SETTING_KEY, enabled)

    def isDataSharingEnabled(self) -> bool:
        return self._isFeatureEnabled(DATA_SHARING_SETTING_KEY)

    def setDataSharingEnabled(self, enabled: bool) -> None:
        self._setFeatureEnabled(DATA_SHARING_SETTING_KEY, enabled)

    def isRegistrationEnabled(self) -> bool:
        return self._isFeatureEnabled(REGISTRATION_SETTING_KEY)

    def setRegistrationEnabled(self, enabled: bool) -> None:
        self._setFeatureEnabled(REGISTRATION_SETTING_KEY, enabled)

    def isShareLinksEnabled(self) -> bool:
        return self._isFeatureEnabled(SHARE_LINKS_SETTING_KEY)

    def setShareLinksEnabled(self, enabled: bool) -> None:
        self._setFeatureEnabled(SHARE_LINKS_SETTING_KEY, enabled)

    def isArtistBioEnabled(self) -> bool:
        return self._isFeatureEnabled(ARTIST_BIO_SETTING_KEY)

    def setArtistBioEnabled(self, enabled: bool) -> None:
        self._setFeatureEnabled(ARTIST_BIO_SETTING_KEY, enabled)

    def isAlbumBioEnabled(self) -> bool:
        return self._isFeatureEnabled(ALBUM_BIO_SETTING_KEY)

    def setAlbumBioEnabled(self, enabled: bool) -> None:
        self._setFeatureEnabled(ALBUM_BIO_SETTING_KEY, enabled)

    def getRecentRegistrationCounts(self) -> dict:
        """How many accounts were created in the last 7/30 days - an admin
        activity signal with no per-user equivalent."""
        now = time.time()
        conn = self._conn()
        last7 = conn.execute(
            "SELECT COUNT(*) FROM users WHERE created_at >= ?", (now - 7 * 24 * 3600,)
        ).fetchone()[0]
        last30 = conn.execute(
            "SELECT COUNT(*) FROM users WHERE created_at >= ?", (now - 30 * 24 * 3600,)
        ).fetchone()[0]
        return {"last_7_days": last7, "last_30_days": last30}

    def getInstanceShareCounts(self) -> dict:
        """{"pending", "accepted"} counts across every user_shares row in the
        instance - the admin-page equivalent of getPendingIncomingSharesCount/
        hasAnyAcceptedShare, which are both scoped to a single username."""
        conn = self._conn()
        rows = conn.execute("SELECT status, COUNT(*) AS c FROM user_shares GROUP BY status").fetchall()
        counts = {"pending": 0, "accepted": 0}
        for row in rows:
            if row["status"] in counts:
                counts[row["status"]] = row["c"]
        return counts

    def getActiveShareLinksCount(self) -> int:
        """How many public Wrapped share links are currently live (not
        expired) across every user. Lazily deletes expired rows first, same
        pattern as getShareLink/getShareLinksForUser."""
        conn = self._conn()
        now = time.time()
        with conn:
            conn.execute("DELETE FROM share_links WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
        row = conn.execute(
            "SELECT COUNT(*) FROM share_links WHERE expires_at IS NULL OR expires_at >= ?", (now,)
        ).fetchone()
        return row[0]
