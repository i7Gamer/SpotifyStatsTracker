# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """1.38.0 -> 1.39.0: adds users.hide_tags_panel and the admin's
    instance-wide tags_enabled kill switch. tags_enabled is a plain
    app_settings row (created on first write, absent = enabled, same
    contract as every other feature toggle), so only the users column ALTER
    needs an explicit migration step."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addHideTagsPanelColumnIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added users.hide_tags_panel column.")
        self.updateAppVersion("1.39.0")


if __name__ == "__main__":
    Migrator("1.38.0", "1.39.0").migrate()
