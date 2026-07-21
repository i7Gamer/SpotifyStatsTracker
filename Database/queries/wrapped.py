from __future__ import annotations

from Database.queries._base import *  # noqa: F401,F403 - shared constants/db helpers


class WrappedQueries:
    """WrappedQueries: wrapped data-access methods, mixed into Repository."""

    def getCachedWrappedMaxPlayedAt(self, username: str, year: int) -> float | None:
        row = self._conn().execute(
            "SELECT max_played_at FROM user_wrapped WHERE username = ? AND year = ?",
            (username, year)
        ).fetchone()
        return row[0] if row else None

    def getCachedWrappedTotalPlays(self, username: str, year: int) -> int | None:
        row = self._conn().execute(
            "SELECT total_plays FROM user_wrapped WHERE username = ? AND year = ?",
            (username, year)
        ).fetchone()
        return row[0] if row else None

    def deleteUserWrapped(self, username: str, year: int) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "DELETE FROM user_wrapped WHERE username = ? AND year = ?",
                (username, year)
            )

    def getCachedWrapped(self, username: str, year: int) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM user_wrapped WHERE username = ? AND year = ?",
            (username, year)
        ).fetchone()
        if not row:
            return None
        return dict(row)

    def saveCachedWrapped(self, username: str, year: int, data: dict) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                """
                INSERT INTO user_wrapped (
                    username, year, calculated_at, max_played_at,
                    total_plays, total_ms, longest_streak, peak_day, peak_plays,
                    unique_songs, unique_artists, discovered_songs, discovered_artists,
                    time_series_day, time_series_week, time_series_month,
                    top_songs, top_artists, top_albums,
                    discovered_songs_list, discovered_artists_list, discovered_albums_list
                ) VALUES (
                    :username, :year, :calculated_at, :max_played_at,
                    :total_plays, :total_ms, :longest_streak, :peak_day, :peak_plays,
                    :unique_songs, :unique_artists, :discovered_songs, :discovered_artists,
                    :time_series_day, :time_series_week, :time_series_month,
                    :top_songs, :top_artists, :top_albums,
                    :discovered_songs_list, :discovered_artists_list, :discovered_albums_list
                )
                ON CONFLICT(username, year) DO UPDATE SET
                    calculated_at=excluded.calculated_at,
                    max_played_at=excluded.max_played_at,
                    total_plays=excluded.total_plays,
                    total_ms=excluded.total_ms,
                    longest_streak=excluded.longest_streak,
                    peak_day=excluded.peak_day,
                    peak_plays=excluded.peak_plays,
                    unique_songs=excluded.unique_songs,
                    unique_artists=excluded.unique_artists,
                    discovered_songs=excluded.discovered_songs,
                    discovered_artists=excluded.discovered_artists,
                    time_series_day=excluded.time_series_day,
                    time_series_week=excluded.time_series_week,
                    time_series_month=excluded.time_series_month,
                    top_songs=excluded.top_songs,
                    top_artists=excluded.top_artists,
                    top_albums=excluded.top_albums,
                    discovered_songs_list=excluded.discovered_songs_list,
                    discovered_artists_list=excluded.discovered_artists_list,
                    discovered_albums_list=excluded.discovered_albums_list
                """,
                {**data, "username": username, "year": year}
            )
