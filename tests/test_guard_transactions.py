# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The reads a write-verb decides from must run inside its write transaction.

Python's sqlite3 under legacy transaction control BEGINs only for DML: a
SELECT inside `with conn:` runs in autocommit and holds no lock, so a verb
that checks in one statement and writes in another decides from state a
concurrent writer can change before the write lands. Three sites were pinned
individually when the 2026-08-15 review found the class (dismissMergeCandidate,
saveCachedWrapped, mergePlaySkipsIntoPlays); this file pins the guard-shaped
verbs the 2026-08-16 sweep of all 100 `with conn:` blocks then turned up.

The pinned property is positional, not a spelling: every SELECT issued before
the verb's FIRST write must already hold the transaction. Reads after the
writes stay exempt on purpose - the merge verbs re-expand group membership
post-commit for the cache-invalidation scope, and those reads are covered by
the wrapped generation check (saveCachedWrapped discards a snapshot that
straddled them), not by this lock.

Pinned structurally rather than by racing threads, with the recording
technique test_wrapped_invalidation_scope introduced: a test that has to lose
a race to fail is a test that passes by luck.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, RecordingConnection

USER = "alice"
WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE")


class GuardTransactionTestCase(DatabaseTestCase):
    def _db(self):
        return self._makeDb({}, [])

    def _seedTrack(self, db, trackId, name="Same Song", isrc=None,
                   canonicalId=None, createdAt=1000.0, plays=0, linkArtist=True):
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT OR IGNORE INTO users (username, created_at) "
                         "VALUES (?, 0)", (USER,))
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) "
                         "VALUES ('alb', 'Album', '')")
            conn.execute("INSERT OR IGNORE INTO artists (id, name, url) "
                         "VALUES ('art', 'Artist', '')")
            conn.execute(
                "INSERT INTO tracks (id, name, url, album_id, duration_ms, isrc, "
                "created_at, canonical_id) VALUES (?, ?, '', 'alb', 200000, ?, ?, ?)",
                (trackId, name, isrc, createdAt, canonicalId))
            if linkArtist:
                conn.execute("INSERT INTO track_artists (track_id, artist_id, position) "
                             "VALUES (?, 'art', 0)", (trackId,))
            for i in range(plays):
                conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                             "VALUES (?, ?, ?, 200000)", (USER, trackId, 1e9 + i))

    def _statementsFor(self, db, call):
        log = []
        proxy = RecordingConnection(db.repo._conn(), log)
        #< instance attribute shadowing the class method; `del` restores the
        #  class lookup exactly, rather than freezing a bound method back on
        db.repo._conn = lambda: proxy
        try:
            call()
        finally:
            del db.repo._conn
        return log

    def _assertDecisionReadsHoldTheLock(self, statements):
        """Every SELECT before the first write ran with the transaction open."""
        writes = [i for i, (sql, _) in enumerate(statements)
                  if sql.upper().startswith(WRITE_PREFIXES)]
        self.assertTrue(writes, "the verb never wrote - the fixture exercised nothing")
        preWriteSelects = [(sql, inTx) for sql, inTx in statements[:writes[0]]
                           if sql.upper().startswith("SELECT")]
        self.assertTrue(preWriteSelects,
                        "the verb never read before writing - the guard under test is gone")
        for sql, inTx in preWriteSelects:
            self.assertTrue(inTx, f"decision read holds no lock: {sql}")

    def _canonical(self, db, trackId):
        return db.repo._conn().execute(
            "SELECT canonical_id FROM tracks WHERE id=?", (trackId,)).fetchone()[0]


class TestManualMergeDecidesUnderTheLock(GuardTransactionTestCase):
    """mergeTrackManually resolves the target, checks both tracks exist, reads
    the current pointer and snapshots the leaving group - all decisions about
    what the writes below will do. Read outside the transaction, a matcher
    pass re-heading the resolved root between resolve and write leaves trackId
    pointing at a track that itself now points away: a chain, which every
    reader resolves exactly one hop of."""

    def test_every_read_before_the_first_write_is_in_the_transaction(self):
        db = self._db()
        self._seedTrack(db, "trA")
        self._seedTrack(db, "trB")

        statements = self._statementsFor(
            db, lambda: db.repo.mergeTrackManually("trB", "trA", "tester"))

        self._assertDecisionReadsHoldTheLock(statements)
        self.assertEqual(self._canonical(db, "trB"), "trA")

    def test_a_member_target_still_lands_on_its_canonical(self):
        """The resolve is a decision read too: merging INTO a member must land
        on that member's canonical, so the resolve has to see the membership
        the write acts on."""
        db = self._db()
        self._seedTrack(db, "trA")
        self._seedTrack(db, "trB", canonicalId="trA")
        self._seedTrack(db, "trC")

        statements = self._statementsFor(
            db, lambda: db.repo.mergeTrackManually("trC", "trB", "tester"))

        self._assertDecisionReadsHoldTheLock(statements)
        self.assertEqual(self._canonical(db, "trC"), "trA")


