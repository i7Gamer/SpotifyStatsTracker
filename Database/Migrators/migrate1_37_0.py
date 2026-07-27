# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import BaseMigrator
except ModuleNotFoundError:
    from base import BaseMigrator


class Migrator(BaseMigrator):
    """1.37.0 -> 1.38.0: adds user_tags table for personal tagging system.
    The table is created by the SCHEMA IF NOT EXISTS guard; this step
    only advances the version marker."""

    def migrate(self):
        self.checkPreconditions()
        self.updateAppVersion("1.38.0")


if __name__ == "__main__":
    Migrator("1.37.0", "1.38.0").migrate()
