"""Authenticated image-serving routes (/img/<username>/tracks|artists/...).

Extracted verbatim from app.py. Track/artist images are shared across every
user (Database.imgDir_* are class-level, not per user) - the <username> segment
is only the authorization check, not a directory selector.
"""
import os
from pathlib import Path

from flask import session, send_from_directory

from Database.database import Database


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
        return send_from_directory(Database.imgDir_tracks, filename)
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

        return send_from_directory(imageDir, filename)
    app.add_url_rule('/img/<username>/artists/<filename>', 'serveArtistImage', serveArtistImage)
