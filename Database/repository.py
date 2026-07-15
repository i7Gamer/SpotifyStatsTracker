from __future__ import annotations
import datetime
import json
import time
from pathlib import Path

try:
    import Database.db as db
    from Database.db import ConnectionManager
except ModuleNotFoundError:
    import db
    from db import ConnectionManager

IMAGE_KIND_TRACK = "track"
IMAGE_KIND_ARTIST = "artist"
IMAGE_STATUS_PENDING = "pending"
IMAGE_STATUS_OK = "ok"
IMAGE_STATUS_FAILED = "failed"

# Whitelist mapping the public sortBy values to the SQL output-column aliases
# they're allowed to sort by. sortBy is interpolated directly into ORDER BY
# (column names can't be bound as query parameters), and it's user-controlled
# (app.py's sortBy query param) - this whitelist is what makes that safe.
# "name" sorts COLLATE NOCASE so e.g. "abba" and "ABBA" interleave by letter
# instead of every uppercase name sorting before every lowercase one (SQLite's
# default BINARY collation).
SONG_SORT_COLUMNS = {
    "plays": "plays",
    "totalTimeListened": "total_time_listened",
    "name": "name COLLATE NOCASE",
}

ALBUM_SORT_COLUMNS = {
    "plays": "plays",
    "totalTimeListened": "total_time_listened",
    "name": "name COLLATE NOCASE",
}

ARTIST_SORT_COLUMNS = {
    "plays": "plays",
    "totalTimeListened": "total_time_listened",
    "name": "name COLLATE NOCASE",
}


