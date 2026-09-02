# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An images row marked 'ok' whose file is gone must heal on the first view.

The images table says 'ok' once a download landed on disk, and both fetch
gates trust it: lazyFetchArtistImage returns before any reclaim, and
tryClaimImageDownload's ON CONFLICT refuses to re-claim an 'ok' row. Neither
looks at the disk. So a database restored without Database/Data/Media (a new
machine, a re-created volume, a cleared cache) lost every cover and avatar it
had ever fetched - for good, with no in-app recovery.

The heal lives in the image routes, where the missing file is already
detected: an absent file behind an 'ok' row forgets that row (a status-guarded
DELETE, the releaseImageClaim shape), so the artist path claims and fetches on
this very request and the track path re-claims on the listener's next
saveTrackImg. 'pending' rows are deliberately NOT touched here - one may be
mid-write, and deleteStalePendingImages owns those at boot.

Real Databases and a real (temporary) media directory, with only the image
executor replaced by a recording stub - a MagicMock Database would answer the
route with whatever it returns and pin nothing about the row.
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
from Database.repository import (
    Repository, IMAGE_KIND_ARTIST, IMAGE_KIND_TRACK,
    IMAGE_STATUS_OK, IMAGE_STATUS_FAILED, IMAGE_STATUS_PENDING,
)

