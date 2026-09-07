# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import BaseMigrator
except ModuleNotFoundError:
    from base import BaseMigrator


class Migrator(BaseMigrator):
    """1.53.0 -> 1.54.0: no schema change. 1.54.0 ships five features
    (milestone email, Listening Behavior card, artist single-track timeline,
    two /admin rows) and the 2026-09-07 review's fixes, all on existing
    tables - but the chain steps at minor granularity and migrateIfNeeded
    imports migrate<major>_<minor>_0 for every minor between the database and
    the app, so a minor bump needs this file even when it only advances the
    marker (see migrate1_37_0 for the previous such step)."""

    def migrate(self):
        self.checkPreconditions()
        self.updateAppVersion("1.54.0")


if __name__ == "__main__":
    Migrator("1.53.0", "1.54.0").migrate()
