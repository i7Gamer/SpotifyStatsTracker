# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

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

    def deleteUserWrappedFromYear(self, username: str, year: int) -> int:
        """Drop every cached year at or after `year`. Returns how many went.

        A Wrapped year is NOT a pure function of that year's own plays. The
        discovery fields - discovered_songs, discovered_artists and the three
        discovered_*_list columns - are anchored on each item's ALL-TIME first
        listen (getDiscoveredSongsCount groups over unfiltered history and
        keeps MIN(played_at) in range; wrapped_builder._discoveriesInYear
        filters an unbounded stats call on firstListenedAt). So a play written
        into an earlier year moves items into and out of LATER years'
        discovery lists while leaving those years' own play_count and
        max_played_at untouched - and those two are all
        _wrappedCacheNeedsRecalc compares, so nothing would ever notice.

        Only forwards: a first listen can move within or after the year the
        new play lands in, never before it, so earlier years are safe and
        dropping them would just buy a full-year recomputation each.

        A no-op for years with no cached row, so the cost is bounded by what
        was actually cached rather than by the length of the user's history."""
        conn = self._conn()
        with conn:
            return conn.execute(
                "DELETE FROM user_wrapped WHERE username = ? AND year >= ?",
                (username, year)
            ).rowcount

    def deleteAllUserWrapped(self, username: str) -> int:
        """Drop every cached year for one user, for a change that invalidates
        all of them at once rather than year by year - a timezone change, which
        moves every bucket boundary, weekday name and streak day. Returns how
        many rows went, so the caller can say whether anything was cached."""
        conn = self._conn()
        with conn:
            return conn.execute(
                "DELETE FROM user_wrapped WHERE username = ?", (username,)
            ).rowcount

    def deleteAllWrapped(self) -> int:
        """Drop every cached Wrapped row, for every user at once - the track
        merge's invalidation.

        A merge is global (sameness is a property of the recording), so its
        effect on the frozen top_songs / unique_songs / discovered_songs_list
        JSON reaches every account's every year simultaneously - and none of
        those years would ever notice on their own, because past years'
        max_played_at and play counts are exactly what a merge does NOT change,
        and they are all _wrappedCacheNeedsRecalc compares. Same reasoning as
        deleteUserWrappedFromYear's, taken to the instance.

        Cheap to invalidate, lazy to rebuild: the page recalculates a missing
        year synchronously on view (dashboard/wrapped_builder.py) and the
        15-minute worker refills the rest. Returns rows dropped."""
        conn = self._conn()
        with conn:
            return conn.execute("DELETE FROM user_wrapped").rowcount

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
