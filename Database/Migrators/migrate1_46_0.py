# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """1.46.0 -> 1.47.0: adds users.default_top_list_window, the Top
    Songs/Artists/Albums pages' own default time window.

    It exists because one column was answering two different questions. The
    Dashboard, Charts, Genres and Compare all open on "what have I been
    listening to lately", so default_dashboard_window is theirs. The three Top
    pages are a career ranking and were hardcoded to all-time. Pointing them at
    the existing column would have made a "Last Week" dashboard silently mean a
    last-week all-time ranking, which is not the same setting wearing two hats.

    Nothing to backfill, and that is the point rather than a convenience: the
    column's DEFAULT is 'all time', which is exactly what every existing
    account's Top pages were already showing, so nobody's view moves on
    upgrade."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addTopListWindowColumnIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added users.default_top_list_window column.")
        self.updateAppVersion("1.47.0")


if __name__ == "__main__":
    Migrator("1.46.0", "1.47.0").migrate()
