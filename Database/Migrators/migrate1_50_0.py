# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """1.50.0 -> 1.51.0: adds users.session_version, the counter that lets a
    password reset - and the new "Sign out everywhere" button - end sessions on
    devices the browser doing it cannot reach.

    Sessions here are signed COOKIES with no server-side store, so until now
    "log out" only ever meant "clear the cookie in front of me". Every other
    device kept a valid session for the rest of its 30 days, including after a
    password reset, which is the one moment people expect the opposite.

    Arrives at 0 for everyone, and that is the whole upgrade story: a cookie
    minted before this column existed carries no version at all, and the guard
    reads a missing one as 0 - so nobody is logged out by the upgrade itself.
    The first bump invalidates those older cookies too, and by then their owner
    has explicitly asked for exactly that.

    Nothing to backfill, and no read path changes what it shows because of
    it."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addUserSessionVersionColumnIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added users.session_version.")
        self.updateAppVersion("1.51.0")


if __name__ == "__main__":
    Migrator("1.50.0", "1.51.0").migrate()
