import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import threading
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Other test modules replace Database.database with a MagicMock at import time and
# only restore it in their tearDownModule (which runs after their tests execute, not
# during collection). Since unittest discover imports every test file before running
# any tests, we might see that mock here first - force a real import regardless.
if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from conftest import DatabaseTestCase
from Database.database import Database
from Database.repository import IMAGE_KIND_TRACK, IMAGE_STATUS_OK, IMAGE_STATUS_FAILED


def _bareDatabase():
    """A Database instance with only the state lazyFetchArtistImage needs, skipping
    the heavy __init__ (autoimporter/listener setup) that isn't relevant here."""
    from Database.repository import Repository
    db = Database.__new__(Database)
    db._imageIdsLock = threading.RLock()
    temp_dir = tempfile.mkdtemp()
    db.repo = Repository(Path(temp_dir) / "test.db")
    return db


class TestLazyFetchArtistImage(unittest.TestCase):
    def test_returns_true_without_network_call_if_file_already_exists(self):
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist123.jpeg"
            imagePath.write_bytes(b"already-here")

            with patch("Database.database.requests.get") as mock_get:
                result = db.lazyFetchArtistImage("artist123", imagePath)

            self.assertTrue(result)
            mock_get.assert_not_called()

    def test_returns_false_when_artist_id_missing(self):
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "0.jpeg"
            with patch("Database.database.requests.get") as mock_get:
                result = db.lazyFetchArtistImage("", imagePath)

            self.assertFalse(result)
            mock_get.assert_not_called()

    def test_fetches_and_saves_image_on_first_call(self):
        """The actual fetch runs on the shared background executor, not
        inline - lazyFetchArtistImage() returns the submitted Future rather
        than the outcome directly, so the test waits on it explicitly."""
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist123.jpeg"

            pageResponse = MagicMock()
            pageResponse.text = '<meta property="og:image" content="https://example.com/pic.jpg">'
            imageResponse = MagicMock()
            imageResponse.content = b"fake-image-bytes"

            with patch("Database.database.requests.get", side_effect=[pageResponse, imageResponse]) as mock_get:
                future = db.lazyFetchArtistImage("artist123", imagePath)
                result = future.result(timeout=5)

            self.assertTrue(result)
            self.assertEqual(imagePath.read_bytes(), b"fake-image-bytes")
            self.assertEqual(mock_get.call_count, 2)

    def test_does_not_retry_after_a_failed_attempt_for_same_artist(self):
        """Negative caching: once we've tried (and failed to find an og:image) for an
        artist id, subsequent lookups for that id must not re-hit Spotify."""
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "missingArtist.jpeg"

            noImageResponse = MagicMock()
            noImageResponse.text = "<html>no og:image here</html>"

            with patch("Database.database.requests.get", return_value=noImageResponse) as mock_get:
                firstFuture = db.lazyFetchArtistImage("missingArtist", imagePath)
                firstResult = firstFuture.result(timeout=5)
                secondResult = db.lazyFetchArtistImage("missingArtist", imagePath)

            self.assertFalse(firstResult)
            self.assertFalse(secondResult)   #< dedup path returns a plain bool, no new Future/fetch
            mock_get.assert_called_once()

    def test_network_exception_is_swallowed_and_returns_false(self):
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist999.jpeg"
            with patch("Database.database.requests.get", side_effect=Exception("boom")):
                future = db.lazyFetchArtistImage("artist999", imagePath)
                result = future.result(timeout=5)

            self.assertFalse(result)

    def test_dispatch_does_not_block_the_calling_thread(self):
        """The whole point of routing this through the shared executor: an
        HTTP request thread calling this must get control back immediately
        instead of blocking on up to two sequential network calls."""
        import time
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artistSlow.jpeg"

            def slowGet(*args, **kwargs):
                time.sleep(0.3)
                response = MagicMock()
                response.text = "<html>no og:image here</html>"
                return response

            with patch("Database.database.requests.get", side_effect=slowGet):
                start = time.monotonic()
                future = db.lazyFetchArtistImage("artistSlow", imagePath)
                elapsed = time.monotonic() - start

                self.assertLess(elapsed, 0.1)
                future.result(timeout=5)   #< let the background task finish before tmpdir cleanup


