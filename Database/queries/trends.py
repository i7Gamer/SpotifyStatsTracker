from __future__ import annotations
import time
from config import (
    TREND_OBSESSION_DAYS,
    TREND_OBSESSION_MIN_PLAYS,
    TREND_REDISCOVERY_GAP_DAYS,
    TREND_REDISCOVERY_MIN_HISTORICAL_PLAYS,
    TREND_FORGOTTEN_GAP_DAYS,
    TREND_FORGOTTEN_MIN_HISTORICAL_PLAYS,
)

SECONDS_PER_DAY = 86400


class TrendQueries:
    """TrendQueries: SQL queries for Dashboard Obsession, Rediscovery, and Forgotten Favorites."""

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
            WHERE username = ? AND played_at >= ?
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
                WHERE username = ? AND played_at >= ?
                GROUP BY track_id
                HAVING recent_count >= 2
                ORDER BY recent_count DESC, recent_ms DESC
                LIMIT 1
                """,
                (username, obsession_cutoff),
            ).fetchone()

        # 2. Rediscovery
        rediscovery_gap_cutoff = now_ts - (TREND_REDISCOVERY_GAP_DAYS * SECONDS_PER_DAY)
        rediscovery_row = conn.execute(
            """
            SELECT track_id, 
                   COUNT(CASE WHEN played_at >= ? THEN 1 END) as recent_count,
                   COUNT(CASE WHEN played_at < ? THEN 1 END) as old_count,
                   MAX(CASE WHEN played_at < ? THEN played_at END) as max_old_played_at
            FROM plays
            WHERE username = ?
            GROUP BY track_id
            HAVING recent_count >= 1
               AND old_count >= ?
               AND max_old_played_at IS NOT NULL
               AND max_old_played_at <= ?
            ORDER BY recent_count DESC, old_count DESC
            LIMIT 1
            """,
            (
                obsession_cutoff,
                obsession_cutoff,
                obsession_cutoff,
                username,
                TREND_REDISCOVERY_MIN_HISTORICAL_PLAYS,
                rediscovery_gap_cutoff,
            ),
        ).fetchone()

        # 3. Forgotten Favorite
        forgotten_gap_cutoff = now_ts - (TREND_FORGOTTEN_GAP_DAYS * SECONDS_PER_DAY)
        forgotten_row = conn.execute(
            """
            SELECT track_id, COUNT(*) as total_plays, MAX(played_at) as last_played_at
            FROM plays
            WHERE username = ?
            GROUP BY track_id
            HAVING last_played_at <= ? AND total_plays >= ?
            ORDER BY total_plays DESC, last_played_at DESC
            LIMIT 1
            """,
            (username, forgotten_gap_cutoff, TREND_FORGOTTEN_MIN_HISTORICAL_PLAYS),
        ).fetchone()

        # Fallback for forgotten if no track hits high threshold
        if not forgotten_row:
            forgotten_row = conn.execute(
                """
                SELECT track_id, COUNT(*) as total_plays, MAX(played_at) as last_played_at
                FROM plays
                WHERE username = ?
                GROUP BY track_id
                HAVING last_played_at <= ? AND total_plays >= 2
                ORDER BY total_plays DESC, last_played_at DESC
                LIMIT 1
                """,
                (username, forgotten_gap_cutoff),
            ).fetchone()

        return {
            "obsession": dict(obsession_row) if obsession_row else None,
            "rediscovery": dict(rediscovery_row) if rediscovery_row else None,
            "forgotten": dict(forgotten_row) if forgotten_row else None,
        }
