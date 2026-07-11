from __future__ import annotations
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

    def upsertTrack(self, track: dict) -> None:
        """Upsert a track and its nested album/artists (as produced by
        Client.formatTrack). Last write wins, matching the previous
        tracks[id] = track dict-assignment semantics.

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

        conn.execute(
            """
            INSERT INTO tracks (id, name, url, album_id, image_id, duration_ms, explicit, isrc, disc_number, track_number)
            VALUES (:id, :name, :url, :albumId, :imageId, :duration, :explicit, :isrc, :discNumber, :trackNumber)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, url=excluded.url, album_id=excluded.album_id, image_id=excluded.image_id,
                duration_ms=excluded.duration_ms, explicit=excluded.explicit, isrc=excluded.isrc,
                disc_number=excluded.disc_number, track_number=excluded.track_number
            """,
            {**track, "albumId": album["id"], "explicit": bool(track.get("explicit", False))},
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
            cur = conn.execute(
                """
                INSERT INTO images (id, kind, status) VALUES (?, ?, ?)
                ON CONFLICT(id, kind) DO UPDATE SET status=excluded.status
                WHERE images.status=?
                """,
                (imageId, kind, IMAGE_STATUS_PENDING, IMAGE_STATUS_FAILED),
            )
            return cur.rowcount > 0

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
                   playedFrom: str | None = None) -> bool:
        """Returns True if a new row was inserted, False if this exact
        (username, trackId, playedAt) play was already recorded.

        Does NOT commit - see upsertTrack()'s docstring."""
        conn = self._conn()
        cur = conn.execute(
            "INSERT OR IGNORE INTO plays (username, track_id, played_at, time_played, played_from) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, trackId, playedAt, timePlayed, playedFrom),
        )
        return cur.rowcount > 0

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

    @staticmethod
    def _playRowToEntry(row) -> dict:
        return {
            "id": row["track_id"],
            "playedAt": row["played_at"],
            "timePlayed": row["time_played"],
            "playedFrom": row["played_from"],
        }

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
                             endTs: float | None = None) -> list[dict]:
        """One row per artist who appears on at least one played track, grouped by
        artist id (not name - two different artists that happen to share a display
        name are no longer merged, unlike the old name-keyed in-memory grouping)."""
        conn = self._conn()
        rows = conn.execute(
            f"""
            SELECT ar.id AS id, ar.name AS name, ar.url AS url, ar.image_id AS image_id,
                   COUNT(*) AS plays, SUM(p.time_played) AS total_time_listened,
                   MIN(p.played_at) AS first_listened_at, COUNT(DISTINCT p.track_id) AS unique_song_count
            FROM plays p
            JOIN track_artists ta ON ta.track_id = p.track_id
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE p.username = ? {self._dateRangeClause().replace("played_at", "p.played_at")}
            GROUP BY ar.id
            """,
            (username, startTs, startTs, endTs, endTs),
        ).fetchall()
        return [
            {
                "id": r["id"], "name": r["name"], "url": r["url"], "imageUrl": "", "imageId": r["image_id"],
                "plays": r["plays"], "totalTimeListened": r["total_time_listened"],
                "uniqueSongCount": r["unique_song_count"], "firstListenedAt": r["first_listened_at"],
            }
            for r in rows
        ]

    def getPlaysInRange(self, username: str, startTs: float | None = None,
                         endTs: float | None = None) -> list[dict]:
        """Raw (playedAt, timePlayed) pairs for date-bucketed charts (time series,
        hour-of-day heatmap) - bucketing itself stays in Python since it depends on
        the app's configurable IANA timezone, which SQLite's date functions can't
        express correctly."""
        conn = self._conn()
        rows = conn.execute(
            f"SELECT played_at, time_played FROM plays WHERE username = ? {self._dateRangeClause()}",
            (username, startTs, startTs, endTs, endTs),
        ).fetchall()
        return [{"playedAt": r["played_at"], "timePlayed": r["time_played"]} for r in rows]

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
