try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """1.39.0 -> 1.40.0: adds users.hide_now_playing, the per-user opt-out for
    broadcasting your current track to people you share with (the dashboard's
    friends-listening strip). The admin's instance-wide
    friends_now_playing_enabled switch is a plain app_settings row (created on
    first write, absent = enabled, same contract as every other feature
    toggle), so only the users column ALTER needs an explicit migration step."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addHideNowPlayingColumnIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added users.hide_now_playing column.")
        self.updateAppVersion("1.40.0")


if __name__ == "__main__":
    Migrator("1.39.0", "1.40.0").migrate()
