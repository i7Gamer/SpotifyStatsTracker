# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """1.44.0 -> 1.45.0: drops every cached Wrapped year so it rebuilds.

    user_wrapped stores fully-materialized results, and staleness is judged by
    exactly two signals - the year's max played_at and its play count. That
    catches new listening and nothing else, which is correct for the case it was
    written for and blind to every case where the same plays produce a different
    answer. Three of those landed at once:

      1.43.0  a day whose only activity was a skip stopped counting as a
              listening day, so longest_streak (cached per year) changed for
              every year containing one - 38 such days across the live
              database's two active users.
      1.44.0  the dangling-row repair made previously-invisible plays joinable,
              which changes top_songs / unique_songs / discovered_* - all
              materialized in the cache, and all computed through a JOIN the
              staleness count doesn't use.
      1.44.0  the TZ-unset default became a real zone rather than an offset
              frozen at import, moving day and year boundaries for every user
              without a profile timezone.

    None of the three changes a play count or a max timestamp, so nothing would
    ever have recalculated: the pre-fix figures would have been served forever
    for every past year, while the dashboard - which queries live - showed the
    corrected ones. Same page, two answers.

    Deleting is the whole fix. The rows are a cache: the background worker
    rebuilds each year on its next pass, and a page load rebuilds on a miss. The
    cost is one recalculation per user-year, which is what the worker exists to
    do. From here on a timezone change invalidates on its own (see
    Repository.updateUserSettings) - this is for the changes that already
    happened.

    Re-runnable: a second pass finds nothing to delete."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            conn = repo._conn()
            with conn:
                dropped = conn.execute("DELETE FROM user_wrapped").rowcount
        finally:
            repo.connectionManager.close()

        print(f"Dropped {dropped} cached Wrapped year(s); they rebuild from current data on next use.")
        self.updateAppVersion("1.45.0")


if __name__ == "__main__":
    Migrator("1.44.0", "1.45.0").migrate()
