"""The genre comparison block on /compare: per-side top genres + shared
genres, rendered only when BOTH users pass the unlock gate."""
import json
import unittest
from unittest.mock import patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp, COMPARE_GENRE_POOL_SIZE
from test_charts_genres import coverageDict

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


def _zeroHeatmapGrid():
    return [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]


class CompareGenresTestCase(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _makeStubDb(self, coverage=None, distribution=None):
        db = MagicMock()
        db.tz = None
        db.getPlayTotals.return_value = (0, 0)
        db.getTopSongs.return_value = []
        db.getTopArtists.return_value = []
        db.getTopAlbums.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getSongsCount.return_value = 0
        db.getArtistsCount.return_value = 0
        db.getCompletionStats.return_value = {"skips": 0, "completes": 0, "partials": 0}
        db.getExplicitRatio.return_value = {"explicit": 0, "clean": 0}
        db.getHourOfDayHeatmap.return_value = _zeroHeatmapGrid()
        db.readProgress.return_value = {"status": "idle", "current": 0, "total": 0,
                                        "percentage": 0, "message": "", "error": False}
        if coverage is not None:
            db.getGenreCoverage.return_value = coverage
        if distribution is not None:
            db.getGenreDistribution.return_value = distribution
        return db

    def setUp(self):
        self.dash = self._makeApp()
        for username in ("alice", "bob"):
            self.dash.repo.upsertUser(username, f"{username}@example.com")
            self.dash.repo.setUserCookies(username, {"sp_dc": "test"})
        self.dash.repo.createShareRequest("alice", "bob")
        shareId = self.dash.repo.getPendingIncomingShares("bob")[0]["id"]
        self.dash.repo.respondToShareRequest(shareId, "bob", accept=True)
        self.dbs = {"alice": self._makeStubDb(), "bob": self._makeStubDb()}

    def _loginAs(self, username):
        patch.object(self.dash, 'is_user_logged_in', return_value=True).start()
        patch.object(self.dash, 'get_username_for_email', return_value=username).start()
        patch.object(self.dash, 'get_user_db', side_effect=lambda u, e: self.dbs[u]).start()
        self.addCleanup(patch.stopall)

        client = self.dash.app.test_client()
        with client.session_transaction() as sess:
            sess['email'] = f"{username}@example.com"
            sess['username'] = username
        return client

    def _unlockBoth(self):
        self.dbs["alice"] = self._makeStubDb(coverage=coverageDict(80, 60, 90),
                                             distribution={"rock": 100, "shoegaze": 50, "jazz": 10})
        self.dbs["bob"] = self._makeStubDb(coverage=coverageDict(70, 55, 65),
                                           distribution={"rock": 40, "jazz": 80, "techno": 30})

    def test_unstubbed_dbs_render_the_locked_block(self):
        client = self._loginAs("alice")
        resp = client.get("/compare")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Genre comparison unlocks", resp.data)
        self.dbs["alice"].getGenreDistribution.assert_not_called()
        self.dbs["bob"].getGenreDistribution.assert_not_called()

    def test_locked_when_only_the_viewer_passes(self):
        self.dbs["alice"] = self._makeStubDb(coverage=coverageDict(80, 60, 90),
                                             distribution={"rock": 1})
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b"Genre comparison unlocks", resp.data)
        self.dbs["alice"].getGenreDistribution.assert_not_called()   #< no point querying one side

    def test_unlocked_shows_both_sides_and_the_shared_intersection(self):
        self._unlockBoth()
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"Genre comparison unlocks", resp.data)
        for genre in (b"rock", b"shoegaze", b"jazz", b"techno"):
            self.assertIn(genre, resp.data)
        # Shared = intersection ordered by combined plays: rock 140 > jazz 90.
        body = resp.data.decode()
        self.assertLess(body.find("You both listen to"), body.find("Last.fm"))
        sharedSection = body[body.find("You both listen to"):]
        self.assertLess(sharedSection.find("rock"), sharedSection.find("jazz"))
        self.assertNotIn("techno", sharedSection.split("Last.fm")[0])   #< bob-only genre isn't shared

        _, kwargs = self.dbs["alice"].getGenreDistribution.call_args
        self.assertEqual(kwargs["limit"], COMPARE_GENRE_POOL_SIZE)
        self.dbs["bob"].getGenreDistribution.assert_called_once()

    def test_ajax_payload_carries_the_genres_chunk(self):
        self._unlockBoth()
        client = self._loginAs("alice")

        resp = client.get("/compare?ajax=true")

        payload = json.loads(resp.data)
        self.assertIn("genresHtml", payload)
        self.assertIn("shoegaze", payload["genresHtml"])

    def test_taste_match_includes_genre_overlap_when_gate_passes_both_sides(self):
        """Genres fold into taste match like any other category (see
        test_compare_route.py's taste-match suite for the core arithmetic),
        gated on genresUnlocked instead of raw row presence - a sparse
        partial backfill shouldn't swing the score. Artists/songs/albums
        pools are empty on both stub sides, so genres end up the ONLY
        category with data and the category weight cancels out of the
        normalization: actual=2*w(1)+2*w(2)=3.2619 (rock/jazz exact matches
        at ranks 1/2 on both sides), ideal=sum(2*w(1..3))=4.2619 -> 76.5%
        raw -> round(100*0.765^0.6)=85% displayed."""
        self.dbs["alice"] = self._makeStubDb(
            coverage=coverageDict(80, 60, 90),
            distribution={"rock": 100, "jazz": 50, "indie": 10})
        self.dbs["bob"] = self._makeStubDb(
            coverage=coverageDict(70, 55, 65),
            distribution={"rock": 90, "jazz": 40, "blues": 5})
        client = self._loginAs("alice")

        resp = client.get("/compare?ajax=true")

        self.assertEqual(json.loads(resp.data)["tasteMatch"], 85)

    def test_taste_match_excludes_genre_when_gate_fails_for_either_side(self):
        """Same genre pools as the unlocked test above, but alice's coverage
        no longer passes the gate - genres must stay excluded (not scored as
        0% overlap), and with every other category also empty on both stub
        sides the match is hidden entirely, matching pre-genre behavior."""
        self.dbs["alice"] = self._makeStubDb(
            coverage=coverageDict(20, 20, 20),   #< below the gate
            distribution={"rock": 100, "jazz": 50, "indie": 10})
        self.dbs["bob"] = self._makeStubDb(
            coverage=coverageDict(70, 55, 65),
            distribution={"rock": 90, "jazz": 40, "blues": 5})
        client = self._loginAs("alice")

        resp = client.get("/compare?ajax=true")

        self.assertIsNone(json.loads(resp.data)["tasteMatch"])
        self.dbs["alice"].getGenreDistribution.assert_not_called()
        self.dbs["bob"].getGenreDistribution.assert_not_called()

    def test_disabled_hides_the_block_without_querying_either_side(self):
        self._unlockBoth()
        self.dash.repo.setLastfmGenreBackfillEnabled(False)
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"Genre comparison unlocks", resp.data)
        self.assertNotIn(b"You both listen to", resp.data)
        self.dbs["alice"].getGenreCoverage.assert_not_called()
        self.dbs["bob"].getGenreCoverage.assert_not_called()

    def test_disabled_ajax_genres_chunk_is_empty(self):
        self._unlockBoth()
        self.dash.repo.setLastfmGenreBackfillEnabled(False)
        client = self._loginAs("alice")

        resp = client.get("/compare?ajax=true")

        payload = json.loads(resp.data)
        self.assertNotIn("shoegaze", payload["genresHtml"])
        self.assertNotIn("Genre comparison unlocks", payload["genresHtml"])


if __name__ == "__main__":
    unittest.main()
