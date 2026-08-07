# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """1.47.0 -> 1.48.0: adds tracks.isrc_attempted_at, the retry stamp for the
    Spotify Web-API ISRC backfiller.

    tracks.isrc has existed since the catalog tables were introduced and is
    empty for every row on every instance - measured at 0/24,850 on a real
    library. Nothing that writes the catalog can fill it: the pathfinder client
    does not expose ISRCs (Database/Spotify/formatting.py) and Spotify's
    extended history export has no such field, so both paths supply "". Only
    GET /v1/tracks carries external_ids.isrc, which is why the backfiller is
    Web-API-only and why instances without credentials simply stay empty.

    Nothing to backfill here: a NULL stamp means "never asked", which is the
    correct starting state for every existing row, so the whole catalog is
    queued on upgrade and drains at one 50-id request per backfill cycle."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addTrackIsrcAttemptedColumnIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added tracks.isrc_attempted_at column.")
        self.updateAppVersion("1.48.0")


if __name__ == "__main__":
    Migrator("1.47.0", "1.48.0").migrate()
