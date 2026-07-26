from __future__ import annotations
import time

from Database.queries._base import *  # noqa: F401,F403 - shared constants/db helpers
from config import (
    TREND_OBSESSION_DAYS,
    TREND_OBSESSION_MIN_PLAYS,
    TREND_OBSESSION_FALLBACK_MIN_PLAYS,
    TREND_REDISCOVERY_RECENT_DAYS,
    TREND_REDISCOVERY_GAP_DAYS,
    TREND_REDISCOVERY_MIN_HISTORICAL_PLAYS,
    TREND_FORGOTTEN_GAP_DAYS,
    TREND_FORGOTTEN_MIN_HISTORICAL_PLAYS,
    TREND_FORGOTTEN_FALLBACK_MIN_PLAYS,
)

SECONDS_PER_DAY = 86400


class TrendQueries:
    """TrendQueries: SQL queries for Dashboard Obsession, Rediscovery, and Forgotten Favorites.

    All three exclude skips (plays.is_skip=1): a track you keep skipping is not
    an obsession, a rediscovery, or a forgotten favorite. Forgotten Favorite
    additionally requires each counted play to be a full listen (completion
    ratio) - see getDashboardTrendsRaw."""

    def getDashboardTrendsRaw(self, username: str, now_ts: float | None = None) -> dict[str, dict | None]:
        if now_ts is None:
            now_ts = time.time()

        conn = self._conn()

        # 1. Obsession
        obsession_cutoff = now_ts - (TREND_OBSESSION_DAYS * SECONDS_PER_DAY)
        obsession_row = conn.execute(
            """
            SELECT track_id, COUNT(*) as recent_count, SUM(time_played) as recent_ms
            FROM plays
            WHERE username = ? AND is_skip = 0 AND played_at >= ?
            GROUP BY track_id
            HAVING recent_count >= ?
            ORDER BY recent_count DESC, recent_ms DESC
            LIMIT 1
            """,
            (username, obsession_cutoff, TREND_OBSESSION_MIN_PLAYS),
        ).fetchone()

        # Fallback for obsession if user has < TREND_OBSESSION_MIN_PLAYS but plays exist
        if not obsession_row:
            obsession_row = conn.execute(
                """
                SELECT track_id, COUNT(*) as recent_count, SUM(time_played) as recent_ms
                FROM plays
                WHERE username = ? AND is_skip = 0 AND played_at >= ?
                GROUP BY track_id
                HAVING recent_count >= ?
                ORDER BY recent_count DESC, recent_ms DESC
                LIMIT 1
                """,
                (username, obsession_cutoff, TREND_OBSESSION_FALLBACK_MIN_PLAYS),
            ).fetchone()

        # 2. Rediscovery
        rediscovery_recent_cutoff = now_ts - (TREND_REDISCOVERY_RECENT_DAYS * SECONDS_PER_DAY)
        rediscovery_gap_cutoff = now_ts - (TREND_REDISCOVERY_GAP_DAYS * SECONDS_PER_DAY)
        rediscovery_row = conn.execute(
            """
            SELECT track_id,
                   COUNT(CASE WHEN played_at >= ? THEN 1 END) as recent_count,
                   COUNT(CASE WHEN played_at < ? THEN 1 END) as old_count,
                   MAX(CASE WHEN played_at < ? THEN played_at END) as max_old_played_at
            FROM plays
            WHERE username = ? AND is_skip = 0
              -- Only tracks with a recent real play can survive the
              -- `recent_count >= 1` below, so aggregating any others is wasted
              -- work. Restricting the candidates first turns a GROUP BY over
              -- the user's whole history into one over the handful of tracks
              -- they have actually played lately (~180ms -> ~0.5ms on a real
              -- library, on the landing page). The subquery repeats the
              -- is_skip = 0 filter deliberately: a track whose only recent
              -- plays are skips has recent_count 0 and must stay excluded, the
              -- same as before (see test_rediscovery_excludes_skips).
              AND track_id IN (
                  SELECT track_id FROM plays
                  WHERE username = ? AND is_skip = 0 AND played_at >= ?
              )
            GROUP BY track_id
            HAVING recent_count >= 1
               AND old_count >= ?
               AND max_old_played_at IS NOT NULL
               AND max_old_played_at <= ?
            ORDER BY recent_count DESC, old_count DESC
            LIMIT 1
            """,
            (
                rediscovery_recent_cutoff,
                rediscovery_recent_cutoff,
                rediscovery_recent_cutoff,
                username,
                username,
                rediscovery_recent_cutoff,
                TREND_REDISCOVERY_MIN_HISTORICAL_PLAYS,
                rediscovery_gap_cutoff,
            ),
        ).fetchone()

        # 3. Forgotten Favorite - only counts full listens (is_skip=0 and at/over
        # the admin's completion-complete percent, same boundary getCompletionStats
        # uses), so a track that was merely started/skipped a lot never reads as a
        # forgotten favorite - a favorite has to have been actually heard.
        forgotten_gap_cutoff = now_ts - (TREND_FORGOTTEN_GAP_DAYS * SECONDS_PER_DAY)
        completion_ratio = self.getCompletionCompletePercent() / 100.0
        # One statement, run twice: the fallback below differs only in how many
        # historical plays it demands. The completion test is the shared
        # FULL_PLAY_PREDICATE rather than its own copy of the same SQL, so a
        # change to what "a full listen" means reaches here too.
        forgottenQuery = f"""
            SELECT p.track_id as track_id, COUNT(*) as total_plays, MAX(p.played_at) as last_played_at
            FROM plays p
            JOIN tracks t ON p.track_id = t.id
            WHERE p.username = ?
              AND p.is_skip = 0
              AND ({FULL_PLAY_PREDICATE.format(plays="p", track="t")})
            GROUP BY p.track_id
            HAVING last_played_at <= ? AND total_plays >= ?
            ORDER BY total_plays DESC, last_played_at DESC
            LIMIT 1
            """

        def forgottenWithAtLeast(minPlays: int):
            return conn.execute(
                forgottenQuery,
                (username, completion_ratio, forgotten_gap_cutoff, minPlays),
            ).fetchone()

        forgotten_row = forgottenWithAtLeast(TREND_FORGOTTEN_MIN_HISTORICAL_PLAYS)
        # Fallback for forgotten if no track hits high threshold
        if not forgotten_row:
            forgotten_row = forgottenWithAtLeast(TREND_FORGOTTEN_FALLBACK_MIN_PLAYS)

        return {
            "obsession": dict(obsession_row) if obsession_row else None,
            "rediscovery": dict(rediscovery_row) if rediscovery_row else None,
            "forgotten": dict(forgotten_row) if forgotten_row else None,
        }
