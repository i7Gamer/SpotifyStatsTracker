# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The image executor's HTTP goes through one shared requests.Session.

Module-level requests.get builds and discards a Session - and with it a
connection pool - on every call (requests/api.py: `with sessions.Session()
as session:`), so every cover download and every Web API artist lookup paid
a fresh TCP+TLS handshake, and the comment in _downloadImageTask reasoned
from a pool that did not exist. Database._mediaHttpSession is that pool now:
one Session for the process, reached through MediaFetchMixin._mediaGet (the
seam these tests and the rest of the suite patch), closed and replaced by
shutdownWorkerPools beside the executors it serves.

Last.fm, the listener's Web API backfill and the metadata backfiller keep
their module-level requests.get on purpose - out of scope here.
"""
import os
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

import requests

from conftest import DatabaseTestCase
from Database.database import Database
from Database.media_fetch import MEDIA_FETCH_HTTP_TIMEOUT_SECONDS
from Database.repository import IMAGE_KIND_TRACK, IMAGE_STATUS_OK


def _pngBytes():
    from PIL import Image
    buffer = BytesIO()
    Image.new("RGB", (2, 2), 0).save(buffer, format="PNG")
    return buffer.getvalue()


def _imageResponse(imageBytes):
    response = MagicMock()
    response.headers = {"Content-Length": str(len(imageBytes))}
    response.iter_content = lambda chunk_size=None: iter([imageBytes])
    response.__enter__.return_value = response
    return response


class TestMediaHttpSession(DatabaseTestCase):
    def test_the_session_is_one_object_for_the_whole_process(self):
        first = self._makeDb({}, [])
        second = self._makeDb({}, [])
        self.assertIsInstance(Database._mediaHttpSession, requests.Session)
        self.assertIs(first._mediaHttpSession, second._mediaHttpSession)

    def test_image_downloads_use_the_shared_session_not_module_requests_get(self):
        db = self._makeDb({}, [])
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(Database, "_mediaHttpSession") as session, \
             patch("Database.database.requests.get") as moduleGet:
            session.get.return_value = _imageResponse(_pngBytes())

            db._downloadImageTask(Path(tmpdir), "https://i.scdn.co/image/abc", "img1", IMAGE_KIND_TRACK)

            session.get.assert_called_once_with("https://i.scdn.co/image/abc",
                                                timeout=MEDIA_FETCH_HTTP_TIMEOUT_SECONDS, stream=True)
            moduleGet.assert_not_called()
            self.assertEqual(db.repo.imageStatus("img1", IMAGE_KIND_TRACK), IMAGE_STATUS_OK)

    def test_the_web_api_artist_lookup_uses_the_shared_session(self):
        db = self._makeDb({}, [])
        db.getUserSpotifyCredentials = MagicMock(return_value={
            "client_id": "cid", "client_secret": "csecret", "refresh_token": "rtoken"})
        apiResponse = MagicMock()
        apiResponse.status_code = 200
        apiResponse.json.return_value = {"images": [{"url": "https://i.scdn.co/image/abc"}]}

        with patch("Database.Listeners.spotifyListener._refresh_spotify_access_token",
                   return_value="mock_token"), \
             patch.object(Database, "_mediaHttpSession") as session, \
             patch("Database.database.requests.get") as moduleGet, \
             patch("Database.Spotify.Spotify") as cookieClient:
            session.get.return_value = apiResponse

            self.assertEqual(db._fetchArtistImageUrl("artist123"), "https://i.scdn.co/image/abc")

        session.get.assert_called_once()
        self.assertEqual(session.get.call_args.args[0], "https://api.spotify.com/v1/artists/artist123")
        self.assertEqual(session.get.call_args.kwargs["headers"], {"Authorization": "Bearer mock_token"})
        moduleGet.assert_not_called()
        cookieClient.assert_not_called()

    def test_shutdown_closes_the_session_and_leaves_a_fresh_one_in_place(self):
        """Replaced rather than left closed, for the reason the pools are: one
        process outlives many app instances (every route test shuts one
        down), and a download after that must not find a dead session."""
        original = Database._mediaHttpSession
        retired = MagicMock()
        Database._mediaHttpSession = retired
        try:
            Database.shutdownWorkerPools()

            retired.close.assert_called_once()
            self.assertIsNot(Database._mediaHttpSession, retired)
            self.assertIsInstance(Database._mediaHttpSession, requests.Session)
        finally:
            Database._mediaHttpSession = original


if __name__ == "__main__":
    unittest.main()
