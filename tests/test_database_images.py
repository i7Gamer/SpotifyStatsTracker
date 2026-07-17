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
from Database.repository import IMAGE_KIND_ARTIST, IMAGE_KIND_TRACK, IMAGE_STATUS_OK, IMAGE_STATUS_FAILED


def _bareDatabase():
    """A Database instance with only the state lazyFetchArtistImage needs, skipping
    the heavy __init__ (autoimporter/listener setup) that isn't relevant here.
    user/email are set (rather than left unset like other _bareDatabase helpers in
    this test suite) because the SpotipyFree fallback path materializes a per-user
    cookies file, which reads them."""
    from Database.repository import Repository
    db = Database.__new__(Database)
    db._imageIdsLock = threading.RLock()
    temp_dir = tempfile.mkdtemp()
    db.repo = Repository(Path(temp_dir) / "test.db")
    db.user = "testuser"
    db.email = "testuser@example.com"
    return db


def _pngBytes():
    """A tiny real image, since _downloadImageTask feeds CDN response bytes through
    PIL - garbage bytes would fail to decode and the download would be marked failed
    regardless of what the test is trying to exercise."""
    from io import BytesIO
    from PIL import Image
    buffer = BytesIO()
    Image.new("RGB", (2, 2), 0).save(buffer, format="PNG")
    return buffer.getvalue()


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

    def test_fetches_via_web_api_when_credentials_configured(self):
        """The actual fetch runs on the shared background executor, not
        inline - lazyFetchArtistImage() returns the submitted Future rather
        than the outcome directly, so the test waits on it explicitly."""
        db = _bareDatabase()
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "csecret", "refresh_token": "rtoken"})
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist123.jpeg"

            apiResponse = MagicMock()
            apiResponse.status_code = 200
            apiResponse.json.return_value = {"images": [{"url": "https://i.scdn.co/image/abc"}]}
            imageResponse = MagicMock()
            imageResponse.content = _pngBytes()

            with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                       return_value="mock_token"), \
                 patch("Database.database.requests.get", side_effect=[apiResponse, imageResponse]) as mock_get, \
                 patch("SpotipyFree.Spotify") as mock_spotipy_class:
                future = db.lazyFetchArtistImage("artist123", imagePath)
                result = future.result(timeout=5)

            self.assertTrue(result)
            self.assertTrue(imagePath.exists())
            self.assertEqual(mock_get.call_count, 2)   #< GET /v1/artists/{id}, then the CDN image bytes
            mock_spotipy_class.assert_not_called()   #< official API succeeded, no fallback needed
            self.assertEqual(db.repo.imageStatus("artist123", IMAGE_KIND_ARTIST), IMAGE_STATUS_OK)

    def test_falls_back_to_spotipy_free_when_no_credentials_configured(self):
        """Configuring a Spotify API client id/secret is optional - most installs
        won't have one (db.getUserSpotifyCredentials() is naturally None here,
        there's no users row for "testuser" in this fresh temp db), so SpotipyFree
        must still be able to find the image on its own."""
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist123.jpeg"

            mock_sp = MagicMock()
            mock_sp.artist.return_value = {"images": [{"url": "https://i.scdn.co/image/xyz"}]}
            imageResponse = MagicMock()
            imageResponse.content = _pngBytes()

            with patch("SpotipyFree.Spotify", return_value=mock_sp), \
                 patch("Database.database.requests.get", return_value=imageResponse) as mock_get:
                future = db.lazyFetchArtistImage("artist123", imagePath)
                result = future.result(timeout=5)

            self.assertTrue(result)
            self.assertTrue(imagePath.exists())
            mock_sp.artist.assert_called_once_with("artist123")
            mock_get.assert_called_once()   #< just the CDN image bytes; no api.spotify.com call was made
            self.assertEqual(db.repo.imageStatus("artist123", IMAGE_KIND_ARTIST), IMAGE_STATUS_OK)

    def test_falls_back_to_spotipy_free_when_web_api_request_fails(self):
        """Credentials configured but the official API call itself fails (expired
        grant, rate limit, ...) - must not give up, same fallback as no-credentials."""
        db = _bareDatabase()
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "csecret", "refresh_token": "rtoken"})
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist123.jpeg"

            apiResponse = MagicMock()
            apiResponse.status_code = 403
            imageResponse = MagicMock()
            imageResponse.content = _pngBytes()

            mock_sp = MagicMock()
            mock_sp.artist.return_value = {"images": [{"url": "https://i.scdn.co/image/xyz"}]}

            with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                       return_value="mock_token"), \
                 patch("Database.database.requests.get", side_effect=[apiResponse, imageResponse]), \
                 patch("SpotipyFree.Spotify", return_value=mock_sp) as mock_spotipy_class:
                future = db.lazyFetchArtistImage("artist123", imagePath)
                result = future.result(timeout=5)

            self.assertTrue(result)
            mock_spotipy_class.assert_called_once()
            self.assertEqual(db.repo.imageStatus("artist123", IMAGE_KIND_ARTIST), IMAGE_STATUS_OK)

    def test_does_not_fall_back_when_web_api_confirms_no_image(self):
        """A definitive 200 with an empty images list means Spotify itself has no
        picture for this artist - that's real signal, not a transient failure, so
        it must not spend an extra request (and materialize a cookies file) asking
        SpotipyFree the same question again."""
        db = _bareDatabase()
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "csecret", "refresh_token": "rtoken"})
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist123.jpeg"

            apiResponse = MagicMock()
            apiResponse.status_code = 200
            apiResponse.json.return_value = {"images": []}

            with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                       return_value="mock_token"), \
                 patch("Database.database.requests.get", return_value=apiResponse) as mock_get, \
                 patch("SpotipyFree.Spotify") as mock_spotipy_class:
                future = db.lazyFetchArtistImage("artist123", imagePath)
                result = future.result(timeout=5)

            self.assertFalse(result)
            mock_get.assert_called_once()
            mock_spotipy_class.assert_not_called()
            self.assertEqual(db.repo.imageStatus("artist123", IMAGE_KIND_ARTIST), IMAGE_STATUS_FAILED)

    def test_does_not_retry_after_a_failed_attempt_for_same_artist(self):
        """Negative caching: once we've tried (and failed to find any image) for an
        artist id, subsequent lookups for that id must not re-hit Spotify."""
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "missingArtist.jpeg"

            mock_sp = MagicMock()
            mock_sp.artist.return_value = {"images": []}

            with patch("SpotipyFree.Spotify", return_value=mock_sp) as mock_spotipy_class:
                firstFuture = db.lazyFetchArtistImage("missingArtist", imagePath)
                firstResult = firstFuture.result(timeout=5)
                secondResult = db.lazyFetchArtistImage("missingArtist", imagePath)

            self.assertFalse(firstResult)
            self.assertFalse(secondResult)   #< dedup path returns a plain bool, no new Future/fetch
            mock_spotipy_class.assert_called_once()

    def test_network_exception_is_swallowed_and_returns_false(self):
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artist999.jpeg"
            with patch("SpotipyFree.Spotify", side_effect=Exception("boom")):
                future = db.lazyFetchArtistImage("artist999", imagePath)
                result = future.result(timeout=5)

            self.assertFalse(result)
            self.assertEqual(db.repo.imageStatus("artist999", IMAGE_KIND_ARTIST), IMAGE_STATUS_FAILED)

    def test_dispatch_does_not_block_the_calling_thread(self):
        """The whole point of routing this through the shared executor: an
        HTTP request thread calling this must get control back immediately
        instead of blocking on the SpotipyFree lookup.

        Proven with an event gate rather than a wall-clock threshold (a
        previous `elapsed < 0.1s` assertion flaked on loaded CI runners
        where thread spin-up alone costs hundreds of ms): the mocked
        fetch can't finish until the test opens the gate, so
        lazyFetchArtistImage returning at all - with the fetch still
        pending - means the calling thread never ran it inline."""
        db = _bareDatabase()
        with tempfile.TemporaryDirectory() as tmpdir:
            imagePath = Path(tmpdir) / "artistSlow.jpeg"

            gate = threading.Event()

            def gatedArtist(*args, **kwargs):
                #< the timeout turns an inline-fetch regression into a test
                #  failure (the gate only opens after dispatch returns, so
                #  running this on the calling thread would otherwise hang)
                gate.wait(timeout=5)
                return {"images": []}

            mock_sp = MagicMock()
            mock_sp.artist.side_effect = gatedArtist

            with patch("SpotipyFree.Spotify", return_value=mock_sp):
                future = db.lazyFetchArtistImage("artistSlow", imagePath)

                self.assertFalse(future.done())   #< fetch is parked on the gate, dispatch already returned
                gate.set()
                #< no image found -> False; also ensures the background task
                #  finishes before tmpdir cleanup
                self.assertFalse(future.result(timeout=5))


