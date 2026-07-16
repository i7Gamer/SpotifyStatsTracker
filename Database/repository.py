from __future__ import annotations
import datetime
import json
import threading
import time
from pathlib import Path

try:
    import Database.db as db
    from Database.db import ConnectionManager, SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON
    from Database.secret_store import encryptSecret, decryptSecret, isEncrypted
except ModuleNotFoundError:
    import db
    from db import ConnectionManager, SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON
    from secret_store import encryptSecret, decryptSecret, isEncrypted

IMAGE_KIND_TRACK = "track"
IMAGE_KIND_ARTIST = "artist"
IMAGE_STATUS_PENDING = "pending"
IMAGE_STATUS_OK = "ok"
IMAGE_STATUS_FAILED = "failed"

# How long the metadata backfiller waits before re-attempting an album it already
# processed - covers restricted/blanked albums whose metadata Spotify may fill in
# (or unblock) later, without hammering the API for permanently dateless albums.
ALBUM_BACKFILL_RETRY_SECONDS = 7 * 24 * 3600

# getBucketedPlayTotals' fixed UTC bucket width. 15 minutes is the smallest
# granularity any real-world UTC offset uses (e.g. Asia/Kathmandu +5:45), so
# every play in one bucket maps to the same local day/hour/weekday no matter
# which IANA timezone Python later applies - which is what lets the heavy
# per-play aggregation move into SQL without losing timezone correctness.
PLAY_BUCKET_SECONDS = 15 * 60

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
        tracks[id] = track dict-assignment semantics - with one exception: a
        fallback record (SYNTHETIC_FALLBACK_REASON / RESTRICTED_FALLBACK_REASON)
        never overwrites a row that already has real metadata; it only heals
        blanked rows or refreshes other fallback rows. If created_reason is
        provided, it's only set on INSERT (never updated on conflict) - except
        that real metadata replacing a fallback row also replaces the fallback
        marker.

        Does NOT commit - callers compose this with insertPlay() into a single
        transaction (one play = one commit; a bulk import = one commit for the
        whole batch), then call commit()/rollback() themselves."""
        conn = self._conn()

        # Defense in depth: the importer normally prevents a fallback record
        # from ever targeting a track with real metadata (its known-track index
        # resolves those first), but no caller may rely on that - degraded data
        # must never clobber good catalog data at this level either.
        if track.get("created_reason") in (SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON):
            existing = conn.execute(
                "SELECT name, created_reason FROM tracks WHERE id=?", (track["id"],)
            ).fetchone()
            if existing and existing["name"] and existing["created_reason"] not in (
                SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON,
            ):
                return

        album = track.get("album")
        if not album:
            album_id = track.get("albumId") or f"album_{track['id']}"
            album = {
                "id": album_id,
                "name": track.get("name", "Unknown Album"),
                "url": "",
                "totalTracks": 1,
                "releaseDate": 0.0,
                "imageUrl": "",
            }
        artists = track.get("artists") or []
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
            "created_at": track.get("created_at"),
            "created_reason": track.get("created_reason"),
            "availability_reason": track.get("availability_reason"),
            "syntheticReason": SYNTHETIC_FALLBACK_REASON,
            "restrictedReason": RESTRICTED_FALLBACK_REASON,
        }
        if created_reason and not trackData["created_reason"]:
            trackData["created_reason"] = created_reason
        if trackData["created_reason"] and trackData["created_at"] is None:
            trackData["created_at"] = time.time()

        # created_at/created_reason are never updated on conflict, with one exception:
        # a fallback row (synthetic or restricted, see db.py) being overwritten by real
        # metadata drops the fallback marker, so the UI stops badging a track that
        # turned out to be fully available on Spotify after all.
        conn.execute(
            """
            INSERT INTO tracks (id, name, url, album_id, image_id, duration_ms, explicit, isrc, disc_number, track_number, created_at, created_reason, availability_reason)
            VALUES (:id, :name, :url, :albumId, :imageId, :duration, :explicit, :isrc, :discNumber, :trackNumber, :created_at, :created_reason, :availability_reason)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, url=excluded.url, album_id=excluded.album_id, image_id=excluded.image_id,
                duration_ms=excluded.duration_ms, explicit=excluded.explicit, isrc=excluded.isrc,
                disc_number=excluded.disc_number, track_number=excluded.track_number,
                availability_reason=excluded.availability_reason,
                created_at=CASE
                    WHEN tracks.created_reason IN (:syntheticReason, :restrictedReason)
                         AND (excluded.created_reason IS NULL
                              OR excluded.created_reason NOT IN (:syntheticReason, :restrictedReason))
                    THEN excluded.created_at ELSE tracks.created_at END,
                created_reason=CASE
                    WHEN tracks.created_reason IN (:syntheticReason, :restrictedReason)
                         AND (excluded.created_reason IS NULL
                              OR excluded.created_reason NOT IN (:syntheticReason, :restrictedReason))
                    THEN excluded.created_reason ELSE tracks.created_reason END
            """,
            trackData,
        )

        # An empty artists list means the caller had no artist data - not that
        # the track lost its artists. Keep whatever links are already recorded
        # instead of wiping them.
        if artists:
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
            "created_reason": trackRow["created_reason"],
            "availability_reason": trackRow["availability_reason"],
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

    def deleteStalePendingImages(self) -> int:
        """Forget every 'pending' download claim. Only safe at process startup,
        before any download can be in flight: a pending row surviving from a
        previous run means the claimer died (crash, or its status write failed
        against a locked database) - tryClaimImageDownload would refuse to
        reclaim it forever, leaving that artwork permanently missing. Deleted
        rather than marked failed: lazyFetchArtistImage treats 'failed' as
        permanent, while a missing row means never-attempted, so both the
        track and artist paths retry naturally. Returns the number of claims
        cleared."""
        conn = self._conn()
        with conn:
            cur = conn.execute("DELETE FROM images WHERE status=?", (IMAGE_STATUS_PENDING,))
            return cur.rowcount

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

    def getPlaysWithSourceInRange(self, username: str, startTs: float, endTs: float) -> list[dict]:
        """Plays in the closed [startTs, endTs] window including their
        created_reason. The Web API reconciliation needs the source to
        guarantee it only deletes provable double-recordings (a backfill row
        next to a row from another source) - proximity alone is not proof,
        since real exports contain genuine same-track plays seconds apart
        (skip, then restart)."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT track_id, played_at, time_played, created_reason FROM plays "
            "WHERE username=? AND played_at BETWEEN ? AND ?",
            (username, startTs, endTs),
        ).fetchall()
        return [
            {
                "id": r["track_id"],
                "playedAt": r["played_at"],
                "timePlayed": r["time_played"],
                "createdReason": r["created_reason"],
            }
            for r in rows
        ]

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
    def _dateRangeClause(params: list, startTs: float | None, endTs: float | None,
                          column: str = "played_at") -> str:
        """Half-open [startTs, endTs) range, matching app.py's _getDateRange()
        documented half-open interval - a play landing exactly on endTs
        belongs to the next adjacent range, not this one. (The endTs bound
        used to be inclusive, which double-counted a boundary play into both
        a period and the immediately-following one, e.g. getOverallStats()'s
        current vs. previous period comparison.)

        Emits only the conditions whose bound exists, appending the bound
        values to `params` in clause order. The previous static
        '(? IS NULL OR played_at >= ?)' form was non-sargable: SQLite can't
        use played_at as an index range bound through the OR, so every
        ranged query walked the user's whole play history via the username
        index prefix instead of range-scanning (username, played_at)."""
        clause = ""
        if startTs is not None:
            clause += f" AND {column} >= ?"
            params.append(startTs)
        if endTs is not None:
            clause += f" AND {column} < ?"
            params.append(endTs)
        return clause

    def getPlayAggregatesByTrack(self, username: str, startTs: float | None = None,
                                  endTs: float | None = None) -> list[dict]:
        conn = self._conn()
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs)
        rows = conn.execute(
            f"""
            SELECT track_id, COUNT(*) AS plays, SUM(time_played) AS total_time_listened,
                   MIN(played_at) AS first_listened_at
            FROM plays
            WHERE username = ?{rangeClause}
            GROUP BY track_id
            """,
            params,
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
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
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
            WHERE p.username = ?{rangeClause}{extraClauses}
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
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
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
                WHERE p.username = ?{rangeClause}{searchClause}
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
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
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
                WHERE p.username = ?{rangeClause}
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
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
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
                t.created_reason AS created_reason, t.availability_reason AS availability_reason,
                al.id AS album_id, al.name AS album_name, al.url AS album_url,
                al.total_tracks AS album_total_tracks, al.release_date AS album_release_date,
                al.image_id AS album_image_id, al.image_url AS album_image_url,
                COUNT(*) AS plays, SUM(p.time_played) AS total_time_listened,
                MIN(p.played_at) AS first_listened_at
            FROM plays p
            JOIN tracks t ON t.id = p.track_id
            LEFT JOIN albums al ON al.id = t.album_id
            WHERE p.username = ?{rangeClause}{extraClauses}
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
            params = [username]
            rangeClause = self._dateRangeClause(params, startTs, endTs)
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM (
                    SELECT track_id FROM plays WHERE username = ?{rangeClause}
                    GROUP BY track_id
                )
                """,
                params,
            ).fetchone()
            return row["c"]

        pattern = self._likePattern(searchQuery)
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        params += [pattern, pattern, pattern]
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT p.track_id FROM plays p
                JOIN tracks t ON t.id = p.track_id
                LEFT JOIN albums al ON al.id = t.album_id
                WHERE p.username = ?{rangeClause}
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
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
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
            WHERE p.username = ?{rangeClause}{extraClauses}
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
            params = [username]
            rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM (
                    SELECT t.album_id FROM plays p
                    JOIN tracks t ON t.id = p.track_id
                    WHERE p.username = ?{rangeClause}
                    GROUP BY t.album_id
                )
                """,
                params,
            ).fetchone()
            return row["c"]

        pattern = self._likePattern(searchQuery)
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        params += [pattern, pattern]
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c FROM (
                SELECT t.album_id FROM plays p
                JOIN tracks t ON t.id = p.track_id
                JOIN albums al ON al.id = t.album_id
                WHERE p.username = ?{rangeClause}
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
            "created_reason": row["created_reason"],
            "availability_reason": row["availability_reason"],
        }

    def _itemFilterClauses(self, params: list, trackId: str | None, artistId: str | None,
                            albumId: str | None) -> str:
        """The shared track/artist/album narrowing used by the play-scan
        queries; appends the bound values to `params` in clause order."""
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
        return extraClauses

    def getBucketedPlayTotals(self, username: str, startTs: float | None = None,
                               endTs: float | None = None, trackId: str | None = None,
                               artistId: str | None = None, albumId: str | None = None) -> list[dict]:
        """Play count and listened time summed per fixed PLAY_BUCKET_SECONDS
        UTC bucket, ordered by bucket start - the SQL half of the
        date-bucketed charts (time series, heatmap, streak/peak-day stats).
        The buckets are deliberately timezone-agnostic: callers map each
        bucket's start timestamp to the app's configurable IANA timezone in
        Python, which SQLite's date functions can't express correctly, while
        SQL does the per-play heavy lifting (see PLAY_BUCKET_SECONDS for why
        the mapping is lossless).

        `trackId`/`artistId`/`albumId` narrow this to one item's plays -
        reused by the song/artist/album detail pages' "play history over
        time" chart and heatmap."""
        conn = self._conn()
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs)
        extraClauses = self._itemFilterClauses(params, trackId, artistId, albumId)

        rows = conn.execute(
            f"""
            SELECT CAST(played_at / {PLAY_BUCKET_SECONDS} AS INTEGER) AS bucket,
                   COUNT(*) AS plays,
                   COALESCE(SUM(time_played), 0) AS total_time
            FROM plays WHERE username = ?{rangeClause}{extraClauses}
            GROUP BY bucket
            ORDER BY bucket
            """,
            params,
        ).fetchall()
        return [{"bucketStartTs": r["bucket"] * PLAY_BUCKET_SECONDS,
                 "plays": r["plays"],
                 "totalTimeListened": r["total_time"]} for r in rows]

    def getBucketedArtistPlayCounts(self, username: str, startTs: float | None = None,
                                     endTs: float | None = None) -> list[dict]:
        """Play counts per (fixed PLAY_BUCKET_SECONDS UTC bucket, artist name)
        - the SQL half of the artist-trend chart, replacing a row-per-
        (play, artist) transfer. A play whose track has N artists still
        counts once per artist, and grouping by artist NAME (not id) matches
        the Python aggregation this replaces: same-named artists merge either
        way. Ordered by bucket so callers iterate in play-time order."""
        conn = self._conn()
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        rows = conn.execute(
            f"""
            SELECT CAST(p.played_at / {PLAY_BUCKET_SECONDS} AS INTEGER) AS bucket,
                   ar.name AS artist_name,
                   COUNT(*) AS plays
            FROM plays p
            JOIN track_artists ta ON ta.track_id = p.track_id
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE p.username = ?{rangeClause}
            GROUP BY bucket, ar.name
            ORDER BY bucket, ar.name
            """,
            params,
        ).fetchall()
        return [{"bucketStartTs": r["bucket"] * PLAY_BUCKET_SECONDS,
                 "artistName": r["artist_name"],
                 "plays": r["plays"]} for r in rows]

    def getPlayTotals(self, username: str, startTs: float | None = None,
                       endTs: float | None = None) -> tuple[int, int]:
        conn = self._conn()
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs)
        row = conn.execute(
            f"SELECT COUNT(*) AS c, COALESCE(SUM(time_played), 0) AS total FROM plays "
            f"WHERE username = ?{rangeClause}",
            params,
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

    def getUsernameForEmailCaseInsensitive(self, email: str) -> str | None:
        """getUsernameForEmail with case-insensitive matching - emails are
        stored as typed at login, so an ADMIN_EMAIL differing only in case
        must still resolve. ASCII-only folding (SQLite NOCASE), which is all
        email addresses need."""
        conn = self._conn()
        row = conn.execute(
            "SELECT username FROM users WHERE email=? COLLATE NOCASE", (email,)
        ).fetchone()
        return row["username"] if row else None

    def setUserEmail(self, username: str, email: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute("UPDATE users SET email=? WHERE username=?", (email, username))

    def setUserCookies(self, username: str, cookies: dict) -> None:
        # Encrypted at rest: these are a live Spotify session - see
        # Database/secret_store.py.
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET cookies_json=? WHERE username=?",
                (encryptSecret(json.dumps(cookies)), username),
            )

    def getUserCookies(self, username: str) -> dict | None:
        conn = self._conn()
        row = conn.execute("SELECT cookies_json FROM users WHERE username=?", (username,)).fetchone()
        if row is None or row["cookies_json"] is None:
            return None
        # Legacy plaintext rows pass through decryptSecret unchanged; an
        # undecryptable row (rotated/lost key) reads as "no cookies stored",
        # which routes the user through re-login instead of crashing.
        decrypted = decryptSecret(row["cookies_json"])
        if decrypted is None:
            return None
        return json.loads(decrypted)

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
            "client_secret": decryptSecret(row["spotify_client_secret"]),
            "refresh_token": decryptSecret(row["spotify_refresh_token"]),
        }

    def updateUserSpotifyCredentials(self, username: str, clientId: str | None,
                                     clientSecret: str | None, refreshToken: str | None) -> None:
        # The client id is public (it appears in the OAuth authorize URL);
        # the secret and refresh token are encrypted at rest - see
        # Database/secret_store.py.
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET spotify_client_id = ?, spotify_client_secret = ?, spotify_refresh_token = ? WHERE username = ?",
                (clientId,
                 encryptSecret(clientSecret) if clientSecret else clientSecret,
                 encryptSecret(refreshToken) if refreshToken else refreshToken,
                 username)
            )

    def encryptStoredSecretsIfPlaintext(self) -> int:
        """Encrypt any users-table secret still stored as plaintext (rows
        written before encryption existed) - the 1.16.0 -> 1.17.0 migration.
        Already-encrypted and empty values are left untouched, so re-running
        is safe. Returns the number of users updated. Does NOT commit - the
        caller (migrator) owns the transaction."""
        conn = self._conn()
        secretColumns = ("cookies_json", "spotify_client_secret", "spotify_refresh_token")
        updated = 0
        for row in conn.execute(f"SELECT username, {', '.join(secretColumns)} FROM users").fetchall():
            changes = {column: encryptSecret(row[column])
                       for column in secretColumns
                       if row[column] and not isEncrypted(row[column])}
            if changes:
                setClause = ", ".join(f"{column}=?" for column in changes)
                conn.execute(f"UPDATE users SET {setClause} WHERE username=?",
                             (*changes.values(), row["username"]))
                updated += 1
        return updated

    # ---- Per-user: admin role -------------------------------------------------
    # Single-admin model (see docs/proposal-admin-and-share-links.md): the
    # earliest-created user is promoted when no admin exists, and app.py's
    # ADMIN_EMAIL bootstrap is the explicit/recovery path. There's
    # deliberately no in-app grant/revoke UI.

    def isAdmin(self, username: str | None) -> bool:
        if not username:
            return False
        conn = self._conn()
        row = conn.execute("SELECT is_admin FROM users WHERE username=?", (username,)).fetchone()
        return bool(row["is_admin"]) if row else False

    def setUserAdmin(self, username: str, isAdmin: bool) -> None:
        conn = self._conn()
        with conn:
            conn.execute("UPDATE users SET is_admin=? WHERE username=?", (1 if isAdmin else 0, username))

    def getAdminUsernames(self) -> list[str]:
        conn = self._conn()
        rows = conn.execute("SELECT username FROM users WHERE is_admin=1 ORDER BY username").fetchall()
        return [r["username"] for r in rows]

    def promoteEarliestUserToAdminIfNoneExists(self) -> str | None:
        """Promote the earliest-created user (whoever set the instance up) to
        admin - only when no admin exists at all, so re-running (every app
        startup, plus migration 1.17.0) never creates a second admin or
        overrides a deliberate reassignment. Returns the promoted username,
        or None if nothing changed."""
        conn = self._conn()
        with conn:
            if conn.execute("SELECT 1 FROM users WHERE is_admin=1 LIMIT 1").fetchone():
                return None
            row = conn.execute(
                "SELECT username FROM users ORDER BY created_at ASC, username ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE users SET is_admin=1 WHERE username=?", (row["username"],))
            return row["username"]

    def addUserIsAdminColumnIfMissing(self) -> None:
        """SCHEMA's CREATE TABLE IF NOT EXISTS only shapes brand-new databases -
        a users table that already existed before is_admin was added needs an
        explicit ALTER TABLE (migrate1_17_0). Guarded so re-running the
        migration against an already-migrated database doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "is_admin" not in columns:
            with conn:
                conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")

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

    # ---- Per-user: mutual data-sharing --------------------------------------

    # user_shares.status values (mirrors the IMAGE_STATUS_* convention above;
    # the CHECK constraint in Database/db.py's SCHEMA lists the same literals).
    SHARE_STATUS_PENDING = "pending"
    SHARE_STATUS_ACCEPTED = "accepted"

    # Serializes createShareRequest's check-then-insert: two crossing requests
    # (A->B and B->A) on different Waitress threads could otherwise both pass
    # the reverse-pending check before either INSERT lands, leaving two
    # opposite-direction pending rows the same-direction UNIQUE constraint
    # doesn't cover. Class-level so every Repository instance over the shared
    # database file serializes on the same lock (single-process deployment,
    # like the in-memory rate limiter in app.py).
    _shareWriteLock = threading.Lock()

    def createShareRequest(self, requester: str, recipient: str) -> str:
        """Outcome as a string the caller can word a message around:
        "requested" (new pending row), "already_requested" (this exact request
        was already pending - nothing changed), "accepted" (a reverse-direction
        pending request existed, so this counts as accepting it), or
        "already_accepted" (the two already share - nothing changed)."""
        with self._shareWriteLock:
            conn = self._conn()
            with conn:
                if conn.execute(
                    "SELECT 1 FROM user_shares WHERE status=? AND "
                    "((requester_username=? AND recipient_username=?) OR (requester_username=? AND recipient_username=?))",
                    (self.SHARE_STATUS_ACCEPTED, requester, recipient, recipient, requester),
                ).fetchone():
                    return "already_accepted"

                reverseRow = conn.execute(
                    "SELECT id FROM user_shares WHERE requester_username=? AND recipient_username=? AND status=?",
                    (recipient, requester, self.SHARE_STATUS_PENDING),
                ).fetchone()
                if reverseRow:
                    conn.execute(
                        "UPDATE user_shares SET status=?, responded_at=? WHERE id=?",
                        (self.SHARE_STATUS_ACCEPTED, time.time(), reverseRow["id"]),
                    )
                    return "accepted"

                cursor = conn.execute(
                    "INSERT INTO user_shares (requester_username, recipient_username, status, created_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(requester_username, recipient_username) DO NOTHING",
                    (requester, recipient, self.SHARE_STATUS_PENDING, time.time()),
                )
                return "requested" if cursor.rowcount > 0 else "already_requested"

    def getPendingIncomingShares(self, username: str) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, requester_username, created_at FROM user_shares "
            "WHERE recipient_username=? AND status=? ORDER BY created_at, id",
            (username, self.SHARE_STATUS_PENDING),
        ).fetchall()
        return [dict(r) for r in rows]

    def getPendingIncomingSharesCount(self, username: str) -> int:
        """Just the count, for the topbar badge - avoids fetching full rows
        (requester_username/created_at) when the caller only needs a number."""
        row = self._conn().execute(
            "SELECT COUNT(*) AS c FROM user_shares WHERE recipient_username=? AND status=?",
            (username, self.SHARE_STATUS_PENDING),
        ).fetchone()
        return row["c"]

    def getUnseenAcceptedShareCount(self, username: str) -> int:
        """How many of `username`'s share REQUESTS (not requests they
        received) were accepted since they last visited /profile - the
        recipient doesn't get one of these, since accepting is itself their
        acknowledgment."""
        row = self._conn().execute(
            "SELECT COUNT(*) AS c FROM user_shares WHERE requester_username=? AND status=? AND requester_seen_accepted=0",
            (username, self.SHARE_STATUS_ACCEPTED),
        ).fetchone()
        return row["c"]

    def markAcceptedSharesSeenByRequester(self, username: str) -> None:
        """Clears the "your share request was accepted" notification - called
        when `username` visits /profile, where the newly-active share is
        actually visible in their Active Shares list."""
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE user_shares SET requester_seen_accepted=1 WHERE requester_username=? AND status=? AND requester_seen_accepted=0",
                (username, self.SHARE_STATUS_ACCEPTED),
            )

    def getPendingOutgoingShares(self, username: str) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, recipient_username, created_at FROM user_shares "
            "WHERE requester_username=? AND status=? ORDER BY created_at, id",
            (username, self.SHARE_STATUS_PENDING),
        ).fetchall()
        return [dict(r) for r in rows]

    def getAcceptedShareUsernames(self, username: str) -> list[str]:
        """The other username on each of `username`'s accepted shares,
        regardless of which side originally sent the request. Used where only
        the counterpart names matter (the Compare page's authorized-user set) -
        see getAcceptedShares() for the id-bearing version a "Revoke" button
        needs."""
        return [share["counterpart"] for share in self.getAcceptedShares(username)]

    def getAcceptedShares(self, username: str) -> list[dict]:
        """[{id, counterpart}] for each of `username`'s accepted shares,
        ordered by counterpart name so pickers and lists render stably -
        SQLite's row order is otherwise unspecified, which would make e.g.
        the Compare page's default counterpart flap between requests."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, CASE WHEN requester_username=? THEN recipient_username ELSE requester_username END AS counterpart "
            "FROM user_shares WHERE status=? AND (requester_username=? OR recipient_username=?) "
            "ORDER BY counterpart",
            (username, self.SHARE_STATUS_ACCEPTED, username, username),
        ).fetchall()
        return [dict(r) for r in rows]

    def hasAnyAcceptedShare(self, username: str) -> bool:
        """True iff `username` has at least one accepted share whose
        counterpart also has stored cookies - the exact set of shares the
        Compare page can actually load (it skips cookie-less counterparts),
        so the nav link this backs never points at a page that would 404.
        LIMIT 1 existence check: this runs on every template render (see
        app.py's _injectShareStatus)."""
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM user_shares us "
            "JOIN users u ON u.username = CASE WHEN us.requester_username=? THEN us.recipient_username ELSE us.requester_username END "
            "WHERE us.status=? AND (us.requester_username=? OR us.recipient_username=?) "
            "AND u.cookies_json IS NOT NULL LIMIT 1",
            (username, self.SHARE_STATUS_ACCEPTED, username, username),
        ).fetchone()
        return row is not None

    def respondToShareRequest(self, shareId: int, actingUsername: str, accept: bool) -> bool:
        """Only the recipient of a still-pending request may respond. Returns
        whether a row was actually affected, so the caller can tell "not
        found/not yours/already resolved" apart from "done"."""
        conn = self._conn()
        with conn:
            if accept:
                cursor = conn.execute(
                    "UPDATE user_shares SET status=?, responded_at=? "
                    "WHERE id=? AND recipient_username=? AND status=?",
                    (self.SHARE_STATUS_ACCEPTED, time.time(), shareId, actingUsername, self.SHARE_STATUS_PENDING),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM user_shares WHERE id=? AND recipient_username=? AND status=?",
                    (shareId, actingUsername, self.SHARE_STATUS_PENDING),
                )
            return cursor.rowcount > 0

    def cancelShareRequest(self, shareId: int, requesterUsername: str) -> bool:
        """Only the original requester may cancel their own still-pending
        request."""
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                "DELETE FROM user_shares WHERE id=? AND requester_username=? AND status=?",
                (shareId, requesterUsername, self.SHARE_STATUS_PENDING),
            )
            return cursor.rowcount > 0

    def revokeShare(self, shareId: int, actingUsername: str) -> bool:
        """Either party to an already-accepted share may end it unilaterally -
        deleting the row ends mutual access for both sides, not just the
        acting user's own view."""
        conn = self._conn()
        with conn:
            cursor = conn.execute(
                "DELETE FROM user_shares WHERE id=? AND status=? AND (requester_username=? OR recipient_username=?)",
                (shareId, self.SHARE_STATUS_ACCEPTED, actingUsername, actingUsername),
            )
            return cursor.rowcount > 0

    def getAllUsernamesExcept(self, username: str) -> list[str]:
        """Plain username list for a "who can I request a share with" picker -
        deliberately narrower than getAllUsersDetails(), which also selects
        cookies_json/spotify_refresh_token that this list has no reason to
        touch."""
        conn = self._conn()
        rows = conn.execute("SELECT username FROM users WHERE username != ? ORDER BY username", (username,)).fetchall()
        return [r["username"] for r in rows]

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

    def addAvailabilityColumnsIfMissing(self) -> None:
        """Add tracks.availability_reason (Spotify playability restriction, e.g.
        COUNTRY_RESTRICTED) and albums.backfill_attempted_at (backfill retry
        rate-limiting) if missing. Guarded so re-running doesn't fail."""
        conn = self._conn()
        trackColumns = {row["name"] for row in conn.execute("PRAGMA table_info(tracks)").fetchall()}
        albumColumns = {row["name"] for row in conn.execute("PRAGMA table_info(albums)").fetchall()}
        with conn:
            if "availability_reason" not in trackColumns:
                conn.execute("ALTER TABLE tracks ADD COLUMN availability_reason TEXT")
            if "backfill_attempted_at" not in albumColumns:
                conn.execute("ALTER TABLE albums ADD COLUMN backfill_attempted_at REAL")

    def addRequesterSeenAcceptedColumnIfMissing(self) -> None:
        """Add user_shares.requester_seen_accepted (the "your share request
        was accepted" topbar notification's dismissal flag) if missing.
        Guarded so re-running doesn't fail."""
        conn = self._conn()
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_shares)").fetchall()}
        if "requester_seen_accepted" not in columns:
            with conn:
                conn.execute("ALTER TABLE user_shares ADD COLUMN requester_seen_accepted INTEGER NOT NULL DEFAULT 0")

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

    def isFileImported(self, username: str, file_hash: str) -> bool:
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM imported_files WHERE username = ? AND file_hash = ?",
            (username, file_hash)
        ).fetchone()
        return row is not None

    def markFileImported(self, username: str, file_hash: str) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO imported_files (username, file_hash) VALUES (?, ?)",
            (username, file_hash)
        )

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

    def getAllUsersDetails(self, username: str | None = None) -> list[dict]:
        """The overview page's per-user rows. `username` narrows to a single
        user's own row - what a non-admin viewer is allowed to see (the full
        listing is admin-only, see app.py's overviewPage)."""
        conn = self._conn()
        query = "SELECT username, email, cookies_json, spotify_client_id, spotify_refresh_token, created_at FROM users"
        params: tuple = ()
        if username is not None:
            query += " WHERE username=?"
            params = (username,)
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def getAlbumsMissingMetadata(self, limit: int) -> list[str]:
        """Albums with incomplete metadata (missing release date or track count),
        excluding fabricated fallback albums (album_<id> never existed on Spotify).
        backfill_attempted_at rate-limits retries: each processed album waits
        ALBUM_BACKFILL_RETRY_SECONDS before being re-queued, so restricted/blanked
        albums get another chance weekly while permanently dateless albums don't
        hammer the API every cycle."""
        conn = self._conn()
        retryCutoff = time.time() - ALBUM_BACKFILL_RETRY_SECONDS
        rows = conn.execute(
            r"""
            SELECT id FROM albums
            WHERE (release_date = 0 OR release_date IS NULL OR total_tracks = 0)
              AND id NOT LIKE 'album\_%' ESCAPE '\'
              AND (backfill_attempted_at IS NULL OR backfill_attempted_at < ?)
            LIMIT ?
            """,
            (retryCutoff, limit)
        ).fetchall()
        return [row["id"] for row in rows]

    def markAlbumsBackfillAttempted(self, albumIds: list[str]) -> None:
        """Stamp albums as processed by the backfiller so they leave the queue for
        ALBUM_BACKFILL_RETRY_SECONDS - including albums Spotify returned no data
        for, which would otherwise be re-fetched every cycle forever."""
        if not albumIds:
            return
        conn = self._conn()
        placeholders = ",".join("?" for _ in albumIds)
        with conn:
            conn.execute(
                f"UPDATE albums SET backfill_attempted_at = ? WHERE id IN ({placeholders})",
                [time.time(), *albumIds],
            )

    def updateAlbumMetadata(self, album_id: str, release_date: float, total_tracks: int, name: str | None = None) -> None:
        """Blank fields aren't data: a zero release_date/total_tracks or an
        empty name never overwrites an existing value, so a partial backfill
        response (e.g. an album Spotify returns without a usable release date)
        can't regress metadata another source already filled."""
        conn = self._conn()
        with conn:
            conn.execute(
                """
                UPDATE albums SET
                    release_date = CASE WHEN :release_date > 0 THEN :release_date ELSE release_date END,
                    total_tracks = CASE WHEN :total_tracks > 0 THEN :total_tracks ELSE total_tracks END,
                    name = CASE WHEN :name IS NOT NULL AND :name != '' THEN :name ELSE name END
                WHERE id = :id
                """,
                {"id": album_id, "release_date": release_date, "total_tracks": total_tracks, "name": name},
            )

    def updateTrackName(self, track_id: str, name: str, duration_ms: int | None = None) -> None:
        """duration_ms also updates the stored duration when provided (>0) - the
        album backfill response is the only source of durations for tracks whose
        own lookup came back blanked (region-restricted)."""
        conn = self._conn()
        with conn:
            if duration_ms:
                conn.execute(
                    "UPDATE tracks SET name = ?, duration_ms = ? WHERE id = ?",
                    (name, duration_ms, track_id)
                )
            else:
                conn.execute(
                    "UPDATE tracks SET name = ? WHERE id = ?",
                    (name, track_id)
                )

    def getMaxPlayedAtInPeriod(self, username: str, startTs: float, endTs: float) -> float | None:
        row = self._conn().execute(
            "SELECT MAX(played_at) FROM plays WHERE username = ? AND played_at >= ? AND played_at < ?",
            (username, startTs, endTs)
        ).fetchone()
        return row[0] if row else None

    def getPlayTimeRange(self, username: str) -> tuple[float, float] | None:
        """(earliest, latest) played_at across the user's whole history, or
        None if they have no plays - lets a caller pin an "all time" query to
        an explicit range (e.g. the Compare page aligning two users' trend
        buckets over one shared axis)."""
        row = self._conn().execute(
            "SELECT MIN(played_at) AS minTs, MAX(played_at) AS maxTs FROM plays WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None or row["minTs"] is None:
            return None
        return row["minTs"], row["maxTs"]

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

