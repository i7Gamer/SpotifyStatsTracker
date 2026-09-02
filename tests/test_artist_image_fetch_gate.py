# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The artist image route only fetches for artists the catalog knows.

/img/<me>/artists/<anything-alnum>.jpeg used to hand whatever it was given
to lazyFetchArtistImage, which claimed any never-seen id and submitted a task
that makes a Web API GET and, on anything but 200, a pathfinder lookup on the
deliberately unlimited path (Database/rate_limit.py). So any signed-in client
could fan out two outbound Spotify requests per distinct junk id from the
instance's IP and litter the images table with a 'pending' or 'failed' row
each. Templates only ever reference catalog artists, so gating the fetch on
the id's shape (22 base62 characters) and then on the artists table changes
nothing legitimate - and the shape check comes first so a malformed id never
costs the database read either.

Real Databases, with only the image executor replaced by a recording stub:
the assertion is "nothing was submitted and no row was written", which a
MagicMock Database could not make.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from _app_factory import AppTestCase
from conftest import makeDatabaseWithData
from Database.database import Database
from Database.repository import Repository, IMAGE_KIND_ARTIST, IMAGE_STATUS_PENDING

_CATALOG_ARTIST_ID = "0OdUWJ0sBjDrqHygGUXeCF"
_UNKNOWN_ARTIST_ID = "4Z8W4fKeB5YxbusRsdQVPb"   #< real-shaped, never seeded
_JUNK_IDS = ("zzzz1", "0OdUWJ0sBjDrqHygGUXeC", "0OdUWJ0sBjDrqHygGUXeCF1")   #< short, one short, one long


class TestArtistExists(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)

    def test_a_seeded_artist_exists_and_an_unknown_one_does_not(self):
        from conftest import normalizeTrackForTest
        self.repo.upsertTrack(normalizeTrackForTest(
            {"id": "t1", "name": "Song", "artists": [{"id": _CATALOG_ARTIST_ID, "name": "Artist"}]}))
        self.repo.commit()

        self.assertTrue(self.repo.artistExists(_CATALOG_ARTIST_ID))
        self.assertFalse(self.repo.artistExists(_UNKNOWN_ARTIST_ID))


class TestArtistImageFetchGate(AppTestCase):
    def setUp(self):
        patcher = patch("app.SpotifyDashboardApp._get_or_create_secret_key",
                        return_value="test-secret-key")
        patcher.start()
        self.addCleanup(patcher.stop)

        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        root = Path(self._tmpdir.name)
        self.artistsDir = root / "artists"
        self.artistsDir.mkdir()
        self.enterContext(patch.object(Database, "imgDir_artists", self.artistsDir))

        tracks = {"t1": {"id": "t1", "name": "Song",
                         "artists": [{"id": _CATALOG_ARTIST_ID, "name": "Artist"}]}}
        self.db = makeDatabaseWithData(root / "test.db", tracks, [], username="alice")
        self.addCleanup(self.db.repo.connectionManager.close)
        self.db._imageDownloadExecutor = MagicMock()

        self.dash = self._makeApp()
        self.dash.user_databases["alice"] = self.db
        self.enterContext(patch.object(self.dash, "is_user_logged_in", return_value=True))
        self.enterContext(patch.object(self.dash, "get_username_for_email", return_value="alice"))
        self.client = self.dash.app.test_client()
        with self.client.session_transaction() as sess:
            sess["email"] = "alice@example.com"

    def _get(self, artistId):
        response = self.client.get(f"/img/alice/artists/{artistId}.jpeg")
        response.close()
        return response

    def test_a_catalog_artist_with_no_file_is_fetched(self):
        """The control: the gate must not cost a real artist their picture."""
        self.assertEqual(self._get(_CATALOG_ARTIST_ID).status_code, 404)

        self.db._imageDownloadExecutor.submit.assert_called_once()
        self.assertEqual(self.db._imageDownloadExecutor.submit.call_args[0][1], _CATALOG_ARTIST_ID)
        self.assertEqual(self.db.repo.imageStatus(_CATALOG_ARTIST_ID, IMAGE_KIND_ARTIST), IMAGE_STATUS_PENDING)

    def test_a_real_shaped_id_outside_the_catalog_is_not_fetched(self):
        self.assertEqual(self._get(_UNKNOWN_ARTIST_ID).status_code, 404)

        self.db._imageDownloadExecutor.submit.assert_not_called()
        self.assertIsNone(self.db.repo.imageStatus(_UNKNOWN_ARTIST_ID, IMAGE_KIND_ARTIST))

    def test_a_malformed_id_is_not_fetched_and_never_reaches_the_catalog(self):
        with patch.object(type(self.db.repo), "artistExists") as artistExists:
            for junk in _JUNK_IDS:
                with self.subTest(junk=junk):
                    self.assertEqual(self._get(junk).status_code, 404)
                    self.assertIsNone(self.db.repo.imageStatus(junk, IMAGE_KIND_ARTIST))
        self.db._imageDownloadExecutor.submit.assert_not_called()
        artistExists.assert_not_called()   #< the shape check is the cheap one, so it runs first


if __name__ == "__main__":
    unittest.main()
