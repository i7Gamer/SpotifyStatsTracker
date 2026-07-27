# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """1.42.0 -> 1.43.0: re-materializes plays.is_skip so a play that counts as
    complete stops also counting as a skip.

    The instance has two independent tunables - the skip threshold and the
    completion percent (getCompletionCompletePercent, the complete-vs-partial
    boundary of the Charts completion pie and the "Full plays only" filter) -
    and nothing tied them together. A play could satisfy both at once: counted
    as listened-to-the-end by one and as abandoned by the other, on the same
    page. Seconds mode reached that on any track shorter than the threshold,
    where the previous fix capped the comparison at the track's full duration,
    so a play at 80% of a 20s intro was complete everywhere and still a skip
    here; percent mode reaches it whenever the skip percent sits above the
    completion percent.

    computeIsSkip now caps at the completion boundary in both modes, but that
    only governs rows written from here on; the rows already on disk keep their
    stale flag until something rewrites them. recomputeSkipFlags is the same
    operation the admin threshold form already triggers, so this is a re-run of
    an existing, idempotent maintenance op rather than a bespoke fix-up: it
    rewrites every row from time_played under the current settings, neither of
    which this migration changes.

    Rows whose classification was already correct are rewritten to the same
    value, so running it again is a no-op."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            mode, value = repo.getSkipThreshold()
            completionPercent = repo.getCompletionCompletePercent()
            processed = repo.recomputeSkipFlags()
        finally:
            repo.connectionManager.close()

        print(f"Recomputed is_skip for {processed} play(s) under the {mode} threshold of "
              f"{value}; plays reaching {completionPercent}% of their track are no longer skips.")
        self.updateAppVersion("1.43.0")


if __name__ == "__main__":
    Migrator("1.42.0", "1.43.0").migrate()