#< real-shaped ids (22 base62 characters), seeded into the catalog below: the
#  artist lazy fetch is gated on both, so a short fixture id would 404 for the
#  wrong reason and the heal would never be reached
_ARTIST_ID = "0OdUWJ0sBjDrqHygGUXeCF"
_ALBUM_ID = "6dVIqQ8qmQ5GBnJ9shOYGE"
_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class TestForgetImageStatus(unittest.TestCase):
    """The repository half: only an 'ok' row is forgotten, in one guarded
    statement, so a claim landing between a read and a write cannot be lost."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)

    def test_an_ok_row_is_forgotten_and_claimable_again(self):
        self.repo.markImageStatus("img1", IMAGE_KIND_TRACK, IMAGE_STATUS_OK)

        self.repo.forgetImageStatus("img1", IMAGE_KIND_TRACK)

        self.assertIsNone(self.repo.imageStatus("img1", IMAGE_KIND_TRACK))
        self.assertTrue(self.repo.tryClaimImageDownload("img1", IMAGE_KIND_TRACK))

    def test_pending_and_failed_rows_are_untouched(self):
        self.repo.tryClaimImageDownload("inflight", IMAGE_KIND_ARTIST)
        self.repo.markImageStatus("noimage", IMAGE_KIND_ARTIST, IMAGE_STATUS_FAILED)

        self.repo.forgetImageStatus("inflight", IMAGE_KIND_ARTIST)
        self.repo.forgetImageStatus("noimage", IMAGE_KIND_ARTIST)

        self.assertEqual(self.repo.imageStatus("inflight", IMAGE_KIND_ARTIST), IMAGE_STATUS_PENDING)
        self.assertEqual(self.repo.imageStatus("noimage", IMAGE_KIND_ARTIST), IMAGE_STATUS_FAILED)

    def test_the_other_kind_under_the_same_id_is_untouched(self):
        self.repo.markImageStatus("shared", IMAGE_KIND_TRACK, IMAGE_STATUS_OK)
        self.repo.markImageStatus("shared", IMAGE_KIND_ARTIST, IMAGE_STATUS_OK)

        self.repo.forgetImageStatus("shared", IMAGE_KIND_TRACK)

        self.assertIsNone(self.repo.imageStatus("shared", IMAGE_KIND_TRACK))
        self.assertEqual(self.repo.imageStatus("shared", IMAGE_KIND_ARTIST), IMAGE_STATUS_OK)

    def test_a_missing_row_is_a_noop(self):
        self.repo.forgetImageStatus("never", IMAGE_KIND_TRACK)
        self.assertIsNone(self.repo.imageStatus("never", IMAGE_KIND_TRACK))


class TestMissingImageHealsOnView(AppTestCase):
    def setUp(self):
        patcher = patch("app.SpotifyDashboardApp._get_or_create_secret_key",
                        return_value="test-secret-key")
        patcher.start()
        self.addCleanup(patcher.stop)

        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        root = Path(self._tmpdir.name)
        self.tracksDir = root / "tracks"
        self.artistsDir = root / "artists"
        self.tracksDir.mkdir()
        self.artistsDir.mkdir()
        self.enterContext(patch.object(Database, "imgDir_tracks", self.tracksDir))
        self.enterContext(patch.object(Database, "imgDir_artists", self.artistsDir))

        tracks = {"t1": {"id": "t1", "name": "Song", "imageId": _ALBUM_ID,
                         "artists": [{"id": _ARTIST_ID, "name": "Artist"}]}}
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

    def _get(self, url):
        response = self.client.get(url)
        response.close()   #< release send_file's handle so the temp dir can be removed on Windows
        return response

    def test_an_artist_ok_row_without_its_file_is_refetched_on_view(self):
        self.db.repo.markImageStatus(_ARTIST_ID, IMAGE_KIND_ARTIST, IMAGE_STATUS_OK)

        response = self._get(f"/img/alice/artists/{_ARTIST_ID}.jpeg")

        self.assertEqual(response.status_code, 404)   #< nothing on disk yet; the fetch is in the background
        self.db._imageDownloadExecutor.submit.assert_called_once()
        submittedTask, submittedId = self.db._imageDownloadExecutor.submit.call_args[0][:2]
        self.assertEqual(submittedTask, self.db._lazyFetchArtistImageTask)
        self.assertEqual(submittedId, _ARTIST_ID)
        #< the row is the new claim, not the stale verdict
        self.assertEqual(self.db.repo.imageStatus(_ARTIST_ID, IMAGE_KIND_ARTIST), IMAGE_STATUS_PENDING)

    def test_a_track_ok_row_without_its_file_is_forgotten_on_view(self):
        """No lazy path for covers: the route 404s once, and the listener's
        next saveTrackImg re-claims because the row is gone."""
        self.db.repo.markImageStatus(_ALBUM_ID, IMAGE_KIND_TRACK, IMAGE_STATUS_OK)

        response = self._get(f"/img/alice/tracks/{_ALBUM_ID}.jpeg")

        self.assertEqual(response.status_code, 404)
        self.assertIsNone(self.db.repo.imageStatus(_ALBUM_ID, IMAGE_KIND_TRACK))
        self.assertTrue(self.db.repo.tryClaimImageDownload(_ALBUM_ID, IMAGE_KIND_TRACK))

    def test_an_ok_row_with_its_file_present_is_untouched(self):
        (self.artistsDir / f"{_ARTIST_ID}.jpeg").write_bytes(_PNG)
        (self.tracksDir / f"{_ALBUM_ID}.jpeg").write_bytes(_PNG)
        self.db.repo.markImageStatus(_ARTIST_ID, IMAGE_KIND_ARTIST, IMAGE_STATUS_OK)
        self.db.repo.markImageStatus(_ALBUM_ID, IMAGE_KIND_TRACK, IMAGE_STATUS_OK)

        self.assertEqual(self._get(f"/img/alice/artists/{_ARTIST_ID}.jpeg").status_code, 200)
        self.assertEqual(self._get(f"/img/alice/tracks/{_ALBUM_ID}.jpeg").status_code, 200)

        self.assertEqual(self.db.repo.imageStatus(_ARTIST_ID, IMAGE_KIND_ARTIST), IMAGE_STATUS_OK)
        self.assertEqual(self.db.repo.imageStatus(_ALBUM_ID, IMAGE_KIND_TRACK), IMAGE_STATUS_OK)
        self.db._imageDownloadExecutor.submit.assert_not_called()

    def test_a_pending_row_without_its_file_is_left_to_its_claimer(self):
        """A pending row may be a download mid-write - forgetting it would let
        a second claim race the first onto the same file."""
        self.db.repo.tryClaimImageDownload(_ARTIST_ID, IMAGE_KIND_ARTIST)
        self.db.repo.tryClaimImageDownload(_ALBUM_ID, IMAGE_KIND_TRACK)

        self.assertEqual(self._get(f"/img/alice/artists/{_ARTIST_ID}.jpeg").status_code, 404)
        self.assertEqual(self._get(f"/img/alice/tracks/{_ALBUM_ID}.jpeg").status_code, 404)

        self.assertEqual(self.db.repo.imageStatus(_ARTIST_ID, IMAGE_KIND_ARTIST), IMAGE_STATUS_PENDING)
        self.assertEqual(self.db.repo.imageStatus(_ALBUM_ID, IMAGE_KIND_TRACK), IMAGE_STATUS_PENDING)
        self.db._imageDownloadExecutor.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
