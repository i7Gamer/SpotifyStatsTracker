# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Authenticated image-serving routes (/img/<username>/tracks|artists/...).

Extracted verbatim from app.py. Track/artist images are shared across every
user (Database.imgDir_* are class-level, not per user) - the <username> segment
is only the authorization check, not a directory selector.
"""
import os
import re
from pathlib import Path

from flask import make_response, session, send_from_directory

from config import IMAGE_CACHE_CONTROL, IMAGE_CACHE_MAX_AGE_SECONDS
from Database.database import Database
from Database.db import SPOTIFY_TRACK_ID_LENGTH
from Database.repository import IMAGE_KIND_ARTIST, IMAGE_KIND_TRACK

# What a Spotify entity id looks like - every kind shares the track id's shape
# (22 base62 characters, see SPOTIFY_TRACK_ID_LENGTH). The artist route's lazy
# fetch is gated on this BEFORE the catalog read: the client names the id, and
# a fetch for an unknown one costs two outbound Spotify requests (one on the
# deliberately unlimited pathfinder path) plus an images row, per distinct id.
SPOTIFY_ID_RE = re.compile(rf"[0-9A-Za-z]{{{SPOTIFY_TRACK_ID_LENGTH}}}")


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
        #< a cookie the account has signed out everywhere carries no email by
        #  the time this runs - SpotifyDashboardApp._endSessionsTheAccountHasInvalidated
        #  clears it before any route sees the request
        email = session.get("email")
        if not email or not dashboard.is_user_logged_in(email):
            return None
        return dashboard.get_username_for_email(email)

    def _imageIdFromFilename(filename):
        """The `<imageId>` of an `<imageId>.jpeg` filename, or None for
        anything else - the ids are alphanumeric, so a stem that is not one
        names no image and must reach neither the images table nor a fetch."""
        stem, _extension = os.path.splitext(filename)
        return stem if stem.isalnum() else None

    def _forgetMissingImage(username, imageId, kind):
        """The file is not on disk: if the images table still says 'ok' for
        it, that verdict is stale - a database restored without
        Database/Data/Media - and both fetch gates would honour it forever.
        Forget it here, where the gap is already known, so the artist path
        below claims and fetches on this request and the listener's next
        saveTrackImg re-claims the cover. See Repository.forgetImageStatus for
        why only 'ok' rows go."""
        db = dashboard.user_databases.get(username)
        if db and imageId:
            db.repo.forgetImageStatus(imageId, kind)
        return db

    def serveTrackImage(username, filename):
        if username != _authorized_image_username() or filename != os.path.basename(filename):
            return "", 404
        imageDir = Database.imgDir_tracks
        if not os.path.exists(os.path.join(imageDir, filename)):
            _forgetMissingImage(username, _imageIdFromFilename(filename), IMAGE_KIND_TRACK)
        return sendCacheableImage(imageDir, filename)
    app.add_url_rule('/img/<username>/tracks/<filename>', 'serveTrackImage', serveTrackImage)

    def serveArtistImage(username, filename):
        if username != _authorized_image_username() or filename != os.path.basename(filename):
            return "", 404
        imageDir = Database.imgDir_artists
        imagePath = os.path.join(imageDir, filename)

        if not os.path.exists(imagePath):
            artistId = _imageIdFromFilename(filename)
            db = _forgetMissingImage(username, artistId, IMAGE_KIND_ARTIST)
            # Only an artist the catalog knows is worth asking Spotify about;
            # templates never reference any other. Shape first (free), then the
            # indexed-PK read - see SPOTIFY_ID_RE for what an unknown id costs.
            if (db and artistId and SPOTIFY_ID_RE.fullmatch(artistId)
                    and db.repo.artistExists(artistId)):
                db.lazyFetchArtistImage(artistId, Path(imagePath))

        return sendCacheableImage(imageDir, filename)
    app.add_url_rule('/img/<username>/artists/<filename>', 'serveArtistImage', serveArtistImage)
