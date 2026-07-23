try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
    from services.milestones import recalculateMilestoneDates, resolveUserTimezone
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository
    from services.milestones import recalculateMilestoneDates, resolveUserTimezone


class Migrator(BaseMigrator):
    """1.35.0 -> 1.36.0: recalculate user_milestones.achieved_at from play
    history. The 1.34.0 milestones feature's first detection pass seeded every
    already-achieved milestone with the seeding moment itself, so existing
    accounts show their whole milestone backlog as achieved on migration day.
    The real dates are derivable from the plays table - the Nth play, the
    cumulative listen-time crossing, the first-ever consecutive-day run, the
    moment the #1 artist took the lead - see services/milestones.py
    recalculateMilestoneDates for the per-kind rules and the deliberately
    untouched cases (thresholds today's data no longer supports, multiple
    top_artist rows). seen flags and users.milestones_baseline_at stay as they
    are, so nothing re-notifies. Historical streaks that were never recorded
    at all (seeding only looked at the then-current streak) are NOT
    backfilled - this only corrects dates on existing rows. No schema change;
    idempotent (a re-run recomputes the same dates)."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            users = 0
            updated = 0
            for username in repo.getMilestoneUsernames():
                users += 1
                updated += recalculateMilestoneDates(
                    repo, username, resolveUserTimezone(repo, username))
            repo.commit()
        finally:
            repo.connectionManager.close()

        print(f"Recalculated {updated} milestone date(s) across {users} user(s).")
        self.updateAppVersion("1.36.0")


if __name__ == "__main__":
    Migrator("1.35.0", "1.36.0").migrate()
