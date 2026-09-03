# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """1.52.0 -> 1.53.0: the two halves of making track merging and the genre
    backfill agree about what a SONG is.

    1. track_merge_decisions.carried_canonical_id - where the MATCHER moved a
       person's verdict, leaving canonical_id saying where the person PUT it.
       The matcher's carry-along re-points every dependent of a member it
       re-homes, and it used to rewrite the audit row too, with no decided_by
       filter. A hand-made merge therefore came out reading "pointed at the
       matcher's head, decided_by=admin" - a decision attributed to someone who
       never made it - and since track_id is that table's PRIMARY KEY there is
       no history, so the release actually chosen was gone. The toggle's off
       edge then had nothing to put back, which is exactly the promise
       unmergeAllIsrcMerges' docstring makes.

       Arrives NULL everywhere, the correct reading of every existing row:
       "still where the person put it". Verdicts the matcher had already
       re-homed before this column existed cannot be recovered - the target was
       overwritten in place - and inventing one would put a release on a
       decision nobody made.

    2. The backfill requeue for merge-group canonicals holding no own genre
       rows. Every genre READ resolves the group (track_genres joined on
       COALESCE(canonical_id, id)) while the queue and the write keyed on the
       played release, so a lookup that landed on a member became unreadable:
       the distribution queries return nothing for those plays, coverage counts
       them uncovered, the card badges empty out. The queue now asks about the
       canonical, but that alone leaves the existing backlog stuck - the member
       was looked up and marked, the canonical never was, and nothing would
       requeue it.

       Nothing is copied or deleted. Members keep their rows and their stamps,
       so unmerging stays lossless and the canonical gets its OWN Last.fm
       answer rather than a differently-titled release's. The cost is that
       coverage recovers over backfill cycles rather than instantly, which is
       the honest trade for not attributing one release's tags to another.

    Idempotent: the column add is guarded by PRAGMA table_info, and the requeue
    matches only rows that still have a stamp AND no own genre rows, so a
    second run clears nothing."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addMergeDecisionCarriedColumnIfMissing()
            requeued = repo.requeueCanonicalsOfMergedGroupsWithoutOwnGenres()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added track_merge_decisions.carried_canonical_id.")
        print(f"Requeued {requeued} merge-group canonical(s) for Last.fm genre lookup; "
              f"their songs' genres were written to a release no read resolves.")
        self.updateAppVersion("1.53.0")


if __name__ == "__main__":
    Migrator("1.52.0", "1.53.0").migrate()
