# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """1.51.0 -> 1.52.0: adds albums.artist_repair_done_at, the stamp that
    lets the artistless-track repair queue terminate.

    That queue selects albums holding a track with no track_artists rows, and
    the metadata backfiller repairs them from the album payload's per-track
    artists. Its exit condition was the absence of such tracks - which an
    album can never reach when Spotify credits nobody on one of them, or when
    the track sits past the album endpoint's first embedded page. Each pass
    repaired nothing, stamped backfill_attempted_at, and the album came back
    ALBUM_BACKFILL_RETRY_SECONDS later, forever. Once the album-metadata queue
    drains - the loop's documented steady state - this is the only album
    source, so those albums occupied slots in every batch indefinitely.

    The column records that an album's COMPLETE track list was walked and
    every credit it carried applied, and the queue skips it for
    ALBUM_ARTIST_REPAIR_RETRY_SECONDS. A window rather than a permanent flag
    on purpose: an album CAN gain a new artistless track later (an import
    writes one), and permanent "give up" markers are the shape this project
    has twice had to ship a migrator to undo.

    Arrives NULL for every existing album, which reads as "never walked" - so
    the upgrade queues exactly what it queued before, and nothing is skipped
    until a cycle has genuinely finished walking one. Nothing to backfill: no
    existing row can honestly claim a complete walk, because before this
    release nothing performed one."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addAlbumArtistRepairColumnIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added albums.artist_repair_done_at column.")
        self.updateAppVersion("1.52.0")


if __name__ == "__main__":
    Migrator("1.51.0", "1.52.0").migrate()
