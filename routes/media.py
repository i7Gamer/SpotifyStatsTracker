# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Authenticated image-serving routes (/img/<username>/tracks|artists/...).

Extracted verbatim from app.py. Track/artist images are shared across every
user (Database.imgDir_* are class-level, not per user) - the <username> segment
is only the authorization check, not a directory selector.
"""
import os
from pathlib import Path

from flask import make_response, session, send_from_directory

from config import IMAGE_CACHE_CONTROL, IMAGE_CACHE_MAX_AGE_SECONDS
from Database.database import Database


def sendCacheableImage(directory, filename):
    """One image file, cacheable for IMAGE_CACHE_MAX_AGE_SECONDS.

    The image files are write-once (see the constant's comment), so the browser
    can be told to stop asking. Without this send_from_directory sets its own
    "no-cache", which is not the app's no-store - the images did cache - but it
    still costs a conditional request per image per page load, and these are
    authenticated routes: each of those 304s runs the session check and its
    queries, ~30 times over on a top-list page.

    The header is REPLACED rather than added to: Flask's max_age= emits
    "public", and a shared proxy must not serve one viewer's authorized image
    to the next. A missing file raises NotFound before any of this, so a 404
    keeps the app-wide no-store and an image that has not been downloaded yet
    is never negatively cached."""
    response = make_response(send_from_directory(directory, filename,
                                                 max_age=IMAGE_CACHE_MAX_AGE_SECONDS))
    response.headers["Cache-Control"] = IMAGE_CACHE_CONTROL
    return response


def register(app, dashboard):
    def _authorized_image_username():
        """Returns the username the current session is allowed to view images for, or None."""
        email = session.get("email")
        if not email or not dashboard.is_user_logged_in(email):
            return None
        return dashboard.get_username_for_email(email)

    def serveTrackImage(username, filename):
        if username != _authorized_image_username() or filename != os.path.basename(filename):
            return "", 404
        return sendCacheableImage(Database.imgDir_tracks, filename)
    app.add_url_rule('/img/<username>/tracks/<filename>', 'serveTrackImage', serveTrackImage)

    def serveArtistImage(username, filename):
        if username != _authorized_image_username() or filename != os.path.basename(filename):
            return "", 404
        imageDir = Database.imgDir_artists
        imagePath = os.path.join(imageDir, filename)

        if not os.path.exists(imagePath):
            parts = os.path.splitext(filename)
            if len(parts) == 2 and parts[0].isalnum():
                artistId = parts[0]
                db = dashboard.user_databases.get(username)
                if db:
                    db.lazyFetchArtistImage(artistId, Path(imagePath))

        return sendCacheableImage(imageDir, filename)
    app.add_url_rule('/img/<username>/artists/<filename>', 'serveArtistImage', serveArtistImage)
