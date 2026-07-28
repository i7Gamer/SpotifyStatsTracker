# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tagging system and tag-filtered playlist export routes.
"""
import logging
import re
from flask import render_template, redirect, request, url_for, jsonify, Response, stream_with_context, abort

from config import PLAYLIST_EXPORT_FORMATS
from services.export import generatePlaylistCsv, generatePlaylistM3u, generatePlaylistXspf

logger = logging.getLogger(__name__)

# Tags are user-controlled free text; strip everything but a safe ASCII subset
# before putting them in the download filename so a tag containing a quote,
# newline, or non-Latin-1 character can't break or inject into the
# Content-Disposition header (Werkzeug encodes it as Latin-1).
FILENAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def register(app, dashboard):

    def addTagApi():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return jsonify({"error": "unauthorized"}), 401
        if not dashboard.repo.isTagsEnabled():
            abort(404)

        data = request.get_json(silent=True) or request.form
        entity_type = data.get("entity_type", "").strip()
        entity_id = data.get("entity_id", "").strip()
        tag = data.get("tag", "").strip()

        if not entity_type or not entity_id or not tag:
            return jsonify({"error": "Missing entity_type, entity_id, or tag"}), 400

        try:
            norm = db.repo.addTag(username, tag, entity_type, entity_id)
            if norm is None:
                # addTag normalizes away leading '#' and whitespace, so input
                # like "#" or "  " stores nothing - reporting success (with a
                # null tag) told the client a tag existed that never did.
                return jsonify({"error": "Tag is empty after normalization"}), 400
            db.repo.commit()
            tags = db.repo.getTagsForEntity(username, entity_type, entity_id)
            return jsonify({"success": True, "tag": norm, "tags": tags})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            logger.error("Error adding tag: %s", e)
            return jsonify({"error": "Failed to add tag"}), 500

    app.add_url_rule("/api/tags", "addTagApi", addTagApi, methods=["POST"])

    def removeTagApi():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return jsonify({"error": "unauthorized"}), 401
        if not dashboard.repo.isTagsEnabled():
            abort(404)

        data = request.get_json(silent=True) or request.form
        entity_type = data.get("entity_type", "").strip()
        entity_id = data.get("entity_id", "").strip()
        tag = data.get("tag", "").strip()

        if not entity_type or not entity_id or not tag:
            return jsonify({"error": "Missing entity_type, entity_id, or tag"}), 400

        try:
            db.repo.removeTag(username, tag, entity_type, entity_id)
            db.repo.commit()
            tags = db.repo.getTagsForEntity(username, entity_type, entity_id)
            return jsonify({"success": True, "tags": tags})
        except Exception as e:
            logger.error("Error removing tag: %s", e)
            return jsonify({"error": "Failed to remove tag"}), 500

    app.add_url_rule("/api/tags", "removeTagApi", removeTagApi, methods=["DELETE"])

    def listUserTagsApi():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return jsonify({"error": "unauthorized"}), 401
        if not dashboard.repo.isTagsEnabled():
            abort(404)

        tags = db.repo.getUserTags(username)
        return jsonify({"tags": tags})

    app.add_url_rule("/api/tags", "listUserTagsApi", listUserTagsApi, methods=["GET"])

    def renameTagApi():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return jsonify({"error": "unauthorized"}), 401
        if not dashboard.repo.isTagsEnabled():
            abort(404)

        data = request.get_json(silent=True) or request.form
        old_tag = data.get("old_tag", "").strip()
        new_tag = data.get("new_tag", "").strip()

        if not old_tag or not new_tag:
            return jsonify({"error": "Missing old_tag or new_tag"}), 400

        cnt = db.repo.renameTag(username, old_tag, new_tag)
        db.repo.commit()
        return jsonify({"success": True, "count": cnt})

    app.add_url_rule("/api/tags/rename", "renameTagApi", renameTagApi, methods=["POST"])

    def deleteTagApi(tag):
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return jsonify({"error": "unauthorized"}), 401
        if not dashboard.repo.isTagsEnabled():
            abort(404)

        cnt = db.repo.deleteTag(username, tag)
        db.repo.commit()
        return jsonify({"success": True, "count": cnt})

    # <path:tag> rather than the default string converter, which rejects
    # slashes: normalizeTag allows them, so a tag like "rock/metal" could be
    # created but never deleted - the client's %2F decodes back to a slash
    # before routing and no rule matched, so Delete silently 404'd.
    app.add_url_rule("/api/tags/<path:tag>", "deleteTagApi", deleteTagApi, methods=["DELETE"])

    def playlistsPage():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))
        if not dashboard.repo.isTagsEnabled():
            abort(404)

        user_tags = db.repo.getUserTags(username)
        return render_template("playlists.html", section="playlists", username=username, user_tags=user_tags)

    app.add_url_rule("/playlists", "playlistsPage", playlistsPage, methods=["GET"])

    def playlistPreviewApi():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return jsonify({"error": "unauthorized"}), 401
        if not dashboard.repo.isTagsEnabled():
            abort(404)

        tags_param = request.args.get("tags", "")
        tags = [t.strip() for t in tags_param.split(",") if t.strip()]
        match_mode = request.args.get("match", "any")

        if not tags:
            return jsonify({"track_count": 0})

        tracks = db.getTaggedTracks(tags, match_mode=match_mode)
        return jsonify({"track_count": len(tracks)})

    app.add_url_rule("/api/playlists/preview", "playlistPreviewApi", playlistPreviewApi, methods=["GET"])

    def playlistExport():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        fmt = request.args.get("format", "csv").lower()
        if fmt not in PLAYLIST_EXPORT_FORMATS:
            fmt = "csv"

        yearParam = request.args.get("year")
        if yearParam is not None:
            # Wrapped's Top 100 songs for one year, reusing the same cached
            # pool the Wrapped page itself renders from (see
            # dashboard._buildWrappedContext) - not a tag filter.
            try:
                year = int(yearParam)
            except (TypeError, ValueError):
                abort(400)
            if year not in dashboard._computeAvailableYears(db):
                abort(400)

            ctx = dashboard._buildWrappedContext(db, year, groupBy="week", limit=100, sortBy="plays",
                                                  includeGenres=False)
            tracks = ctx["topSongs"]
            filename = f"wrapped_top100_{year}.{fmt}"
            title = f"Wrapped {year} Top 100"
        else:
            if not dashboard.repo.isTagsEnabled():
                abort(404)
            tags_param = request.args.get("tags", "")
            tags = [t.strip() for t in tags_param.split(",") if t.strip()]
            match_mode = request.args.get("match", "any")
            sortBy = request.args.get("sort", "plays")
            if sortBy not in ("plays", "recent", "name"):
                sortBy = "plays"

            tracks = db.getTaggedTracks(tags, match_mode=match_mode, sortBy=sortBy)

            tag_summary = FILENAME_UNSAFE_RE.sub("_", "_".join(tags[:3])).strip("_") if tags else "all"
            filename = f"playlist_{tag_summary or 'all'}.{fmt}"
            title = f"Playlist ({', '.join(tags)})" if tags else "Playlist"

        if fmt == "m3u":
            generator = generatePlaylistM3u(tracks)
            mimetype = "audio/x-mpegurl"
        elif fmt == "xspf":
            generator = generatePlaylistXspf(tracks, title=title)
            mimetype = "application/xspf+xml; charset=utf-8"
        else:
            generator = generatePlaylistCsv(tracks)
            mimetype = "text/csv; charset=utf-8"

        response = Response(stream_with_context(generator), mimetype=mimetype)
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    app.add_url_rule("/playlist/export", "playlistExport", playlistExport, methods=["GET"])
