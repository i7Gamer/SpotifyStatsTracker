try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import (
        Repository, SKIP_THRESHOLD_MODE_KEY,
        SKIP_THRESHOLD_DEFAULT_MODE, SKIP_THRESHOLD_DEFAULT_VALUE,
    )
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import (
        Repository, SKIP_THRESHOLD_MODE_KEY,
        SKIP_THRESHOLD_DEFAULT_MODE, SKIP_THRESHOLD_DEFAULT_VALUE,
    )


class Migrator(BaseMigrator):
    """1.32.0 -> 1.33.0: merge play_skips back into plays.

    Rebuilds the plays table to add is_skip and relax the time_played CHECK to
    >= 0, folds every play_skips row in as is_skip=1 (then drops play_skips), and
    seeds the instance-wide skip threshold at its default (seconds/5) - the
    boundary the rebuild used to classify the existing plays. Retires the
    separate skip table and getCompletionStats' 30s line in favour of one
    admin-tunable threshold. See Repository.mergePlaySkipsIntoPlays."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            result = repo.mergePlaySkipsIntoPlays()
            # Seed the threshold default only when unset, so re-running (or a
            # future re-migration) never clobbers an admin's chosen value.
            if repo.getAppSetting(SKIP_THRESHOLD_MODE_KEY) is None:
                repo.setSkipThreshold(SKIP_THRESHOLD_DEFAULT_MODE, SKIP_THRESHOLD_DEFAULT_VALUE)
            repo.commit()
        finally:
            repo.connectionManager.close()

        print(f"Merged play_skips into plays ({result}); seeded skip threshold default.")
        self.updateAppVersion("1.33.0")


if __name__ == "__main__":
    Migrator("1.32.0", "1.33.0").migrate()