class TestDeleteFailedArtistImages(unittest.TestCase):
    """Repository.deleteFailedArtistImages() is the one-time remediation
    migrate1_20_0 runs to un-stick artists caught by the old og:image-scrape
    bug - see that migrator's docstring."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        from Database.repository import Repository
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)

    def test_clears_failed_artist_images_only(self):
        self.repo.markImageStatus("artBroken1", IMAGE_KIND_ARTIST, IMAGE_STATUS_FAILED)
        self.repo.markImageStatus("artBroken2", IMAGE_KIND_ARTIST, IMAGE_STATUS_FAILED)
        self.repo.markImageStatus("artOk", IMAGE_KIND_ARTIST, IMAGE_STATUS_OK)
        self.repo.markImageStatus("trackBroken", IMAGE_KIND_TRACK, IMAGE_STATUS_FAILED)

        cleared = self.repo.deleteFailedArtistImages()

        self.assertEqual(cleared, 2)
        self.assertIsNone(self.repo.imageStatus("artBroken1", IMAGE_KIND_ARTIST))
        self.assertIsNone(self.repo.imageStatus("artBroken2", IMAGE_KIND_ARTIST))
        self.assertEqual(self.repo.imageStatus("artOk", IMAGE_KIND_ARTIST), IMAGE_STATUS_OK)
        #< a failed track image is a real per-URL 404, not a broken fetch method - untouched
        self.assertEqual(self.repo.imageStatus("trackBroken", IMAGE_KIND_TRACK), IMAGE_STATUS_FAILED)

    def test_cleared_artist_is_reclaimable(self):
        self.repo.markImageStatus("artBroken", IMAGE_KIND_ARTIST, IMAGE_STATUS_FAILED)

        self.repo.deleteFailedArtistImages()

        self.assertTrue(self.repo.tryClaimImageDownload("artBroken", IMAGE_KIND_ARTIST))

    def test_no_failed_artist_images_is_a_noop(self):
        self.repo.markImageStatus("artOk", IMAGE_KIND_ARTIST, IMAGE_STATUS_OK)
        self.assertEqual(self.repo.deleteFailedArtistImages(), 0)


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
