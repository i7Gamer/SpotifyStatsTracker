try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
    from Database.db import RESTRICTED_FALLBACK_REASON, UNKNOWN_TRACK_NAME, UNKNOWN_ALBUM_NAME
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository
    from db import RESTRICTED_FALLBACK_REASON, UNKNOWN_TRACK_NAME, UNKNOWN_ALBUM_NAME

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
    instead: the id IS real, so the Spotify link works, and the row is marked
    RESTRICTED_FALLBACK_REASON, which upsertTrack is explicitly built to let
    real metadata replace later (see its guard, and _fallbackTrackRecord in
    Database/patches.py, whose shape this mirrors).

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
                repo.upsertTrack({
                    "id": trackId,
                    # Invents no facts: no duration, no artists, no album name.
                    # The placeholder name is the shared one rather than "" so
                    # the row doesn't render as a blank line in every list it
                    # appears in.
                    "name": UNKNOWN_TRACK_NAME,
                    "url": f"{SPOTIFY_TRACK_URL_PREFIX}{trackId}",
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
                    "created_reason": RESTRICTED_FALLBACK_REASON,
                })
            # Whatever is still orphaned now belongs to a track with no plays.
            sweptCredits = conn.execute(
                """DELETE FROM track_artists
                   WHERE track_id NOT IN (SELECT id FROM tracks)"""
            ).rowcount
            repo.commit()
        finally:
            repo.connectionManager.close()

        print(f"Repaired {len(orphanedPlayTrackIds)} track(s) referenced by plays but missing from "
              f"the catalog, and removed {sweptCredits} artist credit(s) for tracks that no longer exist.")
        self.updateAppVersion("1.44.0")


if __name__ == "__main__":
    Migrator("1.43.0", "1.44.0").migrate()
