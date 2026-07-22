try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Adds users.milestones_baseline_at for the per-user achievement
    milestones feature: lifetime play-count / listen-time thresholds, listening
    streaks, and #1-artist changes surface as a topbar badge plus a Milestones
    section on /profile. The baseline timestamp records a user's first
    detection pass so everything they'd already achieved by then is seeded as
    seen (no notification) - only milestones crossed afterwards notify. The
    user_milestones table itself is created by SCHEMA's CREATE TABLE IF NOT
    EXISTS on the next connect, so this migration only needs the column ALTER."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addMilestonesBaselineColumnIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added users.milestones_baseline_at column.")
        self.updateAppVersion("1.34.0")


if __name__ == "__main__":
    Migrator("1.33.0", "1.34.0").migrate()
