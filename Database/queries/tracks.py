from __future__ import annotations

from Database.queries._base import *  # noqa: F401,F403 - shared constants/db helpers


class TrackQueries:
    """TrackQueries: tracks data-access methods, mixed into Repository."""

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

    def deleteFailedArtistImages(self) -> int:
        """One-time remediation for migrate1_20_0: every artist image previously
        marked 'failed' was marked that way by scraping open.spotify.com's public
        artist page for an og:image meta tag, a method that stopped working for
        every artist (not just ones that genuinely lack a picture) once Spotify
        moved artist pages to a client-rendered SPA shell. None of those rows are
        trustworthy "no image" signals, so they're deleted rather than left
        'failed' - lazyFetchArtistImage treats 'failed' as permanent, and a missing
        row means never-attempted, letting the fixed Web-API/SpotipyFree fetch
        retry them. Returns the number of rows cleared."""
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "DELETE FROM images WHERE kind=? AND status=?", (IMAGE_KIND_ARTIST, IMAGE_STATUS_FAILED))
            return cur.rowcount

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

    def getAlbumsWithArtistlessTracks(self, limit: int) -> list[str]:
        """Albums holding at least one track with NO track_artists rows -
        tracks saved from degraded payloads (or legacy imports) that carried
        no artist data. The metadata backfiller piggybacks these onto its
        album fetches: the album payload's per-track artists repair the
        links. Shares the backfill_attempted_at rate limit (and the
        fabricated-id exclusion) with getAlbumsMissingMetadata."""
        conn = self._conn()
        retryCutoff = time.time() - ALBUM_BACKFILL_RETRY_SECONDS
        rows = conn.execute(
            r"""
            SELECT DISTINCT al.id FROM albums al
            JOIN tracks t ON t.album_id = al.id
            WHERE NOT EXISTS (SELECT 1 FROM track_artists ta WHERE ta.track_id = t.id)
              AND al.id NOT LIKE 'album\_%' ESCAPE '\'
              AND (al.backfill_attempted_at IS NULL OR al.backfill_attempted_at < ?)
            LIMIT ?
            """,
            (retryCutoff, limit)
        ).fetchall()
        return [row["id"] for row in rows]

    def addMissingTrackArtists(self, trackId: str, artists: list[dict]) -> bool:
        """Write artist links for a known track that has NONE - the metadata
        backfiller's repair path. Existing links are never touched (the album
        payload is a repair source, not an authority over what richer
        play-time payloads recorded), and existing artist rows keep their
        data. Returns whether links were written."""
        if not artists:
            return False
        conn = self._conn()
        with conn:
            if conn.execute("SELECT 1 FROM track_artists WHERE track_id=? LIMIT 1",
                            (trackId,)).fetchone() is not None:
                return False
            if conn.execute("SELECT 1 FROM tracks WHERE id=?", (trackId,)).fetchone() is None:
                return False
            for position, artist in enumerate(artists):
                conn.execute(
                    """
                    INSERT INTO artists (id, name, url, image_id)
                    VALUES (:id, :name, :url, :imageId)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    artist,
                )
                conn.execute(
                    "INSERT INTO track_artists (track_id, artist_id, position) VALUES (?, ?, ?)",
                    (trackId, artist["id"], position),
                )
        return True

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

    def getAlbumPrimaryArtists(self, albumIds: list[str]) -> dict[str, dict]:
        """albumId -> {artist_id, artist_name} via each album's tracks'
        position-0 artists (albums carry no artist column of their own). The
        most frequent primary artist wins; ties break by artist id so repeated
        runs derive the same lookup name. Albums with no resolvable artist are
        simply absent - the worker marks those attempted without a lookup."""
        if not albumIds:
            return {}
        conn = self._conn()
        placeholders = ",".join("?" for _ in albumIds)
        rows = conn.execute(
            f"""
            SELECT t.album_id AS album_id, ar.id AS artist_id, ar.name AS artist_name,
                   COUNT(*) AS cnt
            FROM tracks t
            JOIN track_artists ta ON ta.track_id = t.id AND ta.position = 0
            JOIN artists ar ON ar.id = ta.artist_id
            WHERE t.album_id IN ({placeholders})
            GROUP BY t.album_id, ar.id
            ORDER BY t.album_id, cnt DESC, ar.id ASC
            """,
            albumIds,
        ).fetchall()
        primaries: dict[str, dict] = {}
        for row in rows:
            if row["album_id"] not in primaries:   #< rows are sorted best-first per album
                primaries[row["album_id"]] = {"artist_id": row["artist_id"],
                                              "artist_name": row["artist_name"]}
        return primaries
