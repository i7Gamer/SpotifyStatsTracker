# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """1.49.0 -> 1.50.0: adds track_merge_decisions.against_id, the other half
    of a "not the same recording".

    The review queue proposes a release against the one keeping the song's
    page, and a person rules on the pair. Only the release ruled ON was ever
    recorded, so the "Kept separate" log could say what was rejected but never
    what it was rejected against - which is exactly the context a decision
    needs to be re-checked months later, when the reason it was obvious has
    gone.

    An audit column, and nothing more: the verdict still applies to the track
    itself, which leaves the queue for good whatever it was compared with. No
    read path changes behaviour because of it, so an upgrade cannot move
    anybody's numbers or re-open a settled pair.

    Nothing to backfill - NULL is the honest answer for every rejection
    recorded before the column existed. Nothing knows what those were compared
    with, and a guess would put a name on a decision nobody made."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addMergeDecisionAgainstColumnIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added track_merge_decisions.against_id.")
        self.updateAppVersion("1.50.0")


if __name__ == "__main__":
    Migrator("1.49.0", "1.50.0").migrate()
