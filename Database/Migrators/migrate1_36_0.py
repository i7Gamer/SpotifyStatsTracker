try:
    from Database.Migrators.base import BaseMigrator
except ModuleNotFoundError:
    from base import BaseMigrator


class Migrator(BaseMigrator):
    """1.36.0 -> 1.37.0: version-only, no schema change. Everything shipped in
    1.37.0 is UI/CSP-only - the detail pages' "Play now" Spotify embed replaces
    the "Open in Spotify" link with a button that reveals an embedded iFrame API
    player, plus the frame-src/script-src Content-Security-Policy allowances it
    needs. None of it adds or alters a table or column.

    The migration loop (Database/Migrators/migrate.py) still imports a
    migrate{major}_{minor}_0 module for every consecutive minor version it
    steps through, so this step must exist even though it only advances the
    version marker. Without it, an existing 1.36.0 install would crash on
    startup hunting for a migrate1_36_0 file that isn't there."""

    def migrate(self):
        self.checkPreconditions()
        self.updateAppVersion("1.37.0")


if __name__ == "__main__":
    Migrator("1.36.0", "1.37.0").migrate()
