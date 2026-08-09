# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import logging

from Database.queries._base import *  # noqa: F401,F403 - shared constants/db helpers
from config import MEDIA_FOLDER_SIZE_CACHE_TTL_SECONDS

logger = logging.getLogger(__name__)

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

            # Windows fallback (PowerShell). The path is passed via an env var and
            # read with -LiteralPath rather than interpolated into the command
            # string: interpolation would both break on a path containing a quote
            # or a `[` (a -Path wildcard char) and be an injection vector the day
            # the folder path ever became anything but a fixed config value.
            if platform.system() == "Windows":
                import os
                result = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        "(Get-ChildItem -LiteralPath $env:SPOTIFY_MEDIA_SCAN_DIR -Recurse -File "
                        "| Measure-Object -Sum -Property Length).Sum",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env={**os.environ, "SPOTIFY_MEDIA_SCAN_DIR": str(folder_path)},
                )
                if result.stdout.strip():
                    return int(result.stdout.strip())
        except Exception:  # noqa: S110 - the platform scan is the fast path only; the Python
            pass           #  walk below is the answer, and it logs if IT fails

        # Fallback to Python recursive method (slow but always works)
        total_size = 0
        try:
            for file in folder_path.rglob("*"):
                if file.is_file():
                    total_size += file.stat().st_size
        except Exception as e:
            # Returning the partial total is right - a half-walked directory is
            # a better answer than none for a display figure - but silently
            # reporting a wrong media size on /overview gave no way to tell
            # that from a genuinely small cache.
            logger.debug("Media folder size scan failed for %s, reporting partial total: %s", folder_path, e)
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
                      threshold: tuple[str, int] | None = None,
                      completionPercent: int | None = None) -> int:
        """1 if this play counts as a skip under the current (or supplied)
        threshold, else 0. Pass `threshold`/`completionPercent` to avoid the
        per-row settings reads in bulk loops.

        Whatever the threshold says, a play that reached the completion percent
        of the track's duration (getCompletionCompletePercent) is never a skip.
        The two settings are independent, so nothing stopped them contradicting
        each other: the same play could be counted "complete" by the Charts
        completion pie and "abandoned" by the skip rate, on the same page. The
        classifier therefore caps its threshold at the completion boundary,
        which is the widest cap that cannot turn a genuine skip into a play:
          - seconds mode, on a track shorter than the threshold. The threshold
            is unreachable there, so every play of it was a skip - including
            ones that ran to the last millisecond, with no listening behaviour
            able to change that. Real case: a 22.174s intro under a 30s
            threshold reported a 100% skip rate while Spotify's own export
            recorded every play as skipped=false / trackdone.
          - percent mode, whenever the skip percent is set above the completion
            percent (skip 90% vs complete 80% makes a play at 85% both).

        A track whose duration is unknown (<=0/None) has no completion boundary
        to respect, and keeps the documented fallbacks: the fixed sub-5s
        db.SKIP_THRESHOLD_MS floor in percent mode (which needs a duration at
        all), the plain threshold in seconds mode."""
        mode, value = threshold if threshold is not None else self.getSkipThreshold()
        if not durationMs or durationMs <= 0:
            floorMs = db.SKIP_THRESHOLD_MS if mode == SKIP_MODE_PERCENT else value * MS_PER_SECOND
            return 1 if timePlayed < floorMs else 0

        if completionPercent is None:
            completionPercent = self.getCompletionCompletePercent()
        if mode == SKIP_MODE_PERCENT:
            thresholdMs = durationMs * min(value, completionPercent) / PERCENT_DIVISOR
        else:
            thresholdMs = min(value * MS_PER_SECOND,
                              durationMs * completionPercent / PERCENT_DIVISOR)
        return 1 if timePlayed < thresholdMs else 0

    def recomputeSkipFlags(self) -> int:
        """Rewrite plays.is_skip for every row under the current threshold - run
        after the admin changes it. Returns the number of rows processed.
        Self-committing maintenance op (like setAppSetting).

        The bulk rewrite classifies in SQL instead of calling computeIsSkip per
        row, so the rule lives twice; both copies cap at the completion
        boundary, and tests/test_skip_settings.py pins them against each other
        row by row."""
        mode, value = self.getSkipThreshold()
        completionPercent = self.getCompletionCompletePercent()
        conn = self._conn()
        with conn:
            if mode == SKIP_MODE_PERCENT:
                # Per-row threshold: pct of the track's duration, or the fixed
                # floor for tracks whose duration isn't known (<=0/missing).
                # Capping the percent itself is enough here - both boundaries
                # are the same fraction of the same duration.
                cur = conn.execute(
                    f"""
                    UPDATE plays SET is_skip = CASE WHEN time_played < COALESCE(
                        (SELECT CASE WHEN t.duration_ms > 0
                                     THEN t.duration_ms * ? / {PERCENT_DIVISOR}
                                     ELSE ? END
                         FROM tracks t WHERE t.id = plays.track_id),
                        ?)
                    THEN 1 ELSE 0 END
                    """,
                    (min(value, completionPercent), db.SKIP_THRESHOLD_MS, db.SKIP_THRESHOLD_MS),
                )
            else:
                # Same completion cap as computeIsSkip's seconds mode, per row:
                # the threshold or the track's completion boundary, whichever
                # comes first, so a play the completion pie calls complete is
                # never stored as a skip. Tracks with an unknown duration
                # (<=0/missing, or no tracks row) keep the plain threshold via
                # COALESCE.
                thresholdMs = value * MS_PER_SECOND
                cur = conn.execute(
                    f"""
                    UPDATE plays SET is_skip = CASE WHEN time_played < COALESCE(
                        (SELECT CASE WHEN t.duration_ms > 0
                                     THEN MIN(?, t.duration_ms * ? / {PERCENT_DIVISOR})
                                     ELSE ? END
                         FROM tracks t WHERE t.id = plays.track_id),
                        ?)
                    THEN 1 ELSE 0 END
                    """,
                    (thresholdMs, completionPercent, thresholdMs, thresholdMs),
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

    def isPushListenerEnabled(self) -> bool:
        """Whether listeners take player state from Spotify's pushed
        connect-state instead of polling for it.

        The only feature key that defaults to OFF (absent row = disabled) - see
        PUSH_LISTENER_SETTING_KEY for why. Read once per listener build, so a
        flip takes effect when the listener is next rebuilt rather than
        mid-stream."""
        return self.getAppSetting(PUSH_LISTENER_SETTING_KEY, APP_SETTING_FALSE) == APP_SETTING_TRUE

    def setPushListenerEnabled(self, enabled: bool) -> None:
        self._setFeatureEnabled(PUSH_LISTENER_SETTING_KEY, enabled)

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
        and the dashboard's Milestones / Next milestones cards - disabling hides
        them without deleting recorded rows, so re-enabling restores the
        history."""
        return self._isFeatureEnabled(MILESTONES_SETTING_KEY)

    def isTrackMergeEnabled(self) -> bool:
        """Whether duplicate tracks (same recording, released more than once)
        are merged in the global stats.

        Defaults OFF like PUSH_LISTENER_SETTING_KEY, and unlike every other
        feature toggle: a merge moves every account's numbers at once, so it has
        to be a decision someone made, not a default they inherited.

        The toggle gates the DATA rather than the queries. Turning it on runs
        the ISRC matcher (and the backfiller keeps re-running it as new ISRCs
        arrive); turning it off unmerges everything the matcher did, manual
        verdicts excepted. The read paths honour canonical_id unconditionally -
        which is safe precisely because it is only ever set while this is on,
        and it is what makes switching off a genuine, instant, lossless undo
        rather than a filter."""
        return self.getAppSetting(TRACK_MERGE_SETTING_KEY, APP_SETTING_FALSE) == APP_SETTING_TRUE

    def setTrackMergeEnabled(self, enabled: bool) -> None:
        self._setFeatureEnabled(TRACK_MERGE_SETTING_KEY, enabled)

    def setMilestonesEnabled(self, enabled: bool) -> None:
        self._setFeatureEnabled(MILESTONES_SETTING_KEY, enabled)

    def isMilestoneRecalcEnabled(self) -> bool:
        """Whether the periodic milestone pass treats imported history as the
        source of truth - re-deriving achieved_at dates from play history
        after an import (or any pass that recorded rows), recording
        import-surfaced crossings as already seen instead of flooding the
        topbar badge, and pruning milestones a shrinking overwrite import's
        rewritten history no longer supports - see _detectMilestonesSafely in
        app.py. Absent row = enabled. Separate from isMilestonesEnabled so an
        admin can keep milestones on but restore the pre-1.36.0 recording
        behavior."""
        return self._isFeatureEnabled(MILESTONE_RECALC_SETTING_KEY)

    def setMilestoneRecalcEnabled(self, enabled: bool) -> None:
        self._setFeatureEnabled(MILESTONE_RECALC_SETTING_KEY, enabled)

    def isTagsEnabled(self) -> bool:
        """Whether the personal tagging system is on instance-wide (absent
        row = enabled). Gates the tag panel on song/artist/album pages, the
        tag filter on Top Songs/Artists/Albums, and the Playlists page/nav
        link - disabling hides them without deleting recorded user_tags
        rows, so re-enabling restores everything."""
        return self._isFeatureEnabled(TAGS_SETTING_KEY)

    def setTagsEnabled(self, enabled: bool) -> None:
        self._setFeatureEnabled(TAGS_SETTING_KEY, enabled)

    def isFriendsNowPlayingEnabled(self) -> bool:
        """Whether the dashboard shows what people you share with are playing
        right now (absent row = enabled). Deliberately separate from
        isDataSharingEnabled: live presence is a stronger disclosure than the
        aggregate comparison sharing already allows, so an admin can keep
        Compare while turning this off. Both must be on for the strip to
        appear."""
        return self._isFeatureEnabled(FRIENDS_NOW_PLAYING_SETTING_KEY)

    def setFriendsNowPlayingEnabled(self, enabled: bool) -> None:
        self._setFeatureEnabled(FRIENDS_NOW_PLAYING_SETTING_KEY, enabled)

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
