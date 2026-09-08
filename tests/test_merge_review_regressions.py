"""Manual verdicts survive stale plans and interacting automatic carries."""
from unittest.mock import patch

from Database.repository import Repository
from test_track_merge_manual_restore import ManualRestoreTestCase


class TestMergePlanRevalidation(ManualRestoreTestCase):
    def test_manual_verdicts_in_the_plan_apply_gap_survive(self):
        for action in ("member_split", "member_move", "head_move", "head_split"):
            with self.subTest(action=action):
                db = self._db()
                self._track(db, "A", isrc="ISRC1", name="Song")
                self._track(db, "B", isrc="ISRC1", name="Song - 2005 Remaster")
                self._track(db, "D", name="Other")
                other = Repository(db.repo.connectionManager.dbPath)
                self.addCleanup(other.connectionManager.close)
                original = db.repo._planIsrcMerges
                moved = "A" if action.startswith("head") else "B"

                def raced_plan(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if action.endswith("move"):
                        other.mergeTrackManually(moved, "D", decidedBy="admin")
                    else:
                        other.unmergeTrack(moved, decidedBy="admin")
                    return result

                with patch.object(db.repo, "_planIsrcMerges", side_effect=raced_plan):
                    assert db.repo.mergeTracksByIsrc() == {"groups": 0, "merged": 0}
                assert self._decision(db, moved)["decided_by"] == "admin"
                assert self._canonical(db, moved) == ("D" if action.endswith("move") else None)
                self._assertNoChains(db)

    def test_reheading_is_skipped_when_the_old_head_is_pinned(self):
        db = self._db()
        self._track(db, "B", isrc="ISRC1", name="Song - 2005 Remaster")
        self._track(db, "A", isrc="ISRC1", name="Song")
        self._mergedUnderThePlaysOnlyRule(db, "B", "A")
        original = db.repo._planIsrcMerges

        def raced_plan(*args, **kwargs):
            plan = original(*args, **kwargs)
            db.repo.unmergeTrack("B", decidedBy="admin")
            return plan

        with patch.object(db.repo, "_planIsrcMerges", side_effect=raced_plan):
            assert db.repo.mergeTracksByIsrc() == {"groups": 0, "merged": 0}
        assert self._canonical(db, "A") == "B"
        assert self._canonical(db, "B") is None

    def test_preview_keeps_its_public_shape(self):
        db = self._db()
        self._track(db, "A", isrc="ISRC1", name="Song")
        self._track(db, "B", isrc="ISRC1", name="Song - 2005 Remaster")
        group = db.repo.previewMergeTracksByIsrc()["groups"][0]
        assert set(group) == {"isrc", "canonical", "reHeadedFrom", "members", "plays"}
        assert set(group["canonical"]) == {"trackId", "name"}
        assert set(group["members"][0]) == {"trackId", "name", "plays"}

    def test_a_new_unmerged_plain_release_can_rehead_an_existing_group(self):
        db = self._db()
        for track_id in ("A", "B"):
            self._track(db, track_id, isrc="ISRC1", name="Song - 2005 Remaster")
        self._mergedUnderThePlaysOnlyRule(db, "B", "A")
        self._track(db, "C", isrc="ISRC1", name="Song")
        assert db.repo.mergeTracksByIsrc() == {"groups": 1, "merged": 1}
        assert self._canonical(db, "A") == "C"
        assert self._canonical(db, "B") == "C"
        assert self._canonical(db, "C") is None


class TestFinalMergeRestoration(ManualRestoreTestCase):
    def test_multiple_carried_decisions_restore_the_final_group_in_any_order(self):
        for order in ("CBADE", "EDABC"):
            with self.subTest(order=order):
                db = self._db()
                for track_id in order:
                    name = "Song - 2005 Remaster" if track_id in ("B", "D") else "Song"
                    self._track(db, track_id, isrc="ISRC1" if track_id in ("A", "B") else None, name=name)
                repo = db.repo
                repo.mergeTrackManually("C", "B", decidedBy="admin")
                repo.mergeTracksByIsrc()
                repo.mergeTrackManually("B", "D", decidedBy="admin")
                with repo._conn() as conn:
                    conn.execute("UPDATE tracks SET isrc='ISRC2' WHERE id IN ('D', 'E')")
                repo.mergeTracksByIsrc()
                assert repo.unmergeAllIsrcMerges() == 1
                assert self._canonical(db, "B") == "D"
                assert self._canonical(db, "C") == "D"
                assert self._decision(db, "C")["canonical_id"] == "B"
                assert self._decision(db, "B")["canonical_id"] == "D"
                assert self._decision(db, "C")["carried_canonical_id"] is None
                self._assertNoChains(db)
                assert repo.unmergeAllIsrcMerges() == 0

    def test_disable_and_revert_commit_together(self):
        db = self._db()
        self._track(db, "A", isrc="ISRC1", name="Song")
        self._track(db, "B", isrc="ISRC1", name="Song - 2005 Remaster")
        db.repo.setTrackMergeEnabled(True)
        db.repo.mergeTracksByIsrc()
        assert db.repo.unmergeAllIsrcMerges(disableSetting=True) == 1
        assert not db.repo.isTrackMergeEnabled()
        assert self._canonical(db, "B") is None

    def test_invalid_restoration_rolls_back_the_toggle_and_decisions(self):
        db = self._db()
        for track_id in ("A", "B", "C"):
            self._track(db, track_id)
        repo = db.repo
        repo.setTrackMergeEnabled(True)
        with repo._conn() as conn:
            for source, target in (("B", "C"), ("C", "B")):
                conn.execute("UPDATE tracks SET canonical_id='A' WHERE id=?", (source,))
                conn.execute("INSERT INTO track_merge_decisions "
                             "(track_id, canonical_id, reason, decided_at, decided_by, carried_canonical_id) "
                             "VALUES (?, ?, 'manual-merge', 1000, 'admin', 'A')", (source, target))
        before = [tuple(row) for row in repo._conn().execute("SELECT * FROM track_merge_decisions")]
        with self.assertRaises(ValueError):
            repo.unmergeAllIsrcMerges(disableSetting=True)
        assert repo.isTrackMergeEnabled()
        assert self._canonical(db, "B") == "A"
        assert [tuple(row) for row in repo._conn().execute("SELECT * FROM track_merge_decisions")] == before