class TestDownloadImageTaskExtension(DatabaseTestCase):
    """The templates hardcode `<imgId>.jpeg`, so downloaded covers must always be
    saved as .jpeg regardless of the format the CDN returns - a PNG saved as
    `<imgId>.png` would 404 forever."""

    def _makeResponse(self, imageBytes):
        response = MagicMock()
        response.content = imageBytes
        return response

    def _pngBytes(self, mode="RGBA"):
        from io import BytesIO
        from PIL import Image
        buffer = BytesIO()
        Image.new(mode, (2, 2), (255, 0, 0, 128) if mode == "RGBA" else 0).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_png_response_is_saved_as_jpeg(self):
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            imgDir = Path(tmpdir)

            with patch("Database.database.requests.get", return_value=self._makeResponse(self._pngBytes())):
                db._downloadImageTask(imgDir, "https://img.example/x", "img1", IMAGE_KIND_TRACK)

            self.assertTrue((imgDir / "img1.jpeg").exists())
            self.assertFalse((imgDir / "img1.png").exists())

            from PIL import Image
            with Image.open(imgDir / "img1.jpeg") as saved:
                self.assertEqual(saved.format, "JPEG")

    def test_download_marks_image_ok_in_the_shared_catalog(self):
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            imgDir = Path(tmpdir)

            with patch("Database.database.requests.get", return_value=self._makeResponse(self._pngBytes())):
                db._downloadImageTask(imgDir, "https://img.example/x", "img1", IMAGE_KIND_TRACK)

            self.assertEqual(db.repo.imageStatus("img1", IMAGE_KIND_TRACK), IMAGE_STATUS_OK)


class TestSaveImgEmptyUrlGuard(DatabaseTestCase):
    """_saveImg() must silently skip when url is empty/None (MissingSchema fix)."""

    def _makeDbWithFakeExecutor(self, tmpdir):
        db = self._makeDb({}, [])
        db.imgDir_tracks = Path(tmpdir) / "tracks"
        db._imageDownloadExecutor = MagicMock()
        return db

    def test_empty_url_does_not_call_executor(self):
        """An empty imageUrl must never reach the thread pool / requests.get."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._makeDbWithFakeExecutor(tmpdir)
            db._saveImg(db.imgDir_tracks, "", "some-img-id", kind=IMAGE_KIND_TRACK)
            db._imageDownloadExecutor.submit.assert_not_called()

    def test_none_url_does_not_call_executor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._makeDbWithFakeExecutor(tmpdir)
            db._saveImg(db.imgDir_tracks, None, "some-img-id", kind=IMAGE_KIND_TRACK)
            db._imageDownloadExecutor.submit.assert_not_called()

    def test_empty_url_does_not_poison_the_claim(self):
        """imgId must NOT be claimed for an empty URL - a retry should be possible
        if the URL is later populated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._makeDbWithFakeExecutor(tmpdir)
            db._saveImg(db.imgDir_tracks, "", "poison-id", kind=IMAGE_KIND_TRACK)
            self.assertIsNone(db.repo.imageStatus("poison-id", IMAGE_KIND_TRACK))

    def test_valid_url_still_reaches_executor(self):
        """Sanity check: a proper URL must still be submitted to the executor."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._makeDbWithFakeExecutor(tmpdir)
            db._saveImg(db.imgDir_tracks, "https://example.com/cover.jpg", "valid-id", kind=IMAGE_KIND_TRACK)
            db._imageDownloadExecutor.submit.assert_called_once()

    def test_already_claimed_image_does_not_reach_executor(self):
        """The second saveImg for the same id (e.g. two users' plays of the same
        song) must not re-download - the claim is shared, not per user."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._makeDbWithFakeExecutor(tmpdir)
            db._saveImg(db.imgDir_tracks, "https://example.com/cover.jpg", "shared-id", kind=IMAGE_KIND_TRACK)
            db._imageDownloadExecutor.submit.assert_called_once()

            db._saveImg(db.imgDir_tracks, "https://example.com/cover.jpg", "shared-id", kind=IMAGE_KIND_TRACK)
            db._imageDownloadExecutor.submit.assert_called_once()  #< still just the one call


class TestDownloadImageTaskErrorLog(DatabaseTestCase):
    """_downloadImageTask() must include imgId in error log lines and mark the
    image as failed in the shared catalog (not left permanently 'pending')."""

    def _pngBytes(self):
        from io import BytesIO
        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", (1, 1)).save(buf, format="PNG")
        return buf.getvalue()

    def test_request_error_log_includes_imgid(self):
        import requests as req
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            imgDir = Path(tmpdir)
            with patch("Database.database.requests.get",
                       side_effect=req.exceptions.ConnectionError("timeout")), \
                 self.assertLogs("Database.database", level="ERROR") as logs:
                db._downloadImageTask(imgDir, "https://img.example/x", "track-abc", IMAGE_KIND_TRACK)

        self.assertIn("track-abc", " ".join(logs.output))
        self.assertEqual(db.repo.imageStatus("track-abc", IMAGE_KIND_TRACK), IMAGE_STATUS_FAILED)

    def test_save_error_log_includes_imgid(self):
        """If saving the image raises (e.g. corrupt bytes), the imgId appears in the log."""
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir:
            imgDir = Path(tmpdir)
            bad_response = MagicMock()
            bad_response.content = b"not-an-image"
            with patch("Database.database.requests.get", return_value=bad_response), \
                 self.assertLogs("Database.database", level="ERROR") as logs:
                db._downloadImageTask(imgDir, "https://img.example/x", "track-xyz", IMAGE_KIND_TRACK)

        self.assertIn("track-xyz", " ".join(logs.output))
        self.assertEqual(db.repo.imageStatus("track-xyz", IMAGE_KIND_TRACK), IMAGE_STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
