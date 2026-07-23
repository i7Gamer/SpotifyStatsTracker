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
        # is_skip=0: instance-wide "plays" and listen time mean real plays,
        # matching every per-user stat (skips live in plays as is_skip=1 now).
        plays_count = conn.execute("SELECT COUNT(*) FROM plays WHERE is_skip = 0").fetchone()[0]
        total_time_ms = conn.execute("SELECT SUM(time_played) FROM plays WHERE is_skip = 0").fetchone()[0] or 0

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

    # ---- Typed numeric settings ------------------------------------------------

    def getIntSetting(self, key: str, default: int, minValue: int, maxValue: int) -> int:
        """An app_settings value read as a clamped int - falls back to `default`
        when the row is absent or unparseable, so a code constant stays the
        effective value until an admin overrides it."""
        raw = self.getAppSetting(key)
        if raw is None:
            return default
        try:
            return max(minValue, min(maxValue, int(raw)))
        except (TypeError, ValueError):
            return default

    def setIntSetting(self, key: str, value: int, minValue: int, maxValue: int) -> int:
        """Store a clamped int setting; returns the clamped value actually
        written (so a caller can echo the corrected value back to the admin)."""
        clamped = max(minValue, min(maxValue, int(value)))
        self.setAppSetting(key, str(clamped))
        return clamped

    def getDiscoverArtistLimit(self, default: int) -> int:
        """How many artists the dashboard Discover card shows (live, per request)."""
        return self.getIntSetting(DISCOVER_ARTIST_LIMIT_KEY, default,
                                  DISCOVER_ARTIST_LIMIT_MIN, DISCOVER_ARTIST_LIMIT_MAX)

    def getImageDownloadWorkers(self, default: int) -> int:
        return self.getIntSetting(IMAGE_DOWNLOAD_WORKERS_KEY, default, WORKER_COUNT_MIN, WORKER_COUNT_MAX)

    def getArtistBioFetchWorkers(self, default: int) -> int:
        return self.getIntSetting(ARTIST_BIO_FETCH_WORKERS_KEY, default, WORKER_COUNT_MIN, WORKER_COUNT_MAX)

    def getAlbumBioFetchWorkers(self, default: int) -> int:
        return self.getIntSetting(ALBUM_BIO_FETCH_WORKERS_KEY, default, WORKER_COUNT_MIN, WORKER_COUNT_MAX)

    def getCompletionCompletePercent(self) -> int:
        """Completion pie's complete-vs-partial boundary, as a percent of the
        track's duration (live, per request). See getCompletionStats."""
        return self.getIntSetting(COMPLETION_COMPLETE_PERCENT_KEY, COMPLETION_COMPLETE_PERCENT_DEFAULT,
                                  COMPLETION_COMPLETE_PERCENT_MIN, COMPLETION_COMPLETE_PERCENT_MAX)

    def getBackupIntervalHours(self, default: int) -> int:
        """Hours between automatic DB snapshots (0 disables). `default` is the
        env-or-code fallback, so the setting overrides the env var when set."""
        return self.getIntSetting(BACKUP_INTERVAL_HOURS_KEY, default, BACKUP_INTERVAL_HOURS_MIN, BACKUP_INTERVAL_HOURS_MAX)

    def getBackupRetentionCount(self, default: int) -> int:
        """How many DB snapshots to keep (0 disables). See getBackupIntervalHours."""
        return self.getIntSetting(BACKUP_RETENTION_COUNT_KEY, default, BACKUP_RETENTION_COUNT_MIN, BACKUP_RETENTION_COUNT_MAX)

    def isEmailVerificationEnabled(self) -> bool:
        """Whether login enforces the cookie<->email match (absent = enabled).
        The SKIP_EMAIL_VERIFICATION env var still force-disables regardless."""
        return self._isFeatureEnabled(EMAIL_VERIFICATION_SETTING_KEY)

    def setEmailVerificationEnabled(self, enabled: bool) -> None:
        self._setFeatureEnabled(EMAIL_VERIFICATION_SETTING_KEY, enabled)

    def getGenreBackfillRetryDays(self) -> int:
        return self.getIntSetting(GENRE_BACKFILL_RETRY_DAYS_KEY, GENRE_BACKFILL_RETRY_SECONDS // SECONDS_PER_DAY,
                                  BACKFILL_RETRY_DAYS_MIN, BACKFILL_RETRY_DAYS_MAX)

    def getBioBackfillRetryDays(self) -> int:
        return self.getIntSetting(BIO_BACKFILL_RETRY_DAYS_KEY, BIOGRAPHY_BACKFILL_RETRY_SECONDS // SECONDS_PER_DAY,
                                  BACKFILL_RETRY_DAYS_MIN, BACKFILL_RETRY_DAYS_MAX)

    def getGenreBackfillRetrySeconds(self) -> int:
        """Retry cutoff for the Last.fm genre backfill queue (see getArtistsMissingGenres)."""
        return self.getGenreBackfillRetryDays() * SECONDS_PER_DAY

    def getBioBackfillRetrySeconds(self) -> int:
        """Retry cutoff for the Last.fm biography backfill queue (see getArtistsMissingBiographies)."""
        return self.getBioBackfillRetryDays() * SECONDS_PER_DAY

    # ---- Skip threshold (single source of truth for plays.is_skip) -------------

    @staticmethod
    def _clampSkipValue(mode: str, value: int) -> int:
        lo, hi = ((SKIP_PERCENT_MIN, SKIP_PERCENT_MAX) if mode == SKIP_MODE_PERCENT
                  else (SKIP_SECONDS_MIN, SKIP_SECONDS_MAX))
        return max(lo, min(hi, value))

    def getSkipThreshold(self) -> tuple[str, int]:
        """(mode, value) for the instance-wide skip threshold - defaults to
        (seconds, 5) when unset, and defensively normalizes an out-of-range or
        unparseable stored value."""
        mode = self.getAppSetting(SKIP_THRESHOLD_MODE_KEY, SKIP_THRESHOLD_DEFAULT_MODE)
        if mode not in (SKIP_MODE_SECONDS, SKIP_MODE_PERCENT):
            mode = SKIP_THRESHOLD_DEFAULT_MODE
        raw = self.getAppSetting(SKIP_THRESHOLD_VALUE_KEY)
        try:
            value = int(raw) if raw is not None else SKIP_THRESHOLD_DEFAULT_VALUE
        except (TypeError, ValueError):
            value = SKIP_THRESHOLD_DEFAULT_VALUE
        return mode, self._clampSkipValue(mode, value)

    def setSkipThreshold(self, mode: str, value: int) -> tuple[str, int]:
        """Persist the skip threshold (clamped to the mode's bounds). Does NOT
        recompute existing rows - callers pair this with recomputeSkipFlags()."""
        if mode not in (SKIP_MODE_SECONDS, SKIP_MODE_PERCENT):
            raise ValueError(f"Unknown skip threshold mode: {mode!r}")
        value = self._clampSkipValue(mode, int(value))
        self.setAppSetting(SKIP_THRESHOLD_MODE_KEY, mode)
        self.setAppSetting(SKIP_THRESHOLD_VALUE_KEY, str(value))
        return mode, value

    def computeIsSkip(self, timePlayed: int, durationMs: int | None = None,
                      threshold: tuple[str, int] | None = None) -> int:
        """1 if this play counts as a skip under the current (or supplied)
        threshold, else 0. Percent mode needs the track's duration; an unknown
        (<=0/None) duration falls back to the fixed sub-5s db.SKIP_THRESHOLD_MS
        floor. Pass `threshold` to avoid a per-row settings read in bulk loops."""
        mode, value = threshold if threshold is not None else self.getSkipThreshold()
        if mode == SKIP_MODE_PERCENT:
            if durationMs and durationMs > 0:
                return 1 if timePlayed < durationMs * value / 100 else 0
            return 1 if timePlayed < db.SKIP_THRESHOLD_MS else 0
        return 1 if timePlayed < value * 1000 else 0

    def recomputeSkipFlags(self) -> int:
        """Rewrite plays.is_skip for every row under the current threshold - run
        after the admin changes it. Returns the number of rows processed.
        Self-committing maintenance op (like setAppSetting)."""
        mode, value = self.getSkipThreshold()
        conn = self._conn()
        with conn:
            if mode == SKIP_MODE_PERCENT:
                # Per-row threshold: pct of the track's duration, or the fixed
                # floor for tracks whose duration isn't known (<=0/missing).
                cur = conn.execute(
                    """
                    UPDATE plays SET is_skip = CASE WHEN time_played < COALESCE(
                        (SELECT CASE WHEN t.duration_ms > 0
                                     THEN t.duration_ms * ? / 100.0
                                     ELSE ? END
                         FROM tracks t WHERE t.id = plays.track_id),
                        ?)
                    THEN 1 ELSE 0 END
                    """,
                    (value, db.SKIP_THRESHOLD_MS, db.SKIP_THRESHOLD_MS),
                )
            else:
                cur = conn.execute(
                    "UPDATE plays SET is_skip = CASE WHEN time_played < ? THEN 1 ELSE 0 END",
                    (value * 1000,),
                )
            return cur.rowcount

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

    def isMilestonesEnabled(self) -> bool:
        """Whether the achievement-milestones feature is on instance-wide
        (absent row = enabled). Gates background detection plus the topbar badge
        and the /profile Milestones section - disabling hides them without
        deleting recorded rows, so re-enabling restores the history."""
        return self._isFeatureEnabled(MILESTONES_SETTING_KEY)

    def setMilestonesEnabled(self, enabled: bool) -> None:
        self._setFeatureEnabled(MILESTONES_SETTING_KEY, enabled)

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
