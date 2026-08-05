# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The image routes' two-part guard: the session must own the <username>
segment, and the <filename> must be a bare filename.

Both routes hand their filename to send_from_directory against a SHARED media
directory (Database.imgDir_tracks/_artists are class-level - the username
segment authorizes, it does not select a directory), so a filename carrying a
path separator is the thing that must never get through.

Note what the two layers do, because it is easy to write a test that proves
nothing here: a URL like /img/alice/tracks/../../etc/passwd is resolved by
Werkzeug BEFORE routing, so it 404s without the view ever running. That makes
an end-to-end assertion alone a tautology - it would still pass with the
guard deleted. The direct-call tests below are the ones that actually pin
`filename != os.path.basename(filename)`, and the positive controls keep the
404 assertions honest.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase

_TRAVERSAL_URLS = (
    "/img/alice/tracks/../../etc/passwd",
    "/img/alice/tracks/..%2f..%2fetc%2fpasswd",
    "/img/alice/tracks/%2e%2e%2fsecret.txt",
)

# What a traversal attempt looks like once it is INSIDE the view - i.e. what
# the basename guard alone has to refuse.
_TRAVERSAL_FILENAMES = (
    "../../etc/passwd",
    "subdir/cover.jpg",
    "/etc/passwd",
)

# Backslash payloads are deliberately NOT in the lists above: os.path.basename
# is platform-dependent, and on POSIX a backslash is an ordinary filename
# character, so basename("..\\..\\x") returns the whole string unchanged and
# the guard does not fire. That is correct rather than a hole - a backslash
# traverses nothing on Linux - but it means asserting a 404 for these would
# pass on Windows and fail in the Docker/Linux image the app ships as. Run
# them only where the separator actually separates.
_WINDOWS_TRAVERSAL_FILENAMES = (
    "..\\..\\secrets\\data_encryption_key.txt",
    "subdir\\cover.jpg",
)
_WINDOWS_TRAVERSAL_URLS = ("/img/alice/artists/..%5csecret.txt",)

_IMAGE_VIEWS = ("serveTrackImage", "serveArtistImage")


class TestImageRouteTraversal(AppTestCase):
    def setUp(self):
        # Keep the app factory from rewriting the real secrets/flask_secret_key.txt
        patcher = patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
        patcher.start()
        self.addCleanup(patcher.stop)

    def _authedApp(self):
        """An app whose session owns 'alice', so every refusal below is the
        FILENAME being rejected rather than the authorization check."""
        dash = self._makeApp()
        self.enterContext(patch.object(dash, 'is_user_logged_in', return_value=True))
        self.enterContext(patch.object(dash, 'get_username_for_email', return_value='alice'))
        return dash

    def _callView(self, dash, viewName, filename, username="alice"):
        """Invoke the view the way a routed request would, but with a filename
        routing itself would never deliver."""
        with dash.app.test_request_context('/'):
            from flask import session
            session['email'] = 'alice@example.com'
            with patch('routes.media.send_from_directory', return_value="SENT") as send, \
                 patch('routes.media.os.path.exists', return_value=True):
                result = dash.app.view_functions[viewName](username=username, filename=filename)
            return result, send

    def test_the_basename_guard_refuses_any_separator_in_the_filename(self):
        """The guard itself, reached directly: no separator may survive into
        send_from_directory, whichever way it is spelled."""
        dash = self._authedApp()
        filenames = _TRAVERSAL_FILENAMES + (
            _WINDOWS_TRAVERSAL_FILENAMES if os.name == "nt" else ())

        for viewName in _IMAGE_VIEWS:
            for filename in filenames:
                with self.subTest(view=viewName, filename=filename):
                    result, send = self._callView(dash, viewName, filename)

                    self.assertEqual(("", 404), result)
                    send.assert_not_called()

    def test_the_guard_expression_actually_discriminates(self):
        """Non-vacuity check for the platform gating above: every payload the
        test feeds the guard must really be one basename() rewrites here, or
        the 404s would be proving something else."""
        filenames = _TRAVERSAL_FILENAMES + (
            _WINDOWS_TRAVERSAL_FILENAMES if os.name == "nt" else ())

        for filename in filenames:
            with self.subTest(filename=filename):
                self.assertNotEqual(filename, os.path.basename(filename))

    def test_a_bare_filename_is_still_served(self):
        """The positive control the test above needs: with the same session and
        the same call shape, an ordinary filename DOES reach the send."""
        dash = self._authedApp()

        for viewName in _IMAGE_VIEWS:
            with self.subTest(view=viewName):
                result, send = self._callView(dash, viewName, "abc123.jpeg")

                self.assertEqual("SENT", result)
                send.assert_called_once()
                self.assertEqual("abc123.jpeg", send.call_args.args[1])

    def test_another_users_segment_is_refused_even_for_a_valid_filename(self):
        """The other half of the same line: the session owns 'alice', so a
        request naming 'bob' is refused before any file is touched."""
        dash = self._authedApp()

        for viewName in _IMAGE_VIEWS:
            with self.subTest(view=viewName):
                result, send = self._callView(dash, viewName, "abc123.jpeg", username="bob")

                self.assertEqual(("", 404), result)
                send.assert_not_called()

    def test_traversal_urls_never_reach_the_view(self):
        """End-to-end defence in depth: these 404 at the routing layer, before
        the guard is consulted at all. Asserted so a future routing change
        (a <path:filename> converter, say) can't quietly start delivering
        separators to the view - which is when the guard above becomes the
        only thing left."""
        dash = self._authedApp()
        client = dash.app.test_client()
        with client.session_transaction() as sess:
            sess['email'] = 'alice@example.com'

        urls = _TRAVERSAL_URLS + (_WINDOWS_TRAVERSAL_URLS if os.name == "nt" else ())

        with patch('routes.media.send_from_directory', return_value="SENT") as send, \
             patch('routes.media.os.path.exists', return_value=True):
            for url in urls:
                with self.subTest(url=url):
                    response = client.get(url)

                    self.assertEqual(404, response.status_code)
                    send.assert_not_called()

    def test_the_artist_route_never_lazy_fetches_for_a_traversal_filename(self):
        """serveArtistImage's lazy fetch derives an artist id from the
        filename and writes the image to that path, so it must sit behind the
        guard, not beside it."""
        dash = self._authedApp()
        fakeDb = MagicMock()
        dash.user_databases["alice"] = fakeDb

        with dash.app.test_request_context('/'):
            from flask import session
            session['email'] = 'alice@example.com'
            with patch('routes.media.send_from_directory', return_value="SENT"), \
                 patch('routes.media.os.path.exists', return_value=False):
                dash.app.view_functions["serveArtistImage"](
                    username="alice", filename="../../evil.jpeg")

        fakeDb.lazyFetchArtistImage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
