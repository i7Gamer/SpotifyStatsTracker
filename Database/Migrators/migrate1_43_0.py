# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
    from Database.db import (RESTRICTED_FALLBACK_REASON, SYNTHETIC_FALLBACK_REASON,
                             UNKNOWN_TRACK_NAME, UNKNOWN_ALBUM_NAME, looksLikeSpotifyTrackId)
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository
    from db import (RESTRICTED_FALLBACK_REASON, SYNTHETIC_FALLBACK_REASON,
                    UNKNOWN_TRACK_NAME, UNKNOWN_ALBUM_NAME, looksLikeSpotifyTrackId)

SPOTIFY_TRACK_URL_PREFIX = "https://open.spotify.com/track/"


class Migrator(BaseMigrator):
    """1.43.0 -> 1.44.0: repairs the dangling foreign-key rows the startup probe
    reports.

    Repository.checkIntegrity has counted them at every boot since the probe
    landed, and nothing has ever acted on them: the live database carried the
    same 201 for over a week. They are two different problems wearing one name.

    plays -> tracks (6 rows live, four of them carrying three to four minutes of
    time_played) is real listening history. Raw COUNT(*) totals include those
    rows and every track-joined query drops them, so per-page totals disagree
    with each other for no visible reason. Deleting them would destroy the only
    record that the listening happened, so each gets a placeholder track
    instead, marked so upsertTrack will let real metadata replace it later (see
    its guard, and fallbackTrackRecord in Database/Spotify/client.py, whose shape this
    mirrors). A real 22-character Spotify id keeps a working link and the
    RESTRICTED marker; the importer's 32-character md5 surrogate for a track it
    could never resolve gets an empty url and SYNTHETIC, since no later lookup
    can repair it and a link would 404.

    track_artists -> tracks (195 rows live, across 133 track ids) is an artist
    credit for a track that no longer exists. Nothing can render it and nothing
    can repair it - there is no track to attach it to.

    The order is what makes this safe: placeholders are created FIRST, so every
    credit belonging to a track that still has plays becomes valid again (all
    195 live rows point at artists that DO exist, so the revived plays come back
    with their real artist names). Only what is still orphaned afterwards - a
    credit for a track nobody ever played - is swept.

    Deliberately not resurrecting a track just because a credit mentions it:
    that would grow the catalog with rows no page will ever show. Plays are the
    justification.

    Both steps are re-runnable: the second pass finds nothing to create and
    nothing to delete."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            conn = repo._conn()
            orphanedPlayTrackIds = [
                row["track_id"] for row in conn.execute(
                    """SELECT DISTINCT p.track_id FROM plays p
                       LEFT JOIN tracks t ON t.id = p.track_id
                       WHERE t.id IS NULL"""
                ).fetchall()
            ]
            for trackId in orphanedPlayTrackIds:
                # Only a real id gets a link. The importer's surrogate for an
                # unresolvable track is a bare md5 digest, and pointing
                # open.spotify.com at one produces a 404 - "only fabricated ids
                # carry an empty url" is the rule everywhere else in this
                # codebase, and it decides the marker too: a fabricated id can
                # never be repaired by a later lookup, which is what
                # SYNTHETIC_FALLBACK_REASON means as against RESTRICTED.
                isRealId = looksLikeSpotifyTrackId(trackId)
                repo.upsertTrack({
                    "id": trackId,
                    # Invents no facts: no duration, no artists, no album name.
                    # The placeholder name is the shared one rather than "" so
                    # the row doesn't render as a blank line in every list it
                    # appears in.
                    "name": UNKNOWN_TRACK_NAME,
                    "url": f"{SPOTIFY_TRACK_URL_PREFIX}{trackId}" if isRealId else "",
                    "duration": 0,
                    "explicit": False,
                    "isrc": "",
                    "discNumber": 0,
                    "trackNumber": 0,
                    "releaseDate": 0.0,
                    "imageUrl": "",
                    #< no cover art is known; the per-track fallback album
                    #  carries the same empty image
                    "imageId": "",
                    "artists": [],
                    # Supplied rather than left to upsertTrack, which would
                    # synthesize this album and name it after the TRACK - so
                    # every one of these placeholders had an album called
                    # "Unknown Track". The album_ prefix is the convention for a
                    # fabricated album and is what keeps it out of the album
                    # backfill queue, since it never existed on Spotify.
                    "album": {
                        "id": f"album_{trackId}",
                        "name": UNKNOWN_ALBUM_NAME,
                        "url": "",
                        "totalTracks": 1,
                        "releaseDate": 0.0,
                        "imageUrl": "",
                    },
                    "created_reason": RESTRICTED_FALLBACK_REASON if isRealId else SYNTHETIC_FALLBACK_REASON,
                })
            # Whatever is still orphaned now belongs to a track with no plays.
            sweptCredits = conn.execute(
                # NOT EXISTS, not NOT IN: tracks.id is a TEXT PRIMARY KEY,
                # which SQLite permits to be NULL, and a single NULL would make
                # NOT IN evaluate to NULL for every row - deleting nothing while
                # still reporting success.
                """DELETE FROM track_artists
                   WHERE NOT EXISTS (
                       SELECT 1 FROM tracks t WHERE t.id = track_artists.track_id
                   )"""
            ).rowcount
            repo.commit()
        finally:
            repo.connectionManager.close()

        print(f"Repaired {len(orphanedPlayTrackIds)} track(s) referenced by plays but missing from "
              f"the catalog, and removed {sweptCredits} artist credit(s) for tracks that no longer exist.")
        self.updateAppVersion("1.44.0")


if __name__ == "__main__":
    Migrator("1.43.0", "1.44.0").migrate()
