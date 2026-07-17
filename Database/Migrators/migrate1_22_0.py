try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Adds the behavioral play metadata columns (platform, conn_country,
    reason_start, reason_end, shuffle, skipped, offline, incognito) to plays
    and ensures the play_skips table exists (skip events shorter than
    SKIP_THRESHOLD_MS, kept separate from plays)."""

    def migrate(self):
        self.checkPreconditions()

        # Constructing the Repository runs SCHEMA on connect, which creates
        # play_skips (and its index) on its own - only the pre-existing plays
        # table needs explicit ALTERs.
        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addPlayBehavioralColumnsIfMissing()
        finally:
            repo.connectionManager.close()

        print("Added behavioral play columns and play_skips table.")
        self.updateAppVersion("1.23.0")


if __name__ == "__main__":
    Migrator("1.22.0", "1.23.0").migrate()
