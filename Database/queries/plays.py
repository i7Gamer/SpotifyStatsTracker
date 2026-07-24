from __future__ import annotations

from Database.queries._base import *  # noqa: F401,F403 - shared constants/db helpers


class PlayQueries:
    """PlayQueries: plays data-access methods, mixed into Repository."""

    # ---- Per-user: plays (play history) -----------------------------------------

    def insertPlay(self, username: str, trackId: str, playedAt: float, timePlayed: int,
                   playedFrom: str | None = None, created_reason: str | None = None,
                   extras: dict | None = None, is_skip: int = 0) -> bool:
        """Returns True if a new row was inserted, False if this exact
        (username, trackId, playedAt) play was already recorded (updates
        time_played if different, and enriches behavioral columns from
        `extras` - a non-None extras value wins over the stored one, a None
        value never clobbers).
        If created_reason is provided, it's only set on INSERT (never updated
        on an existing play, matching upsertTrack()'s semantics).

        is_skip: 0 for a real play (the default), 1 for a skip. The write paths
        that classify (the importer, and the listener/backfill via
        appendMetadata) compute it from the current threshold and pass it here;
        direct callers default to a real play, and recomputeSkipFlags()
        reclassifies every row when the admin changes the threshold. It's also
        written on the update path so a re-recorded play tracks the supplied
        value.

        Does NOT commit - see upsertTrack()'s docstring."""
        conn = self._conn()
        behavioralSelect = ", ".join(BEHAVIORAL_COLUMNS)
        existing = conn.execute(
            f"SELECT id, time_played, {behavioralSelect} FROM plays WHERE username=? AND track_id=? AND played_at=?",
            (username, trackId, playedAt)
        ).fetchone()

        if existing:
            extras = extras or {}
            behavioralChanged = any(
                extras.get(column) is not None and extras.get(column) != existing[column]
                for column in BEHAVIORAL_COLUMNS
            )
            if existing["time_played"] != timePlayed or behavioralChanged:
                behavioralSet = ", ".join(f"{column} = COALESCE(?, {column})" for column in BEHAVIORAL_COLUMNS)
                conn.execute(
                    f"UPDATE plays SET time_played = ?, is_skip = ?, played_from = COALESCE(?, played_from), {behavioralSet} WHERE id = ?",
                    (timePlayed, is_skip, playedFrom, *[extras.get(column) for column in BEHAVIORAL_COLUMNS], existing["id"])
                )
            return False

        createdAt = time.time() if created_reason else None
        extras = extras or {}
        behavioralInsert = ", ".join(BEHAVIORAL_COLUMNS)
        behavioralPlaceholders = ", ".join("?" for _ in BEHAVIORAL_COLUMNS)
        cur = conn.execute(
            f"INSERT OR IGNORE INTO plays (username, track_id, played_at, time_played, played_from, created_at, created_reason, is_skip, {behavioralInsert}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, {behavioralPlaceholders})",
            (username, trackId, playedAt, timePlayed, playedFrom, createdAt, created_reason, is_skip,
             *[extras.get(column) for column in BEHAVIORAL_COLUMNS]),
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
        # is_skip=0: near-time matching only ever considered real plays (skips
        # used to live in a separate table); a backfill row must never dedup
        # against, or claim/correct, a merged skip row.
        row = conn.execute(
            "SELECT 1 FROM plays WHERE username=? AND track_id=? AND played_at BETWEEN ? AND ? AND is_skip=0 LIMIT 1",
            (username, trackId, playedAt - toleranceSeconds, playedAt + toleranceSeconds),
        ).fetchone()
        return row is not None

    def getPlaysNearTime(self, username: str, trackId: str, playedAt: float, toleranceSeconds: float) -> list[dict]:
        """Return all plays for this exact track already existing for this user
        within toleranceSeconds of playedAt (inclusive both directions).
        Used during imports to detect duplicates and decide whether to update;
        carries the behavioral columns so the import can enrich NULLs in place."""
        conn = self._conn()
        behavioralSelect = ", ".join(BEHAVIORAL_COLUMNS)
        # is_skip=0: only real plays are correction/dedup candidates (see hasPlayNearTime).
        rows = conn.execute(
            f"SELECT id, played_at, time_played, {behavioralSelect} FROM plays "
            f"WHERE username=? AND track_id=? AND played_at BETWEEN ? AND ? AND is_skip=0",
            (username, trackId, playedAt - toleranceSeconds, playedAt + toleranceSeconds),
        ).fetchall()
        return [dict(row) for row in rows]

    def deletePlaysInRange(self, username: str, startTs: float, endTs: float) -> int:
        """Delete every real play (is_skip=0) of this user whose played_at falls
        inside the closed [startTs, endTs] window - the overwrite-import wipe.
        Skips in the same range are removed by deleteSkipsInRange, so the two
        counts stay separately reportable. Returns the number of rows removed.

        Does NOT commit - see upsertTrack()'s docstring."""
        conn = self._conn()
        cur = conn.execute(
            "DELETE FROM plays WHERE username=? AND played_at BETWEEN ? AND ? AND is_skip=0",
            (username, startTs, endTs),
        )
        return cur.rowcount

    def deleteSkipsInRange(self, username: str, startTs: float, endTs: float) -> int:
        """Skip counterpart of deletePlaysInRange(): removes the is_skip=1 rows
        in range (skips now live in plays, not a separate play_skips table).

        Does NOT commit - see upsertTrack()'s docstring."""
        conn = self._conn()
        cur = conn.execute(
            "DELETE FROM plays WHERE username=? AND played_at BETWEEN ? AND ? AND is_skip=1",
            (username, startTs, endTs),
        )
        return cur.rowcount

    def deleteZeroDurationPlays(self) -> int:
        """Remove plays with zero (or negative) recorded listening time, across
        every user - leftover skip/error events that older importer versions
        recorded as real plays before the importer started filtering them out
        at import time. Returns the number of rows removed.

        Does NOT commit - see upsertTrack()'s docstring."""
        conn = self._conn()
        cur = conn.execute("DELETE FROM plays WHERE time_played <= 0")
        return cur.rowcount

    def getPlaysCount(self, username: str, startTs: float | None = None, endTs: float | None = None,
                       trackId: str | None = None, artistId: str | None = None,
                       albumId: str | None = None) -> int:
        conn = self._conn()
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs)
        extraClauses = self._itemFilterClauses(params, trackId, artistId, albumId)
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM plays WHERE username=? AND is_skip=0{rangeClause}{extraClauses}",
            params,
        ).fetchone()
        return row["c"]

    def getPlaysNewestFirst(self, username: str, count: int | None = None, startIndex: int = 0,
                             startTs: float | None = None, endTs: float | None = None,
                             trackId: str | None = None, artistId: str | None = None,
                             albumId: str | None = None) -> list[dict]:
        conn = self._conn()
        limit = -1 if count is None else count
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs)
        extraClauses = self._itemFilterClauses(params, trackId, artistId, albumId)
        params += [limit, startIndex]
        rows = conn.execute(
            f"SELECT track_id, played_at, time_played, played_from FROM plays "
            f"WHERE username=? AND is_skip=0{rangeClause}{extraClauses} ORDER BY played_at DESC, id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [self._playRowToEntry(r) for r in rows]

    def getPlaysOldestFirst(self, username: str, count: int | None = None, startIndex: int = 0,
                             startTs: float | None = None, endTs: float | None = None,
                             trackId: str | None = None, artistId: str | None = None,
                             albumId: str | None = None) -> list[dict]:
        conn = self._conn()
        limit = -1 if count is None else count
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs)
        extraClauses = self._itemFilterClauses(params, trackId, artistId, albumId)
        params += [limit, startIndex]
        behavioralSelect = ", ".join(BEHAVIORAL_COLUMNS)
        rows = conn.execute(
            f"SELECT track_id, played_at, time_played, played_from, {behavioralSelect} FROM plays "
            f"WHERE username=? AND is_skip=0{rangeClause}{extraClauses} ORDER BY played_at ASC, id ASC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [self._playRowToEntry(r) for r in rows]

    def getSkipsOldestFirst(self, username: str, count: int | None = None, startIndex: int = 0) -> list[dict]:
        """Skip events (is_skip=1) oldest-first, shaped like getPlaysOldestFirst
        entries (skips carry no played_from - it comes back None). Feeds the JSON
        export so skips round-trip between instances."""
        conn = self._conn()
        limit = -1 if count is None else count
        behavioralSelect = ", ".join(BEHAVIORAL_COLUMNS)
        rows = conn.execute(
            f"SELECT track_id, played_at, time_played, {behavioralSelect} FROM plays "
            f"WHERE username=? AND is_skip=1 ORDER BY played_at ASC, id ASC LIMIT ? OFFSET ?",
            (username, limit, startIndex),
        ).fetchall()
        return [self._playRowToEntry(r) for r in rows]

    def getSkipCount(self, username: str, startTs: float | None = None, endTs: float | None = None) -> int:
        """Number of skip events (plays.is_skip=1) in range - the boundary is
        the admin-tunable skip threshold, materialized per row, so this is a
        plain count with no per-row duration check."""
        conn = self._conn()
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs)
        row = conn.execute(f"SELECT COUNT(*) AS c FROM plays WHERE username=? AND is_skip=1{rangeClause}", params).fetchone()
        return row["c"]

    def getPlayAndSkipCountsByUser(self) -> dict[str, dict]:
        """All-time play (is_skip=0) and skip (is_skip=1) counts for every user,
        as {username: {"plays": int, "skips": int}}, in ONE grouped scan - the
        admin user table's old getPlaysCount()+getSkipCount() pair per user was
        2*N queries. Users with no plays are simply absent; the caller defaults
        them to 0."""
        conn = self._conn()
        rows = conn.execute(
            """
            SELECT username,
                   SUM(CASE WHEN is_skip = 0 THEN 1 ELSE 0 END) AS plays,
                   SUM(CASE WHEN is_skip = 1 THEN 1 ELSE 0 END) AS skips
            FROM plays
            GROUP BY username
            """
        ).fetchall()
        return {r["username"]: {"plays": r["plays"], "skips": r["skips"]} for r in rows}

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
            "WHERE username=? AND played_at BETWEEN ? AND ? AND is_skip=0",
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

    def getPlayedTrackIds(self, username: str, trackIds: list[str]) -> set[str]:
        """The subset of `trackIds` this user has at least one play of - the
        Compare page's "does the viewer have their own data for this
        counterpart item" check (see app.py's comparePage), so a counterpart
        song only links out to Spotify when the viewer's own detail page
        would have nothing to show. Deliberately a real play-history lookup,
        not membership in the viewer's own top-N pool: a track can rank
        outside someone's top 100 while they've still genuinely played it,
        and getSongsPage's own trackId lookup (what the detail page actually
        renders from) has no pool-depth limit either - this matches that
        exactly."""
        if not trackIds:
            return set()
        conn = self._conn()
        placeholders = ",".join("?" for _ in trackIds)
        rows = conn.execute(
            f"SELECT DISTINCT track_id FROM plays WHERE username=? AND is_skip=0 AND track_id IN ({placeholders})",
            [username, *trackIds],
        ).fetchall()
        return {r["track_id"] for r in rows}

    def getPlayedArtistIds(self, username: str, artistIds: list[str]) -> set[str]:
        """The subset of `artistIds` this user has at least one play of a
        track crediting (any billing position) - the artist counterpart to
        getPlayedTrackIds(), matching getArtistAggregates' own artistId
        lookup exactly."""
        if not artistIds:
            return set()
        conn = self._conn()
        placeholders = ",".join("?" for _ in artistIds)
        rows = conn.execute(
            f"""
            SELECT DISTINCT ta.artist_id AS artist_id
            FROM plays p
            JOIN track_artists ta ON ta.track_id = p.track_id
            WHERE p.username=? AND p.is_skip=0 AND ta.artist_id IN ({placeholders})
            """,
            [username, *artistIds],
        ).fetchall()
        return {r["artist_id"] for r in rows}

    def getPlayedAlbumIds(self, username: str, albumIds: list[str]) -> set[str]:
        """The subset of `albumIds` this user has at least one play of a
        track from - the album counterpart to getPlayedTrackIds(), matching
        getAlbumsPage's own albumId lookup exactly."""
        if not albumIds:
            return set()
        conn = self._conn()
        placeholders = ",".join("?" for _ in albumIds)
        rows = conn.execute(
            f"""
            SELECT DISTINCT t.album_id AS album_id
            FROM plays p
            JOIN tracks t ON t.id = p.track_id
            WHERE p.username=? AND p.is_skip=0 AND t.album_id IN ({placeholders})
            """,
            [username, *albumIds],
        ).fetchall()
        return {r["album_id"] for r in rows}

    @staticmethod
    def _playRowToEntry(row) -> dict:
        columns = row.keys()
        entry = {
            "id": row["track_id"],
            "playedAt": row["played_at"],
            "timePlayed": row["time_played"],
            "playedFrom": row["played_from"] if "played_from" in columns else None,
        }
        # Behavioral columns are only attached when the SELECT carried them
        # (wider play/skip reads) - narrower SELECT sites keep their shape.
        if "platform" in columns:
            extras = {column: row[column] for column in BEHAVIORAL_COLUMNS}
            entry["extras"] = extras if any(v is not None for v in extras.values()) else None
        return entry

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

    def searchPlays(self, username: str, query: str, limit: int | None = None, offset: int = 0,
                     startTs: float | None = None, endTs: float | None = None,
                     oldestFirst: bool = False) -> list[dict]:
        """Plays (newest first, or oldest first with `oldestFirst`) whose track
        name, artist(s), album, or source playlist/album match `query` - the
        SQL-pushed-down, paginated replacement for fetching every play and
        filtering in Python."""
        conn = self._conn()
        limitValue = -1 if limit is None else limit
        pattern = self._likePattern(query)
        params = [username, pattern, pattern, pattern, pattern]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        params += [limitValue, offset]
        direction = "ASC" if oldestFirst else "DESC"
        rows = conn.execute(
            f"""
            SELECT p.track_id AS track_id, p.played_at AS played_at,
                   p.time_played AS time_played, p.played_from AS played_from
            FROM plays p
            {self._SEARCH_JOIN_CLAUSE}
            WHERE p.username = ? AND p.is_skip=0 {self._SEARCH_MATCH_CLAUSE}{rangeClause}
            ORDER BY p.played_at {direction}, p.id {direction}
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [self._playRowToEntry(r) for r in rows]

    def searchPlaysCount(self, username: str, query: str,
                          startTs: float | None = None, endTs: float | None = None) -> int:
        """The paging counterpart to searchPlays() - total matching plays,
        for computing total page count without fetching every match."""
        conn = self._conn()
        pattern = self._likePattern(query)
        params = [username, pattern, pattern, pattern, pattern]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM plays p
            {self._SEARCH_JOIN_CLAUSE}
            WHERE p.username = ? AND p.is_skip=0 {self._SEARCH_MATCH_CLAUSE}{rangeClause}
            """,
            params,
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
            WHERE username = ? AND is_skip=0{rangeClause}
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
        # Aggregate play-side first, then join artists for only the surviving
        # ids. Joining artists up front made SQLite scan the entire global
        # artists catalog (tens of thousands of rows, most never played by this
        # user) and probe plays per artist; aggregating from this user's plays
        # and looking up only the artists that appear is ~70% faster for
        # byte-identical output on a large library.
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        aggFilter = ""
        if artistId is not None:
            # ta.artist_id == ar.id, so this is equivalent to the old outer
            # ar.id filter but prunes the aggregation to the one artist.
            aggFilter += " AND ta.artist_id = ?"
            params.append(artistId)
        outerFilter = ""
        if searchQuery:
            # The name filter only selects WHICH artists to return; it never
            # changes an artist's own play totals, so applying it after
            # aggregation is equivalent to the old pre-group filter.
            outerFilter += " WHERE ar.name LIKE ? ESCAPE '\\'"
            params.append(self._likePattern(searchQuery))
        params += [limitValue, offset]
        rows = conn.execute(
            f"""
            WITH agg AS (
                SELECT ta.artist_id AS artist_id,
                       COUNT(*) AS plays, SUM(p.time_played) AS total_time_listened,
                       MIN(p.played_at) AS first_listened_at, COUNT(DISTINCT p.track_id) AS unique_song_count
                FROM plays p
                JOIN track_artists ta ON ta.track_id = p.track_id
                WHERE p.username = ? AND p.is_skip=0{rangeClause}{aggFilter}
                GROUP BY ta.artist_id
            )
            SELECT ar.id AS id, ar.name AS name, ar.url AS url, ar.image_id AS image_id,
                   agg.plays AS plays, agg.total_time_listened AS total_time_listened,
                   agg.first_listened_at AS first_listened_at, agg.unique_song_count AS unique_song_count
            FROM agg
            JOIN artists ar ON ar.id = agg.artist_id{outerFilter}
            ORDER BY {sortColumn} {direction}, total_time_listened DESC, name COLLATE NOCASE ASC, id ASC
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
                WHERE p.username = ? AND p.is_skip=0{rangeClause}{searchClause}
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
                WHERE p.username = ? AND p.is_skip=0{rangeClause}
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
            WHERE p.username = ? AND p.is_skip=0{rangeClause}{extraClauses}
            GROUP BY t.id
            ORDER BY {sortColumn} {direction}, total_time_listened DESC, name COLLATE NOCASE ASC, track_id ASC
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
                    SELECT track_id FROM plays WHERE username = ? AND is_skip=0{rangeClause}
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
                WHERE p.username = ? AND p.is_skip=0{rangeClause}
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
            WHERE p.username = ? AND p.is_skip=0{rangeClause}{extraClauses}
            GROUP BY al.id
            ORDER BY {sortColumn} {direction}, total_time_listened DESC, name COLLATE NOCASE ASC, album_id ASC
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
                    WHERE p.username = ? AND p.is_skip=0{rangeClause}
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
                WHERE p.username = ? AND p.is_skip=0{rangeClause}
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
            FROM plays WHERE username = ? AND is_skip=0{rangeClause}{extraClauses}
            GROUP BY bucket
            ORDER BY bucket
            """,
            params,
        ).fetchall()
        return [{"bucketStartTs": r["bucket"] * PLAY_BUCKET_SECONDS,
                 "plays": r["plays"],
                 "totalTimeListened": r["total_time"]} for r in rows]

    def getPlaysForMonthDays(self, username: str, monthDays: list[str]) -> list[dict]:
        """Raw plays whose UTC calendar month-day is in `monthDays` (each a
        "%m-%d" string), each with its track name and primary (position-0)
        artist name. Deliberately over-selects a ±1-day UTC window: the caller
        (Database.getOnThisDay) converts played_at to the user's local
        timezone and does the exact local month/day + year grouping, and a
        play's local date can differ from its UTC date by up to a day."""
        if not monthDays:
            return []
        conn = self._conn()
        placeholders = ",".join("?" for _ in monthDays)
        rows = conn.execute(
            f"""
            SELECT p.played_at AS played_at, p.track_id AS track_id,
                   t.name AS track_name, ar.name AS artist_name
            FROM plays p
            JOIN tracks t ON t.id = p.track_id
            LEFT JOIN track_artists ta ON ta.track_id = p.track_id AND ta.position = 0
            LEFT JOIN artists ar ON ar.id = ta.artist_id
            WHERE p.username = ? AND p.is_skip=0
              AND strftime('%m-%d', p.played_at, 'unixepoch') IN ({placeholders})
            """,
            [username, *monthDays],
        ).fetchall()
        return [dict(r) for r in rows]

    def getBucketedArtistPlayCounts(self, username: str, startTs: float | None = None,
                                     endTs: float | None = None) -> list[dict]:
        """Play counts per (fixed PLAY_BUCKET_SECONDS UTC bucket, artist id) -
        the SQL half of the artist-trend chart, replacing a row-per-
        (play, artist) transfer. A play whose track has N artists still
        counts once per artist. artist_id rides along per row so the caller
        (Database.getArtistTrend) can pick a representative id for same-
        named artists, which still merge into one series/line there exactly
        as before - this only adds data, it doesn't change that merge.
        Ordered by bucket so callers iterate in play-time order."""
        conn = self._conn()
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs, column="p.played_at")
        rows = conn.execute(
            f"""
            SELECT CAST(p.played_at / {PLAY_BUCKET_SECONDS} AS INTEGER) AS bucket,
                   ar.id AS artist_id,
                   ar.name AS artist_name,
                   COUNT(*) AS plays
            FROM plays p
            JOIN track_artists ta ON ta.track_id = p.track_id
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE p.username = ? AND p.is_skip=0{rangeClause}
            GROUP BY bucket, ar.id, ar.name
            ORDER BY bucket, ar.name
            """,
            params,
        ).fetchall()
        return [{"bucketStartTs": r["bucket"] * PLAY_BUCKET_SECONDS,
                 "artistId": r["artist_id"],
                 "artistName": r["artist_name"],
                 "plays": r["plays"]} for r in rows]

    def getPlayTotals(self, username: str, startTs: float | None = None,
                       endTs: float | None = None) -> tuple[int, int]:
        conn = self._conn()
        params = [username]
        rangeClause = self._dateRangeClause(params, startTs, endTs)
        row = conn.execute(
            f"SELECT COUNT(*) AS c, COALESCE(SUM(time_played), 0) AS total FROM plays "
            f"WHERE username = ? AND is_skip=0{rangeClause}",
            params,
        ).fetchone()
        return row["c"], row["total"]

    def getDiscoveredSongsCount(self, username: str, startTs: float | None = None,
                                 endTs: float | None = None) -> int:
        """Count of distinct songs first played (across all time) within the year range."""
        conn = self._conn()
        # A song is "discovered in range" iff its all-time first (non-skip) play
        # falls in range - which already implies it was played in range. So group
        # once per track and keep those whose MIN(played_at) is in range, instead
        # of the old per-row correlated MIN() subquery (O(plays) vs O(plays^2)).
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM (
                SELECT track_id
                FROM plays
                WHERE username = ? AND is_skip=0
                GROUP BY track_id
                HAVING MIN(played_at) >= ? AND MIN(played_at) < ?
            )
            """,
            (username, startTs, endTs),
        ).fetchone()
        return row["c"]

    def getDiscoveredArtistsCount(self, username: str, startTs: float | None = None,
                                   endTs: float | None = None) -> int:
        """Count of distinct artists first played (across all time) within the year range."""
        conn = self._conn()
        # Same shape as getDiscoveredSongsCount: an artist is "discovered in
        # range" iff their all-time first (non-skip) play - across any of their
        # tracks - is in range. Group once per artist over the plays<->artist
        # join and keep those whose MIN(played_at) is in range, instead of the
        # old correlated per-row subquery (which re-scanned every track of the
        # artist for each candidate row).
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM (
                SELECT ta.artist_id
                FROM plays p
                JOIN track_artists ta ON ta.track_id = p.track_id
                WHERE p.username = ? AND p.is_skip=0
                GROUP BY ta.artist_id
                HAVING MIN(p.played_at) >= ? AND MIN(p.played_at) < ?
            )
            """,
            (username, startTs, endTs),
        ).fetchone()
        return row["c"]

    def getMaxPlayedAtInPeriod(self, username: str, startTs: float, endTs: float) -> float | None:
        row = self._conn().execute(
            "SELECT MAX(played_at) FROM plays WHERE username = ? AND is_skip=0 AND played_at >= ? AND played_at < ?",
            (username, startTs, endTs)
        ).fetchone()
        return row[0] if row else None

    def getPlayTimeRange(self, username: str, trackId: str | None = None,
                          artistId: str | None = None, albumId: str | None = None) -> tuple[float, float] | None:
        """(earliest, latest) played_at across the user's whole history, or
        None if they have no plays - lets a caller pin an "all time" query to
        an explicit range (e.g. the Compare page aligning two users' trend
        buckets over one shared axis). `trackId`/`artistId`/`albumId` narrow
        it to one item's plays - the span the detail pages' auto trend-bucket
        resolution derives from."""
        params: list = [username]
        extraClauses = self._itemFilterClauses(params, trackId, artistId, albumId)
        row = self._conn().execute(
            f"SELECT MIN(played_at) AS minTs, MAX(played_at) AS maxTs FROM plays "
            f"WHERE username = ? AND is_skip=0{extraClauses}",
            params,
        ).fetchone()
        if row is None or row["minTs"] is None:
            return None
        return row["minTs"], row["maxTs"]

    def getPlayCountInPeriod(self, username: str, startTs: float, endTs: float) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) FROM plays WHERE username = ? AND is_skip=0 AND played_at >= ? AND played_at < ?",
            (username, startTs, endTs)
        ).fetchone()
        return row[0] if row else 0
