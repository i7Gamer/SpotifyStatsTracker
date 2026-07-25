"""The shared WHERE/JOIN fragment builders (Database/queries/_base.py).

These used to be copy-pasted per query - eleven copies of the full-plays filter,
eleven of the id-set clause - which is how the skip queries ended up honouring
neither. Now one definition feeds every caller, so the contracts below are worth
pinning directly rather than only through the queries that use them:

  1. each builder appends its bound values to `params` itself, in the order its
     placeholders appear, and returns only the SQL text
  2. an empty id list is an explicit empty set (match nothing), not "no filter"
  3. a track with unknown duration survives the full-plays filter
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest
from Database.repository import Repository


class TestIdSetClause(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(Path(":memory:"))
        self.addCleanup(self.repo.connectionManager.close)
        self.params = []

    def test_none_means_no_filter(self):
        self.assertEqual(self.repo._idSetClause(self.params, "t.id", None), "")
        self.assertEqual(self.params, [])

    def test_an_empty_list_matches_nothing(self):
        """Not the same as None. A tag nobody has used must show an empty page,
        not the whole library - and `IN ()` is a syntax error in SQLite, hence
        the bare AND 0."""
        self.assertEqual(self.repo._idSetClause(self.params, "t.id", []), " AND 0")
        self.assertEqual(self.params, [])

    def test_ids_become_bound_placeholders(self):
        clause = self.repo._idSetClause(self.params, "t.id", ["a", "b", "c"])

        self.assertEqual(clause, " AND t.id IN (?,?,?)")
        self.assertEqual(self.params, ["a", "b", "c"])

    def test_the_column_is_the_callers_and_the_ids_are_never_inlined(self):
        """The column name is interpolated (a column can't be bound), the
        values never are - so a hostile id is data, not syntax."""
        clause = self.repo._idSetClause(self.params, "p.track_id", ["'); DROP TABLE plays --"])

        self.assertEqual(clause, " AND p.track_id IN (?)")
        self.assertEqual(self.params, ["'); DROP TABLE plays --"])

    def test_it_appends_rather_than_replacing_existing_params(self):
        params = ["alice", 123]

        self.repo._idSetClause(params, "t.id", ["x"])

        self.assertEqual(params, ["alice", 123, "x"])


class TestFullPlaysClause(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.db = self._makeDb({}, [])
        self.repo = self.db.repo

    def test_it_binds_the_admins_percent_as_a_ratio(self):
        params = []

        clause = self.repo._fullPlaysClause(params)

        self.assertIn("p.time_played >= t.duration_ms * ?", clause)
        self.assertEqual(params, [self.repo.getCompletionCompletePercent() / 100.0])

    def test_unknown_durations_survive_the_filter(self):
        """A track whose duration never arrived can't be judged; dropping it
        would hide every play of it instead of just not counting one."""
        self.assertIn("t.duration_ms <= 0 OR", self.repo._fullPlaysClause([]))

    def test_aliases_follow_the_caller(self):
        """Two spellings of the plays table are in use (`p` and the unaliased
        `plays`), and both share this one definition."""
        clause = self.repo._fullPlaysClause([], playsAlias="plays")

        self.assertIn("plays.time_played", clause)
        self.assertNotIn(" p.time_played", clause)

    def test_keep_skips_lets_every_skip_through(self):
        """What the skip-ranked queries need: applied verbatim this filter drops
        every skip, since a skip is the shortest play there is."""
        clause = self.repo._fullPlaysClause([], keepSkips=True)

        self.assertIn("p.is_skip = 1 OR", clause)
        self.assertNotIn("is_skip", self.repo._fullPlaysClause([]))

    def test_the_join_it_needs_matches_the_alias(self):
        self.assertEqual(self.repo._tracksJoin(), " JOIN tracks t ON t.id = p.track_id")
        self.assertEqual(self.repo._tracksJoin(playsAlias="plays"),
                         " JOIN tracks t ON t.id = plays.track_id")

    def test_the_clause_actually_runs_against_the_schema(self):
        """Cheap end-to-end guard: the fragment has to be valid SQL in the
        context its callers splice it into, not just the right string."""
        self.db.repo.upsertTrack(normalizeTrackForTest(
            {"id": "t1", "name": "Song", "duration": 200000, "artists": [{"id": "a1", "name": "A"}]}))
        self.db.repo.insertPlay(self.db.user, "t1", 1750000000, 200000, is_skip=0)
        self.db.repo.insertPlay(self.db.user, "t1", 1750000100, 1000, is_skip=0)
        self.db.repo.commit()

        params = [self.db.user]
        clause = self.repo._fullPlaysClause(params)
        row = self.repo._conn().execute(
            f"SELECT COUNT(*) AS c FROM plays p{self.repo._tracksJoin()}"
            f" WHERE p.username = ?{clause}", params).fetchone()

        self.assertEqual(row["c"], 1)   #< the 1s play is not a full listen


if __name__ == "__main__":
    unittest.main()
