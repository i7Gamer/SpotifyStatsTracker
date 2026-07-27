# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """1.41.0 -> 1.42.0: re-materializes plays.is_skip so tracks shorter than
    the instance skip threshold stop reporting as 100% skipped.

    Seconds mode used to compare time_played against the raw threshold with no
    reference to the track's duration. A track shorter than the threshold can
    never reach it, so every play of it was stored as a skip - including plays
    that ran to the last millisecond. On the instance this was found on, a
    22.174s intro under a 30s threshold had all 17 of its plays flagged, while
    Spotify's own export recorded every one as skipped=false / trackdone.

    computeIsSkip now caps the threshold at the track's own duration, but that
    only governs rows written from here on; the rows already on disk keep their
    stale flag until something rewrites them. recomputeSkipFlags is the same
    operation the admin threshold form already triggers, so this is a re-run of
    an existing, idempotent maintenance op rather than a bespoke fix-up: it
    rewrites every row from time_played under the current threshold, which is
    unchanged by this migration.

    Rows whose classification was already correct are rewritten to the same
    value, so running it again is a no-op."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            mode, value = repo.getSkipThreshold()
            processed = repo.recomputeSkipFlags()
        finally:
            repo.connectionManager.close()

        print(f"Recomputed is_skip for {processed} play(s) under the {mode} "
              f"threshold of {value}; short tracks played in full are no longer skips.")
        self.updateAppVersion("1.42.0")


if __name__ == "__main__":
    Migrator("1.41.0", "1.42.0").migrate()
