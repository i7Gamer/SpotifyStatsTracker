"""Pin results and access paths for the expensive dashboard reads."""
from unittest.mock import patch

from conftest import DatabaseTestCase
from config import TREND_REDISCOVERY_MIN_HISTORICAL_PLAYS
from Database.queries._base import SECONDS_PER_DAY


class DashboardQueryPerformanceTestCase(DatabaseTestCase):
    NOW = 2_000_000_000
    OLD_DAYS = 200
    PLAY_MS = 200_000

    def _catalog(self):
        tracks = {
            trackId: {"id": trackId, "name": trackId, "duration": self.PLAY_MS,
                      "artists": [{"id": artistId, "name": name}]}
            for trackId, artistId, name in (
                ("member", "a1", "alpha"), ("canonical", "a1", "alpha"),
                ("other", "a2", "Beta"), ("third", "a3", "beta"),
                ("fourth", "a4", "Zeta"),
            )
        }
        return self._makeDb(tracks, [])

    def _capture(self, repo, callback):
        statements = []
        conn = repo._conn()
        conn.set_trace_callback(statements.append)
        try:
            result = callback()
        finally:
            conn.set_trace_callback(None)
        return result, statements

    def _plan(self, repo, sql):
        return " ".join(row["detail"] for row in
                        repo._conn().execute("EXPLAIN QUERY PLAN " + sql))

    def test_distribution_matches_per_play_reference_for_merges_ranges_and_limits(self):
        db = self._catalog()
        repo = db.repo
        start, end = 100, 200
        for trackId, stamps in (("member", (start - 1, start, end)),
                                ("canonical", (start, end - 1)),
                                ("other", (start,))):
            for stamp in stamps:
                repo.insertPlay(db.user, trackId, stamp, self.PLAY_MS)
        repo.insertPlay(db.user, "third", start, self.PLAY_MS, is_skip=True)
        repo.upsertUser("otheruser", "other@example.com")
        repo.insertPlay("otheruser", "fourth", start, self.PLAY_MS)
        repo.replaceTrackGenres("member", ["rock", "pop"])
        repo.replaceTrackGenres("canonical", ["indie", "rock"], inherited=True)
        repo.replaceTrackGenres("other", ["pop", "rock"])
        repo.replaceTrackGenres("third", ["skip-only"])
        repo.replaceTrackGenres("fourth", ["other-user"])
        repo.commit()
        for merged in (False, True):
            if merged:
                repo._conn().execute("UPDATE tracks SET canonical_id='canonical' WHERE id='member'")
            for inherited in (0, 1):
                for lower, upper in ((None, None), (start, end), (start, None),
                                     (None, end), (end, start)):
                    for limit in (None, 0, 1, -1):
                        with self.subTest(merged=merged, inherited=inherited,
                                          bounds=(lower, upper), limit=limit):
                            params = [inherited, db.user]
                            bounds = repo._dateRangeClause(params, lower, upper, column="p.played_at")
                            limitSql = "" if limit is None else " LIMIT ?"
                            if limit is not None:
                                params.append(limit)
                            reference = [dict(row) for row in repo._conn().execute(
                                f"SELECT g.genre AS genre, COUNT(*) AS plays FROM plays p "
                                f"{repo._genreMembershipJoin()} "
                                f"WHERE (? OR g.inherited=0) AND p.username=? AND p.is_skip=0{bounds} "
                                f"GROUP BY g.genre ORDER BY plays DESC, g.genre ASC{limitSql}", params)]
                            self.assertEqual(repo.getGenrePlayCounts(db.user, inherited, lower, upper, limit),
                                             reference)
        self.assertEqual(repo.getGenrePlayCounts("empty", 1), [])

    def test_distribution_aggregates_before_canonical_and_genre_joins(self):
        db = self._catalog()
        db.repo._conn().execute("UPDATE tracks SET canonical_id='canonical' WHERE id='member'")
        _, statements = self._capture(db.repo, lambda: db.repo.getGenrePlayCounts(db.user, 1))
        sql = next(sql for sql in statements if "AS plays" in sql)
        plan = self._plan(db.repo, sql)
        self.assertIn("WITH played AS", sql)
        self.assertIn("idx_plays_user_time", plan)
        self.assertIn("SUM(p.cnt)", sql)

    def test_coverage_all_time_uses_sequential_user_history_access(self):
        db = self._catalog()
        db.repo.insertPlay(db.user, "member", self.NOW, self.PLAY_MS)
        db.repo.replaceTrackGenres("member", ["rock"])
        db.repo.commit()
        for bounds in ((None, None), (self.NOW, self.NOW + 1)):
            with self.subTest(bounds=bounds):
                result, statements = self._capture(
                    db.repo, lambda: db.repo.getGenreCoverageCounts(db.user, 1, *bounds))
                self.assertEqual(result["total"], 1)
                self.assertEqual(result["song_covered"], 1)
                sql = next(sql for sql in statements if "WITH played AS" in sql)
                self.assertIn("idx_plays_user_time", self._plan(db.repo, sql))

    def test_artist_ids_match_full_ranking_including_all_tiebreakers(self):
        db = self._catalog()
        repo = db.repo
        # a4 wins on time; a1 wins the name tie; a2 wins the id tie with a3.
        for trackId in ("member", "other", "third", "fourth"):
            duration = self.PLAY_MS * (2 if trackId == "fourth" else 1)
            repo.insertPlay(db.user, trackId, self.NOW, duration)
        repo.insertPlay(db.user, "canonical", self.NOW, self.PLAY_MS, is_skip=True)
        repo.upsertUser("otheruser", "other@example.com")
        repo.insertPlay("otheruser", "canonical", self.NOW, self.PLAY_MS)
        repo.commit()
        expected = ["a4", "a1", "a2", "a3"]
        for merged in (False, True):
            if merged:
                repo._conn().execute("UPDATE tracks SET canonical_id='canonical' WHERE id='member'")
            for limit in (None, -1, 0, 1, len(expected)):
                with self.subTest(merged=merged, limit=limit):
                    reference = [row["id"] for row in repo.getArtistAggregates(db.user, limit=limit)]
                    self.assertEqual(repo.getTopArtistIds(db.user, limit), reference)
                    self.assertEqual(reference, expected if limit is None or limit < 0 else expected[:limit])
        self.assertEqual(repo.getTopArtistIds("empty", None), [])

    def test_artist_ids_count_collaborators_and_duplicate_credits_like_top_artists(self):
        db = self._catalog()
        repo = db.repo
        conn = repo._conn()
        conn.execute("INSERT INTO track_artists VALUES ('member', 'a2', 1)")
        conn.execute("INSERT INTO track_artists VALUES ('member', 'a2', 2)")
        repo.insertPlay(db.user, "member", self.NOW, self.PLAY_MS)
        repo.commit()
        self.assertEqual(repo.getTopArtistIds(db.user, None), ["a2", "a1"])
        self.assertEqual(repo.getTopArtistIds(db.user, None),
                         [row["id"] for row in repo.getArtistAggregates(db.user)])

    def test_discover_uses_lightweight_ranking_without_hydrating_top_artists(self):
        db = self._catalog()
        db.repo.insertPlay(db.user, "member", self.NOW, self.PLAY_MS)
        db.repo.replaceTrackGenres("member", ["rock"])
        db.repo.replaceArtistGenres("a1", ["rock"])
        db.repo.commit()
        with patch.object(db, "getTopArtists", side_effect=AssertionError("unused hydration")):
            result, statements = self._capture(
                db.repo, lambda: db.getRecommendedArtists(limit=1, genrePool=1, excludeTopN=1))
        self.assertEqual(result, [])
        ranking = next(sql for sql in statements if "SUM(p.time_played)" in sql)
        self.assertNotIn("COUNT(DISTINCT", ranking)
        self.assertNotIn("JOIN tracks", ranking)
        self.assertNotIn("MIN(", ranking)

    def test_recent_trend_candidates_seek_played_releases_and_keep_sibling_history(self):
        db = self._catalog()
        repo = db.repo
        conn = repo._conn()
        conn.execute("UPDATE tracks SET canonical_id='canonical' WHERE id='member'")
        old = self.NOW - self.OLD_DAYS * SECONDS_PER_DAY
        for offset in range(TREND_REDISCOVERY_MIN_HISTORICAL_PLAYS):
            repo.insertPlay(db.user, "member", old - offset, self.PLAY_MS)
        repo.insertPlay(db.user, "canonical", self.NOW, self.PLAY_MS)
        repo.commit()
        raw, statements = self._capture(repo, lambda: repo.getDashboardTrendsRaw(db.user, self.NOW))
        self.assertEqual(raw["rediscovery"]["track_id"], "canonical")
        self.assertEqual(raw["rediscovery"]["old_count"], TREND_REDISCOVERY_MIN_HISTORICAL_PLAYS)
        rediscoverySql = next(sql for sql in statements if "as max_old_played_at" in sql.lower())
        self.assertIn("idx_plays_user_track (username=? AND track_id=?)",
                      self._plan(repo, rediscoverySql))
        # One historical play prevents a false fresh find, but no rediscovery.
        conn.execute("DELETE FROM plays WHERE played_at < ? AND played_at != ?", (self.NOW, old))
        repo.commit()
        raw, statements = self._capture(repo, lambda: repo.getDashboardTrendsRaw(db.user, self.NOW))
        self.assertIsNone(raw["rediscovery"])
        self.assertIsNone(raw["freshFind"])
        freshSql = next(sql for sql in statements if "as first_played_at" in sql)
        self.assertIn("idx_plays_user_track (username=? AND track_id=?)", self._plan(repo, freshSql))
