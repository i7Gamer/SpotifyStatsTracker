"""The /compare/blend download: both users' shared songs as a playlist file.

The list is the Top Common Songs list, uncapped: the same shared pools
(getTopSongs at COMPARE_SHARED_POOL_SIZE) ranked by the same
_buildSharedItems score, so the file can never disagree with the page. The
formats and the attachment shape are the Playlists page's own exporters."""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase


def _song(trackId, name, plays, artist="Talking Heads", album="77", isrc=""):
    return {
        "id": trackId, "name": name, "plays": plays, "totalTimeListened": plays * 1000,
        "duration": 200000, "artists": [{"name": artist}], "album": {"name": album},
        "isrc": isrc, "url": f"https://open.spotify.com/track/{trackId}",
    }


class BlendExportTestCase(AppTestCase):
    def setUp(self):
        self.dash = self._makeApp()
        for username in ("alice", "bob", "carol"):
            self.dash.repo.upsertUser(username, f"{username}@example.com")
            self.dash.repo.setUserCookies(username, {"sp_dc": "test"})
        self.dbs = {}
        for username in ("alice", "bob", "carol"):
            db = MagicMock()
            db.tz = None
            db.getTopSongs.return_value = []
            self.dbs[username] = db
        # The counterpart's db comes from _getReadOnlyUserDb, not get_user_db
        # (2026-09-04 review, C1 - see routes/compare.py's compareBlendExport);
        # AppTestCase._loginAs only stubs get_user_db, so without this an
        # unmocked _getReadOnlyUserDb would construct a REAL Database for
        # the counterpart instead of handing back self.dbs[...].
        patch.object(self.dash, '_getReadOnlyUserDb', side_effect=lambda u: self.dbs[u]).start()
        self.addCleanup(patch.stopall)

    def _accept(self, requester, recipient):
        self.dash.repo.createShareRequest(requester, recipient)
        shareId = self.dash.repo.getPendingIncomingShares(recipient)[0]["id"]
        self.dash.repo.respondToShareRequest(shareId, recipient, accept=True)


    def _seedOverlap(self):
        """alice and bob share S1 and S2; S3 is alice-only. The shared rank
        puts S2 first: equal rank-discount sums, and S2's combined plays win
        the tiebreak (25 vs 11) - the page's own ordering rule."""
        self.dbs["alice"].getTopSongs.return_value = [
            _song("S1" * 11, "Psycho Killer", 10, isrc="USRC12345678"),
            _song("S2" * 11, "Once in a Lifetime", 5, album="Remain in Light"),
            _song("S3" * 11, "Heaven", 2),
        ]
        self.dbs["bob"].getTopSongs.return_value = [
            _song("S2" * 11, "Once in a Lifetime", 20, album="Remain in Light"),
            _song("S1" * 11, "Psycho Killer", 1, isrc="USRC12345678"),
        ]


class TestBlendExportGuards(BlendExportTestCase):
    def test_404_when_data_sharing_is_disabled(self):
        self._accept("alice", "bob")
        self.dash.repo.setDataSharingEnabled(False)
        client = self._loginAs("alice")

        self.assertEqual(client.get("/compare/blend?with=bob").status_code, 404)

    def test_anonymous_is_sent_to_login(self):
        resp = self.dash.app.test_client().get("/compare/blend")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_404_with_no_accepted_share(self):
        client = self._loginAs("alice")

        self.assertEqual(client.get("/compare/blend").status_code, 404)

    def test_an_unaccepted_counterpart_falls_back_like_the_page(self):
        """?with= is untrusted; same rule as comparePage - never someone the
        session user has no mutual share with, and no 404 oracle either."""
        self._accept("alice", "bob")
        self._seedOverlap()
        client = self._loginAs("alice")

        resp = client.get("/compare/blend?with=carol&interval=")

        self.assertEqual(resp.status_code, 200)
        #< the filename names who the blend is actually with
        self.assertIn("blend_alice_bob.csv", resp.headers["Content-Disposition"])


class TestBlendExportContent(BlendExportTestCase):
    def test_csv_carries_the_shared_songs_in_page_order(self):
        self._accept("alice", "bob")
        self._seedOverlap()
        client = self._loginAs("alice")

        resp = client.get("/compare/blend?with=bob&interval=")
        body = resp.data.decode()

        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.mimetype)
        #< resp.mimetype strips parameters, so it can never see Werkzeug
        #  doubling "; charset=utf-8" for a text/* Response(mimetype=...) -
        #  only the exact header catches that (R5, 2026-09-06 review)
        self.assertEqual(resp.headers["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn('attachment; filename="blend_alice_bob.csv"',
                      resp.headers["Content-Disposition"])
        lines = [l for l in body.splitlines() if l]
        self.assertEqual(lines[0].split(",")[0], "Spotify URI")
        #< S2 first (combined 25 plays beats 11), S3 nowhere (alice-only)
        self.assertIn("Once in a Lifetime", lines[1])
        self.assertIn("Psycho Killer", lines[2])
        self.assertEqual(len(lines), 3)
        #< the catalog fields made it through: artist, album, ISRC
        self.assertIn("Talking Heads", lines[1])
        self.assertIn("Remain in Light", lines[1])
        self.assertIn("USRC12345678", lines[2])

    def test_m3u_and_xspf_come_out_in_their_own_formats(self):
        self._accept("alice", "bob")
        self._seedOverlap()
        client = self._loginAs("alice")

        m3u = client.get("/compare/blend?with=bob&interval=&format=m3u")
        self.assertTrue(m3u.data.decode().startswith("#EXTM3U"))
        self.assertIn("spotify:track:" + "S2" * 11, m3u.data.decode())
        self.assertIn(".m3u", m3u.headers["Content-Disposition"])
        #< audio/x-mpegurl never gets Werkzeug's text/* charset treatment
        self.assertEqual(m3u.headers["Content-Type"], "audio/x-mpegurl")

        xspf = client.get("/compare/blend?with=bob&interval=&format=xspf")
        self.assertIn("<playlist", xspf.data.decode())
        self.assertIn("Blend", xspf.data.decode())
        self.assertEqual(xspf.headers["Content-Type"], "application/xspf+xml; charset=utf-8")

    def test_an_unknown_format_degrades_to_csv(self):
        self._accept("alice", "bob")
        self._seedOverlap()
        client = self._loginAs("alice")

        resp = client.get("/compare/blend?with=bob&interval=&format=exe")

        self.assertIn(".csv", resp.headers["Content-Disposition"])

    def test_the_shared_pool_is_queried_at_pool_depth_not_display_depth(self):
        """The blend is the FULL overlap - the deeper COMPARE_SHARED_POOL_SIZE
        pool - not the first page of the Top Common card."""
        from config import COMPARE_SHARED_POOL_SIZE
        self._accept("alice", "bob")
        self._seedOverlap()
        client = self._loginAs("alice")

        client.get("/compare/blend?with=bob&interval=")

        for username in ("alice", "bob"):
            _, kwargs = self.dbs[username].getTopSongs.call_args
            self.assertEqual(kwargs.get("limit"), COMPARE_SHARED_POOL_SIZE)


if __name__ == "__main__":
    unittest.main()