class Repository:
    """Data-access layer over the shared SQLite database.

    Catalog methods (tracks/artists/albums/playlists/images) operate on data
    that's global across every user, keyed by Spotify's own catalog ids.
    Per-user methods (plays/users/progress) are scoped by `username`.
    """

    def __init__(self, dbPath: Path | None = None):
        # Resolved against db.DEFAULT_DB_PATH at call time rather than as a
        # normal default argument, so tests can monkeypatch db.DEFAULT_DB_PATH
        # (see conftest.py's _isolateDefaultDbPath) and have every Repository()
        # constructed without an explicit path - including indirectly, e.g. via
        # SpotifyDashboardApp() - redirect to a per-test temp file instead of the
        # real project database.
        self.connectionManager = ConnectionManager(dbPath if dbPath is not None else db.DEFAULT_DB_PATH)

    def _conn(self):
        return self.connectionManager.connection()

    def connection(self):
        """Exposes the thread-local connection for callers that need to compose
        several non-auto-committing writes (upsertTrack/insertPlay) into a single
        transaction - e.g. a bulk import that must commit all-or-nothing."""
        return self._conn()

    def commit(self):
        self._conn().commit()

    def rollback(self):
        self._conn().rollback()

    # ---- Catalog: tracks / artists / albums ----------------------------------

    def upsertTrack(self, track: dict, created_reason: str | None = None) -> None:
        """Upsert a track and its nested album/artists (as produced by
        Client.formatTrack). Last write wins, matching the previous
        tracks[id] = track dict-assignment semantics. If created_reason is provided,
        it's only set on INSERT (never updated on conflict).

        Does NOT commit - callers compose this with insertPlay() into a single
        transaction (one play = one commit; a bulk import = one commit for the
        whole batch), then call commit()/rollback() themselves."""
        album = track["album"]
        artists = track["artists"]
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO albums (id, name, url, total_tracks, release_date, image_id, image_url)
            VALUES (:id, :name, :url, :totalTracks, :releaseDate, :id, :imageUrl)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, url=excluded.url, total_tracks=excluded.total_tracks,
                release_date=excluded.release_date, image_url=excluded.image_url
            """,
            album,
        )

        for artist in artists:
            conn.execute(
                """
                INSERT INTO artists (id, name, url, image_id)
                VALUES (:id, :name, :url, :imageId)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, url=excluded.url, image_id=excluded.image_id
                """,
                artist,
            )

        trackData = {
            **track,
            "albumId": album["id"],
            "explicit": bool(track.get("explicit", False)),
            "created_at": None,
            "created_reason": None,
        }
        if created_reason:
            trackData["created_at"] = time.time()
            trackData["created_reason"] = created_reason

        conn.execute(
            """
            INSERT INTO tracks (id, name, url, album_id, image_id, duration_ms, explicit, isrc, disc_number, track_number, created_at, created_reason)
            VALUES (:id, :name, :url, :albumId, :imageId, :duration, :explicit, :isrc, :discNumber, :trackNumber, :created_at, :created_reason)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, url=excluded.url, album_id=excluded.album_id, image_id=excluded.image_id,
                duration_ms=excluded.duration_ms, explicit=excluded.explicit, isrc=excluded.isrc,
                disc_number=excluded.disc_number, track_number=excluded.track_number
            """,
            trackData,
        )

        conn.execute("DELETE FROM track_artists WHERE track_id=?", (track["id"],))
        for position, artist in enumerate(artists):
            conn.execute(
                "INSERT INTO track_artists (track_id, artist_id, position) VALUES (?, ?, ?)",
                (track["id"], artist["id"], position),
            )

    def getTrack(self, trackId: str) -> dict | None:
        conn = self._conn()
        trackRow = conn.execute("SELECT * FROM tracks WHERE id=?", (trackId,)).fetchone()
        if trackRow is None:
            return None
        albumRow = conn.execute("SELECT * FROM albums WHERE id=?", (trackRow["album_id"],)).fetchone()
        artistRows = conn.execute(
            """
            SELECT a.id, a.name, a.url, a.image_id FROM track_artists ta
            JOIN artists a ON a.id = ta.artist_id
            WHERE ta.track_id=? ORDER BY ta.position
            """,
            (trackId,),
        ).fetchall()
        return self._trackRowToDict(trackRow, albumRow, artistRows)

    def getAllTracks(self) -> list[dict]:
        """Every track in the shared catalog, fully reconstructed - used to seed
        the importer's "don't re-fetch metadata we already have" cache."""
        conn = self._conn()
        trackRows = conn.execute("SELECT * FROM tracks").fetchall()
        albumsById = {row["id"]: row for row in conn.execute("SELECT * FROM albums").fetchall()}
        artistsByTrack: dict[str, list] = {}
        for row in conn.execute(
            """
            SELECT ta.track_id, a.id, a.name, a.url, a.image_id FROM track_artists ta
            JOIN artists a ON a.id = ta.artist_id
            ORDER BY ta.track_id, ta.position
            """
        ).fetchall():
            artistsByTrack.setdefault(row["track_id"], []).append(row)

        return [
            self._trackRowToDict(trackRow, albumsById.get(trackRow["album_id"]),
                                  artistsByTrack.get(trackRow["id"], []))
            for trackRow in trackRows
        ]

    def getTracksByIds(self, trackIds: list[str]) -> dict[str, dict]:
        """Batch equivalent of getTrack() for a specific set of track ids, in a
        fixed 3 queries total regardless of how many ids are requested (tracks,
        albums, artists) - the caller-facing counterpart to getAllTracks(),
        scoped instead of unbounded. Mirrors getAllTracks()'s own raw-row
        artist query (not _artistsForTracks(), which returns already-converted
        dicts shaped for _songRowToDict rather than the raw rows
        _trackRowToDict() expects). Reused by Database._paginateEntries() so
        hydrating a page of play history doesn't pay 3 queries per play
        (getTrack()'s old N+1)."""
        if not trackIds:
            return {}
        conn = self._conn()
        placeholders = ",".join("?" for _ in trackIds)
        trackRows = conn.execute(f"SELECT * FROM tracks WHERE id IN ({placeholders})", trackIds).fetchall()

        albumIds = {row["album_id"] for row in trackRows}
        albumsById = {}
        if albumIds:
            albumIdList = list(albumIds)
            albumPlaceholders = ",".join("?" for _ in albumIdList)
            albumsById = {
                row["id"]: row
                for row in conn.execute(f"SELECT * FROM albums WHERE id IN ({albumPlaceholders})", albumIdList).fetchall()
            }

        resolvedIds = [row["id"] for row in trackRows]
        artistsByTrack: dict[str, list] = {}
        if resolvedIds:
            idPlaceholders = ",".join("?" for _ in resolvedIds)
            for row in conn.execute(
                f"""
                SELECT ta.track_id, a.id, a.name, a.url, a.image_id FROM track_artists ta
                JOIN artists a ON a.id = ta.artist_id
                WHERE ta.track_id IN ({idPlaceholders})
                ORDER BY ta.track_id, ta.position
                """,
                resolvedIds,
            ).fetchall():
                artistsByTrack.setdefault(row["track_id"], []).append(row)

        return {
            row["id"]: self._trackRowToDict(row, albumsById.get(row["album_id"]), artistsByTrack.get(row["id"], []))
            for row in trackRows
        }

    @classmethod
    def _trackRowToDict(cls, trackRow, albumRow, artistRows) -> dict:
        return {
            "id": trackRow["id"],
            "name": trackRow["name"],
            "url": trackRow["url"],
            "imageUrl": albumRow["image_url"] if albumRow else "",
            "imageId": trackRow["image_id"],
            "duration": trackRow["duration_ms"],
            "explicit": bool(trackRow["explicit"]),
            "isrc": trackRow["isrc"] or "",
            "discNumber": trackRow["disc_number"],
            "trackNumber": trackRow["track_number"],
            "releaseDate": albumRow["release_date"] if albumRow else None,
            "album": cls._albumRowToDict(albumRow) if albumRow else None,
            "artists": [
                {"id": r["id"], "name": r["name"], "url": r["url"], "imageUrl": "", "imageId": r["image_id"]}
                for r in artistRows
            ],
        }

    def trackExists(self, trackId: str) -> bool:
        conn = self._conn()
        row = conn.execute("SELECT 1 FROM tracks WHERE id=?", (trackId,)).fetchone()
        return row is not None

    @staticmethod
    def _albumRowToDict(albumRow) -> dict:
        return {
            "id": albumRow["id"],
            "name": albumRow["name"],
            "url": albumRow["url"],
            "imageId": albumRow["image_id"],
            "imageUrl": albumRow["image_url"],
            "totalTracks": albumRow["total_tracks"],
            "releaseDate": albumRow["release_date"],
        }

    # ---- Catalog: playlists ----------------------------------------------------

    def upsertPlaylistName(self, playlistId: str, playlistType: str, name: str | None) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                """
                INSERT INTO playlists (id, type, name) VALUES (?, ?, ?)
                ON CONFLICT(id, type) DO UPDATE SET name=excluded.name
                """,
                (playlistId, playlistType, name),
            )

    def getPlaylistName(self, playlistId: str, playlistType: str) -> str | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT name FROM playlists WHERE id=? AND type=?", (playlistId, playlistType)
        ).fetchone()
        return row["name"] if row else None

    def playlistKnown(self, playlistId: str, playlistType: str) -> bool:
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM playlists WHERE id=? AND type=?", (playlistId, playlistType)
        ).fetchone()
        return row is not None

    # ---- Catalog: images (shared download-dedup tracking) ----------------------

    def tryClaimImageDownload(self, imageId: str, kind: str) -> bool:
        """Atomically claim the right to download this image: returns True if the
        caller should proceed (nothing else has claimed or finished it), False if
        it's already downloaded or another thread already claimed it. A
        previously-failed claim can be reclaimed."""
        conn = self._conn()
        with conn:
            row = conn.execute(
                "SELECT status FROM images WHERE id=? AND kind=?",
                (imageId, kind)
            ).fetchone()

            if row is not None and row["status"] in (IMAGE_STATUS_OK, IMAGE_STATUS_PENDING):
                return False

            conn.execute(
                """
                INSERT INTO images (id, kind, status) VALUES (?, ?, ?)
                ON CONFLICT(id, kind) DO UPDATE SET status=excluded.status
                """,
                (imageId, kind, IMAGE_STATUS_PENDING),
            )
            return True

    def markImageStatus(self, imageId: str, kind: str, status: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                """
                INSERT INTO images (id, kind, status) VALUES (?, ?, ?)
                ON CONFLICT(id, kind) DO UPDATE SET status=excluded.status
                """,
                (imageId, kind, status),
            )

    def imageStatus(self, imageId: str, kind: str) -> str | None:
        conn = self._conn()
        row = conn.execute("SELECT status FROM images WHERE id=? AND kind=?", (imageId, kind)).fetchone()
        return row["status"] if row else None

    # ---- Per-user: plays (play history) -----------------------------------------

    def insertPlay(self, username: str, trackId: str, playedAt: float, timePlayed: int,
                   playedFrom: str | None = None, created_reason: str | None = None) -> bool:
        """Returns True if a new row was inserted, False if this exact
        (username, trackId, playedAt) play was already recorded (updates time_played if different).
        If created_reason is provided, it's only set on INSERT (never updated
        on an existing play, matching upsertTrack()'s semantics).

        Does NOT commit - see upsertTrack()'s docstring."""
        conn = self._conn()
        existing = conn.execute(
            "SELECT id, time_played FROM plays WHERE username=? AND track_id=? AND played_at=?",
            (username, trackId, playedAt)
        ).fetchone()

        if existing:
            if existing["time_played"] != timePlayed:
                conn.execute(
                    "UPDATE plays SET time_played = ?, played_from = COALESCE(?, played_from) WHERE id = ?",
                    (timePlayed, playedFrom, existing["id"])
                )
            return False

        createdAt = time.time() if created_reason else None
        cur = conn.execute(
            "INSERT OR IGNORE INTO plays (username, track_id, played_at, time_played, played_from, created_at, created_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (username, trackId, playedAt, timePlayed, playedFrom, createdAt, created_reason),
        )
        return cur.rowcount > 0

    def deletePlay(self, username: str, trackId: str, playedAt: float) -> bool:
        """Delete one specific play - the exact (username, trackId, playedAt)
        tuple insertPlay() already treats as unique. Returns True if a row was
        deleted.

        Does NOT commit - see upsertTrack()'s docstring."""
        conn = self._conn()
        cur = conn.execute(
            "DELETE FROM plays WHERE username=? AND track_id=? AND played_at=?",
            (username, trackId, playedAt),
        )
        return cur.rowcount > 0

    def hasPlayNearTime(self, username: str, trackId: str, playedAt: float, toleranceSeconds: float) -> bool:
        """True if a play for this exact track already exists for this user
        within toleranceSeconds of playedAt (inclusive both directions).
        Reuses idx_plays_user_track. See Database.appendTrackData for why this
        is a wide, defense-in-depth guard applied only to Web API backfill
        inserts, not the live listener's own insert path."""
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM plays WHERE username=? AND track_id=? AND played_at BETWEEN ? AND ? LIMIT 1",
            (username, trackId, playedAt - toleranceSeconds, playedAt + toleranceSeconds),
        ).fetchone()
        return row is not None

    def getPlaysNearTime(self, username: str, trackId: str, playedAt: float, toleranceSeconds: float) -> list[dict]:
        """Return all plays for this exact track already existing for this user
        within toleranceSeconds of playedAt (inclusive both directions).
        Used during imports to detect duplicates and decide whether to update."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, played_at, time_played FROM plays WHERE username=? AND track_id=? AND played_at BETWEEN ? AND ?",
            (username, trackId, playedAt - toleranceSeconds, playedAt + toleranceSeconds),
        ).fetchall()
        return [dict(row) for row in rows]

    def deleteZeroDurationPlays(self) -> int:
        """Remove plays with zero (or negative) recorded listening time, across
        every user - leftover skip/error events that older importer versions
        recorded as real plays before the importer started filtering them out
        at import time. Returns the number of rows removed.

        Does NOT commit - see upsertTrack()'s docstring."""
        conn = self._conn()
        cur = conn.execute("DELETE FROM plays WHERE time_played <= 0")
        return cur.rowcount

    def getPlaysCount(self, username: str) -> int:
        conn = self._conn()
        row = conn.execute("SELECT COUNT(*) AS c FROM plays WHERE username=?", (username,)).fetchone()
        return row["c"]

    def getPlaysNewestFirst(self, username: str, count: int | None = None, startIndex: int = 0) -> list[dict]:
        conn = self._conn()
        limit = -1 if count is None else count
        rows = conn.execute(
            "SELECT track_id, played_at, time_played, played_from FROM plays "
            "WHERE username=? ORDER BY played_at DESC, id DESC LIMIT ? OFFSET ?",
            (username, limit, startIndex),
        ).fetchall()
        return [self._playRowToEntry(r) for r in rows]

    def getPlaysOldestFirst(self, username: str, count: int | None = None, startIndex: int = 0) -> list[dict]:
        conn = self._conn()
        limit = -1 if count is None else count
        rows = conn.execute(
            "SELECT track_id, played_at, time_played, played_from FROM plays "
            "WHERE username=? ORDER BY played_at ASC, id ASC LIMIT ? OFFSET ?",
            (username, limit, startIndex),
        ).fetchall()
        return [self._playRowToEntry(r) for r in rows]

    def getRecordedPlayedAtTimes(self, username: str) -> set:
        """Return set of all recorded played_at timestamps for this user.
        Used for deduplication when polling REST API."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT played_at FROM plays WHERE username=?",
            (username,),
        ).fetchall()
        return {row["played_at"] for row in rows}

    def deletePlaysBefore(self, username: str, timestamp: float) -> int:
        """Delete all plays for this user before the given timestamp (unix seconds).
        Returns the number of rows deleted. Does NOT commit."""
        conn = self._conn()
        cur = conn.execute(
            "DELETE FROM plays WHERE username=? AND played_at < ?",
            (username, timestamp),
        )
        return cur.rowcount

    @staticmethod
    def _playRowToEntry(row) -> dict:
        return {
            "id": row["track_id"],
            "playedAt": row["played_at"],
            "timePlayed": row["time_played"],
            "playedFrom": row["played_from"],
        }

    @staticmethod
    def _likePattern(query: str) -> str:
        """Wraps `query` for a LIKE '%...%' match, escaping LIKE's own wildcard
        characters so a literal "%" or "_" typed by the user is matched as text
        rather than treated as a wildcard - matches the substring-only
        semantics of the Python `in` check this replaces."""
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    # Dashboard search: matches a track's name, its artist(s), its album, and
    # the playlist/album it was played from, done in SQL so matching is
    # pushed down instead of requiring every play in the user's history to be
    # hydrated first. played_from is stored as "type:id" (see
    # Client.formatTrack), so it's split with substr/instr to join against
    # playlists(id, type) rather than needing a second round-trip through
    # Database.playlistName() per row.
    _SEARCH_JOIN_CLAUSE = """
        JOIN tracks t ON t.id = p.track_id
        LEFT JOIN albums al ON al.id = t.album_id
        LEFT JOIN playlists pl
               ON pl.id = substr(p.played_from, instr(p.played_from, ':') + 1)
              AND pl.type = substr(p.played_from, 1, instr(p.played_from, ':') - 1)
    """
    _SEARCH_MATCH_CLAUSE = """
        AND (
            t.name LIKE ? ESCAPE '\\'
            OR al.name LIKE ? ESCAPE '\\'
            OR pl.name LIKE ? ESCAPE '\\'
            OR EXISTS (
                SELECT 1 FROM track_artists ta JOIN artists ar ON ar.id = ta.artist_id
                WHERE ta.track_id = p.track_id AND ar.name LIKE ? ESCAPE '\\'
            )
        )
    """

    def searchPlays(self, username: str, query: str, limit: int | None = None, offset: int = 0) -> list[dict]:
        """Plays (newest first) whose track name, artist(s), album, or source
        playlist/album match `query` - the SQL-pushed-down, paginated
        replacement for fetching every play and filtering in Python."""
        conn = self._conn()
        limitValue = -1 if limit is None else limit
        pattern = self._likePattern(query)
        rows = conn.execute(
            f"""
            SELECT p.track_id AS track_id, p.played_at AS played_at,
                   p.time_played AS time_played, p.played_from AS played_from
            FROM plays p
            {self._SEARCH_JOIN_CLAUSE}
            WHERE p.username = ? {self._SEARCH_MATCH_CLAUSE}
            ORDER BY p.played_at DESC, p.id DESC
            LIMIT ? OFFSET ?
            """,
            (username, pattern, pattern, pattern, pattern, limitValue, offset),
        ).fetchall()
        return [self._playRowToEntry(r) for r in rows]

    def searchPlaysCount(self, username: str, query: str) -> int:
        """The paging counterpart to searchPlays() - total matching plays,
        for computing total page count without fetching every match."""
        conn = self._conn()
        pattern = self._likePattern(query)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM plays p
            {self._SEARCH_JOIN_CLAUSE}
            WHERE p.username = ? {self._SEARCH_MATCH_CLAUSE}
            """,
            (username, pattern, pattern, pattern, pattern),
        ).fetchone()
        return row["c"]

    # ---- Per-user: stats aggregates (SQL GROUP BY instead of Python loops over
    # the full history) -----------------------------------------------------------

    @staticmethod
    def _dateRangeClause() -> str:
        return "AND (? IS NULL OR played_at >= ?) AND (? IS NULL OR played_at <= ?)"

    def getPlayAggregatesByTrack(self, username: str, startTs: float | None = None,
                                  endTs: float | None = None) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            f"""
            SELECT track_id, COUNT(*) AS plays, SUM(time_played) AS total_time_listened,
                   MIN(played_at) AS first_listened_at
            FROM plays
            WHERE username = ? {self._dateRangeClause()}
            GROUP BY track_id
            """,
            (username, startTs, startTs, endTs, endTs),
        ).fetchall()
        return [
            {"trackId": r["track_id"], "plays": r["plays"], "totalTimeListened": r["total_time_listened"],
             "firstListenedAt": r["first_listened_at"]}
            for r in rows
        ]

    def getArtistAggregates(self, username: str, startTs: float | None = None,
                             endTs: float | None = None, artistId: str | None = None,
                             sortBy: str = "plays", limit: int | None = None, offset: int = 0,
                             searchQuery: str | None = None) -> list[dict]:
        """One row per artist who appears on at least one played track, grouped by
        artist id (not name - two different artists that happen to share a display
        name are no longer merged, unlike the old name-keyed in-memory grouping).
        Sorted/paged in SQL (mirrors getSongsPage()/getAlbumsPage()) rather than
        fetching every artist and sorting/paging in Python.

        `artistId` narrows this to a single artist - reused by artist-detail
        pages to fetch that one artist's own aggregate stats. `searchQuery`
        narrows to artists whose name matches (the only field Top Artists'
        search ever matched, since a bare artist dict carries no track/album/
        playlist text to search)."""
        if sortBy not in ARTIST_SORT_COLUMNS:
            raise ValueError(f"Unknown sortBy: {sortBy!r}")
        sortColumn = ARTIST_SORT_COLUMNS[sortBy]
        direction = "ASC" if sortBy == "name" else "DESC"
        limitValue = -1 if limit is None else limit

        conn = self._conn()
        params = [username, startTs, startTs, endTs, endTs]
        extraClauses = ""
        if artistId is not None:
            extraClauses += " AND ar.id = ?"
            params.append(artistId)
        if searchQuery:
            extraClauses += " AND ar.name LIKE ? ESCAPE '\\'"
            params.append(self._likePattern(searchQuery))
        params += [limitValue, offset]
        rows = conn.execute(
            f"""
            SELECT ar.id AS id, ar.name AS name, ar.url AS url, ar.image_id AS image_id,
                   COUNT(*) AS plays, SUM(p.time_played) AS total_time_listened,
                   MIN(p.played_at) AS first_listened_at, COUNT(DISTINCT p.track_id) AS unique_song_count
            FROM plays p
            JOIN track_artists ta ON ta.track_id = p.track_id
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE p.username = ? {self._dateRangeClause().replace("played_at", "p.played_at")}{extraClauses}
            GROUP BY ar.id
            ORDER BY {sortColumn} {direction}, total_time_listened {direction}, name COLLATE NOCASE {direction}, id ASC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [
            {
                "id": r["id"], "name": r["name"], "url": r["url"], "imageUrl": "", "imageId": r["image_id"],
                "plays": r["plays"], "totalTimeListened": r["total_time_listened"],
                "uniqueSongCount": r["unique_song_count"], "firstListenedAt": r["first_listened_at"],
            }
            for r in rows
        ]

    def getArtistsCount(self, username: str, startTs: float | None = None, endTs: float | None = None,
                         searchQuery: str | None = None) -> int:
        """Number of distinct artists played in range - the paging counterpart
        to getArtistAggregates(), used to compute total page count without
        fetching every artist's metadata."""
        conn = self._conn()
        params = [username, startTs, startTs, endTs, endTs]
        searchClause = ""
        if searchQuery:
            searchClause = " AND ar.name LIKE ? ESCAPE '\\'"
            params.append(self._likePattern(searchQuery))
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT ta.artist_id FROM plays p
                JOIN track_artists ta ON ta.track_id = p.track_id
                JOIN artists ar ON ar.id = ta.artist_id
                WHERE p.username = ? {self._dateRangeClause().replace("played_at", "p.played_at")}{searchClause}
                GROUP BY ta.artist_id
            )
            """,
            params,
        ).fetchone()
        return row["c"]

    def getArtistTotals(self, username: str, startTs: float | None = None,
                         endTs: float | None = None) -> tuple[int, int, int]:
        """(total plays, total unique songs, total time listened) summed across
        every artist in range - the Top Artists page's "(top list)" totals.
        Deliberately a sum of each artist's own aggregate (an artist with N
        plays contributes N; a multi-artist track's plays are counted once per
        artist on it), not the same number as getPlayTotals()'s track-level
        total - matches the totals the old fetch-everything-then-sum-in-Python
        code computed, just without hydrating every artist's name/url first."""
        conn = self._conn()
        params = [username, startTs, startTs, endTs, endTs]
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(plays), 0) AS total_plays,
                   COALESCE(SUM(unique_song_count), 0) AS total_unique,
                   COALESCE(SUM(total_time_listened), 0) AS total_time_listened
            FROM (
                SELECT COUNT(*) AS plays, COUNT(DISTINCT p.track_id) AS unique_song_count,
                       SUM(p.time_played) AS total_time_listened
                FROM plays p
                JOIN track_artists ta ON ta.track_id = p.track_id
                WHERE p.username = ? {self._dateRangeClause().replace("played_at", "p.played_at")}
                GROUP BY ta.artist_id
            )
            """,
            params,
        ).fetchone()
        return row["total_plays"], row["total_unique"], row["total_time_listened"]

    def getSongsPage(self, username: str, startTs: float | None = None, endTs: float | None = None,
                      sortBy: str = "plays", limit: int | None = None, offset: int = 0,
                      trackId: str | None = None, artistId: str | None = None,
                      albumId: str | None = None, searchQuery: str | None = None) -> list[dict]:
        """Sorted/paged song stats in one batched round-trip, replacing the old
        "aggregate, then getTrack() per row" N+1 pattern - a caller asking for
        page N now pays for page N, not for every song ever played.

        tracks/albums are a 1:1 relationship (tracks.album_id NOT NULL), so
        they're safe to aggregate together in one GROUP BY t.id query without
        duplicating rows. artists are 1:many per track, so they're fetched in a
        second, small query keyed by just this page's track ids (mirrors
        getAllTracks()'s two-query shape) rather than fanning out the GROUP BY.

        `trackId`/`artistId`/`albumId` narrow the result to a single track, an
        artist's songs, or an album's songs - reused by the song/artist/album
        detail pages instead of a separate query per lookup. `artistId` is
        matched via EXISTS rather than an extra JOIN so a multi-artist track
        still yields exactly one row. `searchQuery` narrows to songs whose
        name, album, or artist(s) match - safe to check via the current row's
        own t.id (unlike getAlbumsPage(), every row already shares the same
        t.id within a GROUP BY t.id group, so there's no risk of the filter
        seeing a different track's data than the one being aggregated).
        """
        if sortBy not in SONG_SORT_COLUMNS:
            raise ValueError(f"Unknown sortBy: {sortBy!r}")
        sortColumn = SONG_SORT_COLUMNS[sortBy]
        direction = "ASC" if sortBy == "name" else "DESC"
        limitValue = -1 if limit is None else limit

        conn = self._conn()
        params = [username, startTs, startTs, endTs, endTs]
        extraClauses = ""
        if trackId is not None:
            extraClauses += " AND t.id = ?"
            params.append(trackId)
        if artistId is not None:
            extraClauses += " AND EXISTS (SELECT 1 FROM track_artists ta2 WHERE ta2.track_id = t.id AND ta2.artist_id = ?)"
            params.append(artistId)
        if albumId is not None:
            extraClauses += " AND al.id = ?"
            params.append(albumId)
        if searchQuery:
            pattern = self._likePattern(searchQuery)
            extraClauses += """ AND (
                t.name LIKE ? ESCAPE '\\'
                OR al.name LIKE ? ESCAPE '\\'
                OR EXISTS (
                    SELECT 1 FROM track_artists ta3 JOIN artists ar3 ON ar3.id = ta3.artist_id
                    WHERE ta3.track_id = t.id AND ar3.name LIKE ? ESCAPE '\\'
                )
            )"""
            params += [pattern, pattern, pattern]
        params += [limitValue, offset]

        rows = conn.execute(
            f"""
            SELECT
                t.id AS track_id, t.name AS name, t.url AS url, t.image_id AS image_id,
                t.duration_ms AS duration_ms, t.explicit AS explicit, t.isrc AS isrc,
                t.disc_number AS disc_number, t.track_number AS track_number,
                al.id AS album_id, al.name AS album_name, al.url AS album_url,
                al.total_tracks AS album_total_tracks, al.release_date AS album_release_date,
                al.image_id AS album_image_id, al.image_url AS album_image_url,
                COUNT(*) AS plays, SUM(p.time_played) AS total_time_listened,
                MIN(p.played_at) AS first_listened_at
            FROM plays p
            JOIN tracks t ON t.id = p.track_id
            LEFT JOIN albums al ON al.id = t.album_id
            WHERE p.username = ? {self._dateRangeClause().replace("played_at", "p.played_at")}{extraClauses}
            GROUP BY t.id
            ORDER BY {sortColumn} {direction}, total_time_listened {direction}, name COLLATE NOCASE {direction}, track_id ASC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()

        artistsByTrack = self._artistsForTracks([row["track_id"] for row in rows])
        return [self._songRowToDict(row, artistsByTrack.get(row["track_id"], [])) for row in rows]

    def getSongsCount(self, username: str, startTs: float | None = None, endTs: float | None = None,
                       searchQuery: str | None = None) -> int:
        """Number of distinct songs played in range - the paging counterpart to
        getSongsPage(), used to compute total page count without fetching every
        song's metadata."""
        conn = self._conn()
        if not searchQuery:
            # No name/artist/album lookup needed, so skip the joins entirely -
            # this stays exactly as cheap as before search support was added.
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM (
                    SELECT track_id FROM plays WHERE username = ? {self._dateRangeClause()}
                    GROUP BY track_id
                )
                """,
                (username, startTs, startTs, endTs, endTs),
            ).fetchone()
            return row["c"]

        pattern = self._likePattern(searchQuery)
        params = [username, startTs, startTs, endTs, endTs, pattern, pattern, pattern]
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT p.track_id FROM plays p
                JOIN tracks t ON t.id = p.track_id
                LEFT JOIN albums al ON al.id = t.album_id
                WHERE p.username = ? {self._dateRangeClause().replace("played_at", "p.played_at")}
                AND (
                    t.name LIKE ? ESCAPE '\\'
                    OR al.name LIKE ? ESCAPE '\\'
                    OR EXISTS (
                        SELECT 1 FROM track_artists ta JOIN artists ar ON ar.id = ta.artist_id
                        WHERE ta.track_id = t.id AND ar.name LIKE ? ESCAPE '\\'
                    )
                )
                GROUP BY p.track_id
            )
            """,
            params,
        ).fetchone()
        return row["c"]

    def getAlbumsPage(self, username: str, startTs: float | None = None, endTs: float | None = None,
                       sortBy: str = "plays", limit: int | None = None, offset: int = 0,
                       albumId: str | None = None, searchQuery: str | None = None) -> list[dict]:
        """Sorted/paged album stats in one batched round-trip - one row per
        album, aggregated across every track on it this user played. Mirrors
        getSongsPage()'s SQL-first sort/page pattern exactly.

        `albumId` narrows this to a single album - reused by album-detail pages
        to fetch that one album's own aggregate stats. `searchQuery` narrows to
        albums whose name or any artist on them matches - the artist check
        deliberately looks up every track on the album (`t2.album_id = al.id`)
        rather than just the current row's own track: unlike getSongsPage()
        (grouped by t.id, so every row in a group already shares one track),
        an album's rows span multiple different tracks, so filtering by the
        current row's track alone would silently drop that album's non-matching
        tracks from the aggregate instead of keeping the album's true totals.
        """
        if sortBy not in ALBUM_SORT_COLUMNS:
            raise ValueError(f"Unknown sortBy: {sortBy!r}")
        sortColumn = ALBUM_SORT_COLUMNS[sortBy]
        direction = "ASC" if sortBy == "name" else "DESC"
        limitValue = -1 if limit is None else limit

        conn = self._conn()
        params = [username, startTs, startTs, endTs, endTs]
        extraClauses = ""
        if albumId is not None:
            extraClauses += " AND al.id = ?"
            params.append(albumId)
        if searchQuery:
            pattern = self._likePattern(searchQuery)
            extraClauses += """ AND (
                al.name LIKE ? ESCAPE '\\'
                OR EXISTS (
                    SELECT 1 FROM tracks t2
                    JOIN track_artists ta2 ON ta2.track_id = t2.id
                    JOIN artists ar2 ON ar2.id = ta2.artist_id
                    WHERE t2.album_id = al.id AND ar2.name LIKE ? ESCAPE '\\'
                )
            )"""
            params += [pattern, pattern]
        params += [limitValue, offset]

        rows = conn.execute(
            f"""
            SELECT
                al.id AS album_id, al.name AS name, al.url AS url, al.image_id AS image_id,
                al.image_url AS image_url, al.total_tracks AS total_tracks, al.release_date AS release_date,
                COUNT(*) AS plays, SUM(p.time_played) AS total_time_listened,
                COUNT(DISTINCT p.track_id) AS unique_song_count, MIN(p.played_at) AS first_listened_at
            FROM plays p
            JOIN tracks t ON t.id = p.track_id
            JOIN albums al ON al.id = t.album_id
            WHERE p.username = ? {self._dateRangeClause().replace("played_at", "p.played_at")}{extraClauses}
            GROUP BY al.id
            ORDER BY {sortColumn} {direction}, total_time_listened {direction}, name COLLATE NOCASE {direction}, album_id ASC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()

        artistsByAlbum = self._artistsForAlbums([row["album_id"] for row in rows])
        return [self._albumStatsRowToDict(row, artistsByAlbum.get(row["album_id"], [])) for row in rows]

    def getAlbumsCount(self, username: str, startTs: float | None = None, endTs: float | None = None,
                        searchQuery: str | None = None) -> int:
        """Number of distinct albums played in range - the paging counterpart to
        getAlbumsPage(), used to compute total page count without fetching every
        album's metadata."""
        conn = self._conn()
        if not searchQuery:
            # No name/artist lookup needed, so skip the joins entirely - stays
            # exactly as cheap as before search support was added.
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM (
                    SELECT t.album_id FROM plays p
                    JOIN tracks t ON t.id = p.track_id
                    WHERE p.username = ? {self._dateRangeClause().replace("played_at", "p.played_at")}
                    GROUP BY t.album_id
                )
                """,
                (username, startTs, startTs, endTs, endTs),
            ).fetchone()
            return row["c"]

        pattern = self._likePattern(searchQuery)
        params = [username, startTs, startTs, endTs, endTs, pattern, pattern]
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT t.album_id FROM plays p
                JOIN tracks t ON t.id = p.track_id
                JOIN albums al ON al.id = t.album_id
                WHERE p.username = ? {self._dateRangeClause().replace("played_at", "p.played_at")}
                AND (
                    al.name LIKE ? ESCAPE '\\'
                    OR EXISTS (
                        SELECT 1 FROM tracks t2
                        JOIN track_artists ta2 ON ta2.track_id = t2.id
                        JOIN artists ar2 ON ar2.id = ta2.artist_id
                        WHERE t2.album_id = al.id AND ar2.name LIKE ? ESCAPE '\\'
                    )
                )
                GROUP BY t.album_id
            )
            """,
            params,
        ).fetchone()
        return row["c"]

    def _artistsForAlbums(self, albumIds: list[str]) -> dict[str, list[dict]]:
        """Distinct artists across every track on each album, grouped by album id
        and ordered by their earliest track position - the album-level
        counterpart to _artistsForTracks()."""
        if not albumIds:
            return {}
        conn = self._conn()
        placeholders = ",".join("?" for _ in albumIds)
        rows = conn.execute(
            f"""
            SELECT t.album_id AS album_id, a.id AS id, a.name AS name, a.url AS url, a.image_id AS image_id,
                   MIN(ta.position) AS min_position
            FROM track_artists ta
            JOIN artists a ON a.id = ta.artist_id
            JOIN tracks t ON t.id = ta.track_id
            WHERE t.album_id IN ({placeholders})
            GROUP BY t.album_id, a.id
            ORDER BY t.album_id, min_position
            """,
            albumIds,
        ).fetchall()
        artistsByAlbum: dict[str, list] = {}
        for row in rows:
            artistsByAlbum.setdefault(row["album_id"], []).append(
                {"id": row["id"], "name": row["name"], "url": row["url"], "imageUrl": "", "imageId": row["image_id"]}
            )
        return artistsByAlbum

    @staticmethod
    def _albumStatsRowToDict(row, artists: list[dict]) -> dict:
        return {
            "id": row["album_id"],
            "name": row["name"],
            "url": row["url"],
            "imageId": row["image_id"],
            "imageUrl": row["image_url"],
            "totalTracks": row["total_tracks"],
            "releaseDate": row["release_date"],
            "artists": artists,
            "plays": row["plays"],
            "totalTimeListened": row["total_time_listened"],
            "uniqueSongCount": row["unique_song_count"],
            "firstListenedAt": row["first_listened_at"],
        }

    def _artistsForTracks(self, trackIds: list[str]) -> dict[str, list[dict]]:
        """Ordered artists for a specific set of track ids, grouped by track id -
        the batched counterpart to the per-artist JOIN in getTrack()."""
        if not trackIds:
            return {}
        conn = self._conn()
        placeholders = ",".join("?" for _ in trackIds)
        rows = conn.execute(
            f"""
            SELECT ta.track_id AS track_id, a.id AS id, a.name AS name, a.url AS url, a.image_id AS image_id
            FROM track_artists ta
            JOIN artists a ON a.id = ta.artist_id
            WHERE ta.track_id IN ({placeholders})
            ORDER BY ta.track_id, ta.position
            """,
            trackIds,
        ).fetchall()
        artistsByTrack: dict[str, list] = {}
        for row in rows:
            artistsByTrack.setdefault(row["track_id"], []).append(
                {"id": row["id"], "name": row["name"], "url": row["url"], "imageUrl": "", "imageId": row["image_id"]}
            )
        return artistsByTrack

    @staticmethod
    def _songRowToDict(row, artists: list[dict]) -> dict:
        hasAlbum = row["album_id"] is not None
        return {
            "id": row["track_id"],
            "name": row["name"],
            "url": row["url"],
            "imageUrl": row["album_image_url"] if hasAlbum else "",
            "imageId": row["image_id"],
            "duration": row["duration_ms"],
            "explicit": bool(row["explicit"]),
            "isrc": row["isrc"] or "",
            "discNumber": row["disc_number"],
            "trackNumber": row["track_number"],
            "releaseDate": row["album_release_date"] if hasAlbum else None,
            "album": {
                "id": row["album_id"],
                "name": row["album_name"],
                "url": row["album_url"],
                "imageId": row["album_image_id"],
                "imageUrl": row["album_image_url"],
                "totalTracks": row["album_total_tracks"],
                "releaseDate": row["album_release_date"],
            } if hasAlbum else None,
            "artists": artists,
            "plays": row["plays"],
            "totalTimeListened": row["total_time_listened"],
            "firstListenedAt": row["first_listened_at"],
        }

    def getPlaysInRange(self, username: str, startTs: float | None = None, endTs: float | None = None,
                         trackId: str | None = None, artistId: str | None = None,
                         albumId: str | None = None) -> list[dict]:
        """Raw (playedAt, timePlayed) pairs for date-bucketed charts (time series,
        hour-of-day heatmap) - bucketing itself stays in Python since it depends on
        the app's configurable IANA timezone, which SQLite's date functions can't
        express correctly.

        `trackId`/`artistId`/`albumId` narrow this to one item's plays - reused
        by the song/artist/album detail pages' "play history over time" chart,
        which otherwise reads the exact same shape as the main Charts page."""
        conn = self._conn()
        params = [username, startTs, startTs, endTs, endTs]
        extraClauses = ""
        if trackId is not None:
            extraClauses += " AND track_id = ?"
            params.append(trackId)
        if artistId is not None:
            extraClauses += " AND EXISTS (SELECT 1 FROM track_artists ta WHERE ta.track_id = plays.track_id AND ta.artist_id = ?)"
            params.append(artistId)
        if albumId is not None:
            extraClauses += " AND EXISTS (SELECT 1 FROM tracks t WHERE t.id = plays.track_id AND t.album_id = ?)"
            params.append(albumId)

        rows = conn.execute(
            f"SELECT track_id, played_at, time_played FROM plays WHERE username = ? {self._dateRangeClause()}{extraClauses}",
            params,
        ).fetchall()
        return [{"id": r["track_id"], "playedAt": r["played_at"], "timePlayed": r["time_played"]} for r in rows]

    def getPlayArtistPairsInRange(self, username: str, startTs: float | None = None,
                                   endTs: float | None = None) -> list[dict]:
        """One row per (play, artist) pair - a play whose track has N artists
        yields N rows, matching the per-artist increment the old Python loop did."""
        conn = self._conn()
        rows = conn.execute(
            f"""
            SELECT p.played_at AS played_at, ar.name AS artist_name
            FROM plays p
            JOIN track_artists ta ON ta.track_id = p.track_id
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE p.username = ? {self._dateRangeClause().replace("played_at", "p.played_at")}
            """,
            (username, startTs, startTs, endTs, endTs),
        ).fetchall()
        return [{"playedAt": r["played_at"], "artistName": r["artist_name"]} for r in rows]

    def getPlayTotals(self, username: str, startTs: float | None = None,
                       endTs: float | None = None) -> tuple[int, int]:
        conn = self._conn()
        row = conn.execute(
            f"SELECT COUNT(*) AS c, COALESCE(SUM(time_played), 0) AS total FROM plays "
            f"WHERE username = ? {self._dateRangeClause()}",
            (username, startTs, startTs, endTs, endTs),
        ).fetchone()
        return row["c"], row["total"]

    def getDiscoveredSongsCount(self, username: str, startTs: float | None = None,
                                 endTs: float | None = None) -> int:
        """Count of distinct songs first played (across all time) within the year range."""
        conn = self._conn()
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT DISTINCT p.track_id
                FROM plays p
                WHERE p.username = ? AND p.played_at BETWEEN ? AND ?
                AND (SELECT MIN(played_at) FROM plays WHERE username = ? AND track_id = p.track_id)
                    BETWEEN ? AND ?
            )
            """,
            (username, startTs, endTs, username, startTs, endTs),
        ).fetchone()
        return row["c"]

    def getDiscoveredArtistsCount(self, username: str, startTs: float | None = None,
                                   endTs: float | None = None) -> int:
        """Count of distinct artists first played (across all time) within the year range."""
        conn = self._conn()
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT DISTINCT ta.artist_id
                FROM plays p
                JOIN track_artists ta ON ta.track_id = p.track_id
                WHERE p.username = ? AND p.played_at BETWEEN ? AND ?
                AND (SELECT MIN(played_at) FROM plays
                     WHERE username = ? AND track_id IN (
                         SELECT track_id FROM track_artists WHERE artist_id = ta.artist_id
                     ))
                    BETWEEN ? AND ?
            )
            """,
            (username, startTs, endTs, username, startTs, endTs),
        ).fetchone()
        return row["c"]

    # ---- Per-user: users / cookies ----------------------------------------------

    def upsertUser(self, username: str, email: str, createdAt: float | None = None) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO users (username, email, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(username) DO NOTHING",
                (username, email, createdAt if createdAt is not None else time.time()),
            )

    def getUsernameForEmail(self, email: str) -> str | None:
        conn = self._conn()
        row = conn.execute("SELECT username FROM users WHERE email=?", (email,)).fetchone()
        return row["username"] if row else None

    def usernameExists(self, username: str) -> bool:
        conn = self._conn()
        row = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
        return row is not None

    def getEmailForUsername(self, username: str) -> str | None:
        """The stored email for an existing username, or None - either because
        the username doesn't exist, or it exists but has no email on record yet
        (e.g. a migrated account whose users_map.json didn't know it). Callers
        that need to tell those two cases apart should check usernameExists()
        first."""
        conn = self._conn()
        row = conn.execute("SELECT email FROM users WHERE username=?", (username,)).fetchone()
        return row["email"] if row else None

    def setUserEmail(self, username: str, email: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute("UPDATE users SET email=? WHERE username=?", (email, username))

    def setUserCookies(self, username: str, cookies: dict) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET cookies_json=? WHERE username=?",
                (json.dumps(cookies), username),
            )

    def getUserCookies(self, username: str) -> dict | None:
        conn = self._conn()
        row = conn.execute("SELECT cookies_json FROM users WHERE username=?", (username,)).fetchone()
        if row is None or row["cookies_json"] is None:
            return None
        return json.loads(row["cookies_json"])

    def setUserPassword(self, username: str, passwordHash: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET password_hash=? WHERE username=?",
                (passwordHash, username),
            )

    def getUserPasswordHash(self, username: str) -> str | None:
        conn = self._conn()
        row = conn.execute("SELECT password_hash FROM users WHERE username=?", (username,)).fetchone()
        return row["password_hash"] if row else None

    def getUserSpotifyCredentials(self, username: str) -> dict | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT spotify_client_id, spotify_client_secret, spotify_refresh_token FROM users WHERE username=?",
            (username,)
        ).fetchone()
        if not row:
            return None
        return {
            "client_id": row["spotify_client_id"],
            "client_secret": row["spotify_client_secret"],
            "refresh_token": row["spotify_refresh_token"],
        }

    def updateUserSpotifyCredentials(self, username: str, clientId: str | None,
                                     clientSecret: str | None, refreshToken: str | None) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET spotify_client_id = ?, spotify_client_secret = ?, spotify_refresh_token = ? WHERE username = ?",
                (clientId, clientSecret, refreshToken, username)
            )

    def getUserSettings(self, username: str) -> dict:
        conn = self._conn()
        row = conn.execute(
            "SELECT default_dashboard_window, timezone FROM users WHERE username=?",
            (username,),
        ).fetchone()
        if row:
            return {
                "default_dashboard_window": row["default_dashboard_window"] or "day",
                "timezone": row["timezone"]
            }
        return {"default_dashboard_window": "day", "timezone": None}

    def updateUserSettings(self, username: str, default_dashboard_window: str, timezone: str | None) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET default_dashboard_window=?, timezone=? WHERE username=?",
                (default_dashboard_window, timezone, username),
            )

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

    def addUserSettingsColumnsIfMissing(self) -> None:
        """Add default_dashboard_window and timezone columns to users table if missing."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        with conn:
            if "default_dashboard_window" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN default_dashboard_window TEXT DEFAULT 'day'")
            if "timezone" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN timezone TEXT")

    def getAllUsersWithCookies(self) -> list[tuple[str, str]]:
        """(username, email) for every user who has logged in at least once -
        used at startup to make sure each of them has a running listener."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT username, email FROM users WHERE cookies_json IS NOT NULL"
        ).fetchall()
        return [(r["username"], r["email"]) for r in rows]

    # ---- Per-user: import progress ------------------------------------------------

    def writeProgress(self, username: str, status: str, current: int, total: int,
                       message: str, error: bool) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                """
                INSERT INTO import_progress (username, status, current, total, message, error)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    status=excluded.status, current=excluded.current, total=excluded.total,
                    message=excluded.message, error=excluded.error
                """,
                (username, status, current, total, message, int(error)),
            )

    def readProgress(self, username: str) -> dict | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT status, current, total, message, error FROM import_progress WHERE username=?",
            (username,),
        ).fetchone()
        if row is None:
            return None
        return {
            "status": row["status"],
            "current": row["current"],
            "total": row["total"],
            "percentage": round((row["current"] / row["total"] * 100) if row["total"] else 0),
            "message": row["message"],
            "error": bool(row["error"]),
        }

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

        return {
            "tracks": tracks_count,
            "artists": artists_count,
            "albums": albums_count,
            "plays": plays_count,
            "total_time_ms": total_time_ms,
            "db_size_bytes": db_size,
        }

    def getAllUsersDetails(self) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT username, email, cookies_json, spotify_client_id, spotify_refresh_token, created_at FROM users"
        ).fetchall()
        return [dict(r) for r in rows]

    def getAlbumsMissingMetadata(self, limit: int) -> list[str]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT id FROM albums WHERE (release_date = 0 OR release_date IS NULL) AND total_tracks = 0 LIMIT ?",
            (limit,)
        ).fetchall()
        return [row["id"] for row in rows]

    def updateAlbumMetadata(self, album_id: str, release_date: float, total_tracks: int, name: str | None = None) -> None:
        conn = self._conn()
        with conn:
            if name:
                conn.execute(
                    "UPDATE albums SET release_date = ?, total_tracks = ?, name = ? WHERE id = ?",
                    (release_date, total_tracks, name, album_id)
                )
            else:
                conn.execute(
                    "UPDATE albums SET release_date = ?, total_tracks = ? WHERE id = ?",
                    (release_date, total_tracks, album_id)
                )

    def updateTrackName(self, track_id: str, name: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE tracks SET name = ? WHERE id = ?",
                (name, track_id)
            )

    def cleanupOrphans(self) -> dict[str, int]:
        """Deletes tracks, track-artists, albums, artists, and images that are no longer
        referenced by any plays. Returns the count of deleted rows per category."""
        conn = self._conn()
        deleted = {}
        with conn:
            # 1. Delete orphaned track_artists (tracks with no plays)
            cur = conn.execute("DELETE FROM track_artists WHERE track_id NOT IN (SELECT DISTINCT track_id FROM plays)")
            deleted["track_artists"] = cur.rowcount

            # 2. Delete orphaned tracks (not in plays)
            cur = conn.execute("DELETE FROM tracks WHERE id NOT IN (SELECT DISTINCT track_id FROM plays)")
            deleted["tracks"] = cur.rowcount

            # 3. Delete orphaned albums (no tracks reference them)
            cur = conn.execute("DELETE FROM albums WHERE id NOT IN (SELECT DISTINCT album_id FROM tracks)")
            deleted["albums"] = cur.rowcount

            # 4. Delete orphaned artists (no track_artists reference them)
            cur = conn.execute("DELETE FROM artists WHERE id NOT IN (SELECT DISTINCT artist_id FROM track_artists)")
            deleted["artists"] = cur.rowcount

            # 5. Delete orphaned images (no longer referenced by tracks, albums, or artists)
            cur = conn.execute("""
                DELETE FROM images 
                WHERE id NOT IN (
                    SELECT DISTINCT image_id FROM albums WHERE image_id IS NOT NULL
                    UNION
                    SELECT DISTINCT image_id FROM artists WHERE image_id IS NOT NULL
                    UNION
                    SELECT DISTINCT image_id FROM tracks WHERE image_id IS NOT NULL
                )
            """)
            deleted["images"] = cur.rowcount
            
        return deleted

    def getMaxPlayedAtInPeriod(self, username: str, startTs: float, endTs: float) -> float | None:
        row = self._conn().execute(
            "SELECT MAX(played_at) FROM plays WHERE username = ? AND played_at >= ? AND played_at < ?",
            (username, startTs, endTs)
        ).fetchone()
        return row[0] if row else None

    def getPlayCountInPeriod(self, username: str, startTs: float, endTs: float) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) FROM plays WHERE username = ? AND played_at >= ? AND played_at < ?",
            (username, startTs, endTs)
        ).fetchone()
        return row[0] if row else 0

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

