from __future__ import annotations

from Database.queries._base import *  # noqa: F401,F403 - shared constants/db helpers

try:
    from Database.lastfm import foldStylizedArtistName
except ModuleNotFoundError:
    from lastfm import foldStylizedArtistName


class GenreQueries:
    """GenreQueries: genres data-access methods, mixed into Repository."""

    # ---- Catalog: Last.fm genres ---------------------------------------------
    # Genres are catalog data (shared across users) fetched from Last.fm by the
    # per-user genre backfiller (Database.startLastfmGenreBackfiller). Queue
    # semantics mirror getAlbumsMissingMetadata: lastfm_attempted_at rate-limits
    # retries, entities holding own (non-inherited) genre rows never requeue.

    _GENRE_TABLES = {
        "artist": ("artist_genres", "artist_id"),
        "album": ("album_genres", "album_id"),
        "track": ("track_genres", "track_id"),
    }

    def _replaceGenres(self, kind: str, entityId: str, genres: list[str], inherited: bool) -> None:
        table, idColumn = self._GENRE_TABLES[kind]
        conn = self._conn()
        with conn:
            conn.execute(f"DELETE FROM {table} WHERE {idColumn}=?", (entityId,))
            if kind == "artist":
                conn.executemany(
                    f"INSERT INTO {table} ({idColumn}, genre, position) VALUES (?, ?, ?)",
                    [(entityId, genre, position) for position, genre in enumerate(genres)],
                )
            else:
                conn.executemany(
                    f"INSERT INTO {table} ({idColumn}, genre, position, inherited) VALUES (?, ?, ?, ?)",
                    [(entityId, genre, position, int(inherited)) for position, genre in enumerate(genres)],
                )

    def replaceArtistGenres(self, artistId: str, genres: list[str]) -> None:
        self._replaceGenres("artist", artistId, genres, inherited=False)

    def replaceAlbumGenres(self, albumId: str, genres: list[str], inherited: bool = False) -> None:
        self._replaceGenres("album", albumId, genres, inherited)

    def replaceTrackGenres(self, trackId: str, genres: list[str], inherited: bool = False) -> None:
        self._replaceGenres("track", trackId, genres, inherited)

    def getArtistGenres(self, artistId: str) -> list[str]:
        rows = self._conn().execute(
            "SELECT genre FROM artist_genres WHERE artist_id=? ORDER BY position", (artistId,)
        ).fetchall()
        return [r["genre"] for r in rows]

    def getAlbumGenres(self, albumId: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT genre, inherited FROM album_genres WHERE album_id=? ORDER BY position", (albumId,)
        ).fetchall()
        return [{"genre": r["genre"], "inherited": bool(r["inherited"])} for r in rows]

    def getTrackGenres(self, trackId: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT genre, inherited FROM track_genres WHERE track_id=? ORDER BY position", (trackId,)
        ).fetchall()
        return [{"genre": r["genre"], "inherited": bool(r["inherited"])} for r in rows]

    def _markLastfmAttempted(self, table: str, ids: list[str]) -> None:
        """Stamp entities as processed so they leave the backfill queue -
        including empty/not-found lookups, which would otherwise be re-fetched
        every cycle forever (mirrors markAlbumsBackfillAttempted)."""
        if not ids:
            return
        conn = self._conn()
        placeholders = ",".join("?" for _ in ids)
        with conn:
            conn.execute(
                f"UPDATE {table} SET lastfm_attempted_at = ? WHERE id IN ({placeholders})",
                [time.time(), *ids],
            )

    def markArtistsLastfmAttempted(self, artistIds: list[str]) -> None:
        self._markLastfmAttempted("artists", artistIds)

    def markAlbumsLastfmAttempted(self, albumIds: list[str]) -> None:
        self._markLastfmAttempted("albums", albumIds)

    def markTracksLastfmAttempted(self, trackIds: list[str]) -> None:
        self._markLastfmAttempted("tracks", trackIds)

    def requeueLastfmEntitiesWithoutOwnGenres(self) -> int:
        """Clears lastfm_attempted_at wherever the entity holds no own
        (non-inherited) genre rows, re-entering it into the backfill queues
        immediately - the 1.19.0 -> 1.20.0 migration's lever after lookup
        improvements. Inherited rows stay in place so stats keep working
        until the re-run replaces them. Returns how many were requeued."""
        conn = self._conn()
        cleared = 0
        with conn:
            for table, genreTable, idColumn in (
                ("tracks", "track_genres", "track_id"),
                ("albums", "album_genres", "album_id"),
            ):
                cleared += conn.execute(
                    f"""
                    UPDATE {table} SET lastfm_attempted_at = NULL
                    WHERE lastfm_attempted_at IS NOT NULL
                      AND NOT EXISTS (SELECT 1 FROM {genreTable} g
                                      WHERE g.{idColumn} = {table}.id AND g.inherited = 0)
                    """
                ).rowcount
            cleared += conn.execute(
                """
                UPDATE artists SET lastfm_attempted_at = NULL
                WHERE lastfm_attempted_at IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM artist_genres g WHERE g.artist_id = artists.id)
                """
            ).rowcount
        return cleared

    def requeueAlbumsLastfmWithoutOwnGenres(self) -> int:
        """Clears lastfm_attempted_at for albums holding no own (non-inherited)
        genre rows, re-entering them into the backfill queue immediately - the
        1.24.0 -> 1.25.0 migration's lever after the album.getinfo fallback fix
        (album.gettoptags was confirmed to miss real tag data for ~46% of
        tag-less albums that getinfo has). Scoped to albums only, unlike
        requeueLastfmEntitiesWithoutOwnGenres: that fix doesn't touch artist or
        track lookups, so requeuing those too would just re-run unchanged
        results against the shared rate limiter for no benefit. Existing
        inherited rows stay in place so genre stats keep working until the
        re-run replaces them. Returns how many albums were requeued."""
        conn = self._conn()
        with conn:
            cleared = conn.execute(
                """
                UPDATE albums SET lastfm_attempted_at = NULL
                WHERE lastfm_attempted_at IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM album_genres g
                                  WHERE g.album_id = albums.id AND g.inherited = 0)
                """
            ).rowcount
        return cleared

    def requeueArtistsWithFoldableNamesWithoutGenres(self) -> int:
        """Requeues artists whose Last.fm lookup was attempted before
        getArtistTopTags gained foldStylizedArtistName's stylized-letter/
        decorative-mark retry ("HUGO" recovers real tags where the stored
        "HUGØ" doesn't; confirmed live against the API, not a guess). Only
        artists whose name actually changes under folding are cleared - the
        fold test is a Python function, so candidates are filtered in Python
        rather than SQL, same as requeueDecoratedAlbumsWithoutBios. Returns
        how many artists were requeued."""
        conn = self._conn()
        rows = conn.execute(
            """
            SELECT id, name FROM artists
            WHERE lastfm_attempted_at IS NOT NULL
              AND id NOT IN (SELECT DISTINCT artist_id FROM artist_genres)
            """
        ).fetchall()
        foldableIds = [row["id"] for row in rows
                       if row["name"] and foldStylizedArtistName(row["name"]) != row["name"]]
        if not foldableIds:
            return 0
        with conn:
            conn.executemany(
                "UPDATE artists SET lastfm_attempted_at = NULL WHERE id = ?",
                [(artistId,) for artistId in foldableIds],
            )
        return len(foldableIds)

    def getArtistLastfmState(self, artistId: str) -> dict:
        """Attempt stamp + current genres for one artist - the inheritance
        decision for a tag-less track/album re-reads this at process time (the
        same worker cycle has usually just processed the artist batch)."""
        row = self._conn().execute(
            "SELECT lastfm_attempted_at FROM artists WHERE id=?", (artistId,)
        ).fetchone()
        return {
            "attempted_at": row["lastfm_attempted_at"] if row else None,
            "genres": self.getArtistGenres(artistId),
        }

    def getArtistsMissingGenres(self, limit: int, username: str | None = None) -> list[dict]:
        """Played artists credited within the first
        GENRE_BACKFILL_MAX_ARTIST_POSITION+1 track_artists positions (not just
        the position-0 primary - feature/collab-only artists get queued too)
        still needing a Last.fm lookup, most-played first. `username` scopes
        the queue (and the play counts) to one user's history; None is the
        global queue a worker falls back to once its owner's entities are
        done. Because a track can now match more than one credited artist,
        play_count reflects "plays on tracks where this artist is credited
        within the cutoff", not "plays where this artist is primary"."""
        conn = self._conn()
        params: list = []
        userClause = self._queueUserClause(params, username)
        params.extend([time.time() - GENRE_BACKFILL_RETRY_SECONDS, limit])
        rows = conn.execute(
            f"""
            SELECT ar.id AS id, ar.name AS name, COUNT(*) AS play_count
            FROM plays p
            JOIN track_artists ta ON ta.track_id = p.track_id
                AND ta.position <= {GENRE_BACKFILL_MAX_ARTIST_POSITION}
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE {userClause}(ar.lastfm_attempted_at IS NULL
                   OR (ar.lastfm_attempted_at < ?
                       AND NOT EXISTS (SELECT 1 FROM artist_genres ag WHERE ag.artist_id = ar.id)))
            GROUP BY ar.id
            ORDER BY play_count DESC, ar.id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def getAlbumsMissingGenres(self, limit: int, username: str | None = None) -> list[dict]:
        conn = self._conn()
        params: list = []
        userClause = self._queueUserClause(params, username)
        params.extend([time.time() - GENRE_BACKFILL_RETRY_SECONDS, limit])
        rows = conn.execute(
            f"""
            SELECT al.id AS id, al.name AS name, COUNT(*) AS play_count
            FROM plays p
            JOIN tracks t ON t.id = p.track_id
            JOIN albums al ON al.id = t.album_id
            WHERE {userClause}(al.lastfm_attempted_at IS NULL
                   OR (al.lastfm_attempted_at < ?
                       AND NOT EXISTS (SELECT 1 FROM album_genres ag
                                       WHERE ag.album_id = al.id AND ag.inherited = 0)))
            GROUP BY al.id
            ORDER BY play_count DESC, al.id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def getTracksMissingGenres(self, limit: int, username: str | None = None) -> list[dict]:
        """Rows carry the primary artist - both the track.getTopTags lookup and
        genre inheritance need it. Tracks with no position-0 artist row are
        structurally excluded: they can neither be looked up nor inherit."""
        conn = self._conn()
        params: list = []
        userClause = self._queueUserClause(params, username)
        params.extend([time.time() - GENRE_BACKFILL_RETRY_SECONDS, limit])
        rows = conn.execute(
            f"""
            SELECT t.id AS id, t.name AS name, t.album_id AS album_id,
                   ar.id AS artist_id, ar.name AS artist_name,
                   COUNT(*) AS play_count
            FROM plays p
            JOIN tracks t ON t.id = p.track_id
            JOIN track_artists ta ON ta.track_id = t.id AND ta.position = 0
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE {userClause}(t.lastfm_attempted_at IS NULL
                   OR (t.lastfm_attempted_at < ?
                       AND NOT EXISTS (SELECT 1 FROM track_genres tg
                                       WHERE tg.track_id = t.id AND tg.inherited = 0)))
            GROUP BY t.id
            ORDER BY play_count DESC, t.id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _queueUserClause(params: list, username: str | None) -> str:
        """'p.username = ? AND ' with the bind appended, or '' for the global
        queue - the username filter must be dropped entirely (not IS-NULL-
        parameterized) so SQLite can still range the (username, ...) index
        when scoped and skip it when not."""
        if username is None:
            return ""
        params.append(username)
        return "p.username = ? AND "

    def getArtistLastfmLookupRow(self, artistId: str) -> dict | None:
        """{"id", "name"} for one artist, ignoring attempt state entirely -
        unlike getArtistsMissingGenres, this backs the admin "refresh Last.fm
        data" action, which must work even on an already-attempted artist."""
        row = self._conn().execute(
            "SELECT id, name FROM artists WHERE id = ?", (artistId,)
        ).fetchone()
        return dict(row) if row else None

    def getAlbumLastfmLookupRow(self, albumId: str) -> dict | None:
        """{"id", "name"} for one album - mirrors getArtistLastfmLookupRow."""
        row = self._conn().execute(
            "SELECT id, name FROM albums WHERE id = ?", (albumId,)
        ).fetchone()
        return dict(row) if row else None

    def getTrackLastfmLookupRow(self, trackId: str) -> dict | None:
        """{"id", "name", "album_id", "artist_id", "artist_name"} for one
        track via its position-0 artist - same row shape as
        getTracksMissingGenres, but for exactly one id and ignoring attempt
        state (see getArtistLastfmLookupRow). None if the track has no
        position-0 artist, the same structural exclusion
        getTracksMissingGenres applies."""
        row = self._conn().execute(
            """
            SELECT t.id AS id, t.name AS name, t.album_id AS album_id,
                   ar.id AS artist_id, ar.name AS artist_name
            FROM tracks t
            JOIN track_artists ta ON ta.track_id = t.id AND ta.position = 0
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE t.id = ?
            """,
            (trackId,),
        ).fetchone()
        return dict(row) if row else None

    # ---- Instance-wide admin insights -----------------------------------------
    # Catalog/instance-scoped numbers for the /admin page - unlike the per-user
    # stats above (getGenreCoverage, getBiographyCoverage), these aren't
    # filtered by any one user's plays, so they reflect backfill progress and
    # activity across the whole shared instance.

    def getCatalogGenreCoverage(self, includeInherited: bool | None = None) -> dict:
        """Catalog-wide analogue of Database.getGenreCoverage: the share of
        all tracks/albums/artists (not plays) that carry at least one
        Last.fm genre row. Mirrors GENRE_COVERAGE_CATEGORIES = ("song",
        "album", "artist") from Database/database.py - not imported here to
        avoid a circular import (database.py imports this module)."""
        if includeInherited is None:
            includeInherited = self.isInheritedGenresEnabled()
        inherited = 1 if includeInherited else 0
        conn = self._conn()

        def category(table: str, genreTable: str, idColumn: str, hasInherited: bool) -> dict:
            total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if hasInherited:
                covered = conn.execute(
                    f"""
                    SELECT COUNT(DISTINCT e.id) FROM {table} e
                    JOIN {genreTable} g ON g.{idColumn} = e.id
                    WHERE (? OR g.inherited = 0)
                    """,
                    (inherited,),
                ).fetchone()[0]
                own_covered = conn.execute(
                    f"""
                    SELECT COUNT(DISTINCT e.id) FROM {table} e
                    JOIN {genreTable} g ON g.{idColumn} = e.id
                    WHERE g.inherited = 0
                    """
                ).fetchone()[0]
            else:
                covered = conn.execute(
                    f"SELECT COUNT(DISTINCT e.id) FROM {table} e JOIN {genreTable} g ON g.{idColumn} = e.id"
                ).fetchone()[0]
                own_covered = covered
            percent = round(covered / total * 100, 1) if total else 0.0
            own_percent = round(own_covered / total * 100, 1) if total else 0.0
            return {
                "covered": covered,
                "own_covered": own_covered,
                "total": total,
                "percent": percent,
                "own_percent": own_percent,
                "ownPercent": own_percent,
            }

        coverage = {
            "song": category("tracks", "track_genres", "track_id", True),
            "album": category("albums", "album_genres", "album_id", True),
            "artist": category("artists", "artist_genres", "artist_id", False),
        }
        percents = [coverage["song"]["percent"], coverage["album"]["percent"], coverage["artist"]["percent"]]
        coverage["overall"] = {"percent": round(sum(percents) / len(percents), 1)}
        return coverage
