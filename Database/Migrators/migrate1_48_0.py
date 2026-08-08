# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """1.48.0 -> 1.49.0: adds tracks.canonical_id and track_merge_decisions,
    the storage for treating one song as one song.

    Spotify lists the same recording once per release it appears on - a single,
    an album, a compilation - and gives each its own track id. A listener who
    played "the song" from two of those has their count split between two
    catalog rows, and both are ranked separately. Measured on a real library:
    456 groups covering 482 redundant ids and 1,203 plays, which is ~1% of the
    history but concentrated exactly where it shows, in the Top lists.

    canonical_id names the track a row has been merged INTO; NULL means "not
    merged", which is also "is its own canonical track". track_merge_decisions
    records why each verdict was reached and whether a person or the matcher
    reached it - so a merge can be explained, reversed, and pinned against a
    later automatic pass. A NULL canonical_id THERE is a deliberate "not the
    same recording", which is a different statement from having no row (never
    looked at), and is what stops a rejected pair being re-proposed forever.

    Nothing is merged here and nothing reads canonical_id yet. Both are
    deliberate: the schema ships on its own, ahead of the matcher that fills it
    and well ahead of the read paths that would honour it, so an upgrade cannot
    move anybody's numbers. That also means there is nothing to backfill - NULL
    is already the right answer for every existing row."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addTrackCanonicalIdColumnIfMissing()
            repo.addTrackMergeDecisionsTableIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added tracks.canonical_id and the track_merge_decisions table.")
        self.updateAppVersion("1.49.0")


if __name__ == "__main__":
    Migrator("1.48.0", "1.49.0").migrate()
