try:
    from Database.Migrators.base import BaseMigrator
except ModuleNotFoundError:
    from base import BaseMigrator


class Migrator(BaseMigrator):
    """1.34.0 -> 1.35.0: version-only, no schema change. Everything shipped in
    1.35.0 is UI/route-only - the dashboard streak calendar, the play-history
    split to /history plus the Next-milestones panel, the Insights/Account nav
    grouping, deferred charts/genres AJAX + time-period filter, and the profile
    "show more" milestones - none of which add or alter a table or column.

    The migration loop (Database/Migrators/migrate.py) still imports a
    migrate{major}_{minor}_0 module for every consecutive minor version it
    steps through, so this step must exist even though it only advances the
    version marker. Without it, an existing 1.34.0 install would crash on
    startup hunting for a migrate1_34_0 file that isn't there."""

    def migrate(self):
        self.checkPreconditions()
        self.updateAppVersion("1.35.0")


if __name__ == "__main__":
    Migrator("1.34.0", "1.35.0").migrate()