class TestUnmergeDecidesUnderTheLock(GuardTransactionTestCase):
    """unmergeTrack checks the track exists and snapshots the group it is
    leaving - the scope of the cache invalidation after the split. Snapshot
    and split must see the same membership."""

    def test_the_existence_check_and_group_snapshot_hold_the_lock(self):
        db = self._db()
        self._seedTrack(db, "trA")
        self._seedTrack(db, "trB", canonicalId="trA")

        statements = self._statementsFor(
            db, lambda: db.repo.unmergeTrack("trB", "tester"))

        self._assertDecisionReadsHoldTheLock(statements)
        self.assertIsNone(self._canonical(db, "trB"))


class TestRemoveTagResolvesUnderTheLock(GuardTransactionTestCase):
    """removeTag's track arm resolves the canonical, then deletes across the
    merge group built from that answer. A re-head between resolve and DELETE
    makes the group subselect miss members - the tag survives its own removal
    (until a second click resolves the new head)."""

    def test_the_canonical_resolve_holds_the_lock(self):
        db = self._db()
        self._seedTrack(db, "trA")
        self._seedTrack(db, "trB", canonicalId="trA")
        db.repo.addTag(USER, "mood", "track", "trB")   # lands on the canonical
        conn = db.repo._conn()
        with conn:
            #< a row written before the merge existed, the shape the group
            #  delete exists for
            conn.execute("INSERT INTO user_tags (username, tag, entity_type, entity_id, "
                         "created_at) VALUES (?, 'mood', 'track', 'trB', 0)", (USER,))

        statements = self._statementsFor(
            db, lambda: db.repo.removeTag(USER, "mood", "track", "trB"))

        self._assertDecisionReadsHoldTheLock(statements)
        remaining = db.repo._conn().execute(
            "SELECT COUNT(*) FROM user_tags WHERE username=? AND tag='mood'",
            (USER,)).fetchone()[0]
        self.assertEqual(remaining, 0)


class TestArtistRepairDecidesUnderTheLock(GuardTransactionTestCase):
    """addMissingTrackArtists guards on "this track has NO links" before
    inserting links. The backfiller and an import can both reach the same
    blanked track; both passing the unlocked check ends in doubled links or an
    IntegrityError aborting whichever lands second."""

    def test_the_no_links_guard_holds_the_lock(self):
        db = self._db()
        self._seedTrack(db, "trX", linkArtist=False)
        artists = [{"id": "artNew", "name": "New Artist", "url": "", "imageId": None}]

        statements = self._statementsFor(
            db, lambda: self.assertTrue(db.repo.addMissingTrackArtists("trX", artists)))

        self._assertDecisionReadsHoldTheLock(statements)
        links = db.repo._conn().execute(
            "SELECT COUNT(*) FROM track_artists WHERE track_id='trX'").fetchone()[0]
        self.assertEqual(links, 1)


class TestIsrcMergeCarryAlongReadsUnderTheLock(GuardTransactionTestCase):
    """mergeTracksByIsrc expands each member's dependents mid-write to carry
    them to the new canonical ("every reader resolves exactly one hop"). Those
    reads decide writes, so they must hold the lock.

    Only the carry-along reads (`... WHERE canonical_id = ?`) are pinned. The
    PLAN deliberately reads outside the transaction: the matcher is
    single-flighted (claimTrackMergeRun), a manual reject landing in the gap
    yields to ISRC by design (2026-08-10 review), and a manual merge landing
    in the gap is what the carry-along re-reads membership for - inside the
    lock, which is exactly the property here."""

    CARRY_ALONG_MARKER = "WHERE canonical_id = ?"

    def test_the_dependent_expansion_reads_hold_the_lock(self):
        db = self._db()
        self._seedTrack(db, "trA", isrc="USRC11111111", createdAt=1000.0, plays=2)
        self._seedTrack(db, "trB", isrc="USRC11111111", createdAt=2000.0, plays=1)
        #< a manual merge's anchor sitting on a member: the carry-along's reason
        self._seedTrack(db, "trC", canonicalId="trB")

        statements = self._statementsFor(db, lambda: db.repo.mergeTracksByIsrc())

        carryReads = [(sql, inTx) for sql, inTx in statements
                      if sql.upper().startswith("SELECT")
                      and self.CARRY_ALONG_MARKER in sql]
        self.assertTrue(carryReads,
                        "no carry-along read fired - the fixture lost its dependent")
        for sql, inTx in carryReads:
            self.assertTrue(inTx, f"carry-along read holds no lock: {sql}")
        #< whichever head the election picked, one hop must resolve everything
        heads = [t for t in ("trA", "trB") if self._canonical(db, t) is None]
        self.assertEqual(len(heads), 1, "exactly one track leads the merged group")
        elected = heads[0]
        member = "trB" if elected == "trA" else "trA"
        self.assertEqual(self._canonical(db, member), elected)
        self.assertEqual(self._canonical(db, "trC"), elected,
                         "the dependent was not carried to the elected head")


if __name__ == "__main__":
    import unittest
    unittest.main()
