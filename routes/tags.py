# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tagging system and tag-filtered playlist export routes.
"""
import logging
import re
from flask import render_template, redirect, request, url_for, jsonify, Response, stream_with_context, abort

from config import PLAYLIST_EXPORT_FORMATS, WRAPPED_TOP_SONGS_EXPORT_LIMIT
from routes._auth import makeRequiresUser
from services.export import resolvePlaylistFormat

logger = logging.getLogger(__name__)

# Tags are user-controlled free text; strip everything but a safe ASCII subset
# before putting them in the download filename so a tag containing a quote,
# newline, or non-Latin-1 character can't break or inject into the
# Content-Disposition header (Werkzeug encodes it as Latin-1).
FILENAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _requestedTags() -> list[str]:
    """The ?tags= selection: one repeated `tags` query param per tag
    (?tags=a&tags=b - what playlists.js sends).

    getlist, never a comma-split: a tag NAME may contain a comma
    (normalizeTag allows one), and the old joined-and-split protocol turned
    such a tag into two that don't exist - creatable, rendered as a chip,
    matched never. An old bookmarked comma-joined multi-tag URL now reads as
    ONE unknown tag and downloads an empty playlist; the page itself builds a
    fresh URL per click, so nothing but a stale bookmark ever sends one."""
    return [t.strip() for t in request.args.getlist("tags") if t.strip()]


def _requestFields(*names) -> list[str] | None:
    """The named fields of a JSON or form body, stripped - or None when the
    body is not a mapping at all.

    The three write endpoints below each read `get_json(silent=True) or
    request.form` and went straight to `.get(name, "").strip()`. Two shapes
    reached that as an AttributeError, which Flask serves as a 500 with a
    traceback in the log, for what is only a badly-shaped request:
    a JSON body that is an array or a scalar (no `.get`), and a field whose
    value is null or a number (no `.strip`). The second is the easier one to
    send by accident - `{"tag": null}` is what a client sends for an
    unfilled field.

    A non-string field reads as absent rather than as its str(): the callers
    all reject empty input already, so `{"tag": 5}` becomes "Missing ... tag"
    instead of silently creating a tag named "5".

    Duck-typed on `.get` rather than `isinstance(data, dict)`: request.form is
    a Werkzeug MultiDict, and pinning this to that class's current base would
    turn every form post into a 400 if it ever changes."""
    data = request.get_json(silent=True) or request.form
    if not hasattr(data, "get"):
        return None
    return [value.strip() if isinstance(value := data.get(name), str) else "" for name in names]


def register(app, dashboard):
    # These routes predate routes/_auth.py and each hand-rolled the guard, in a
    # different dialect: seven copies answering {"error": "unauthorized"} where
    # the decorator answers {"error": "Not logged in"}, so a client checking the
    # message had to know which half of the API it was calling. The pages here
    # keep the redirect - the bare flavour - because neither is fetched.
    requiresUser = makeRequiresUser(dashboard)

    @requiresUser(api=True)
    def addTagApi(username, db):
        if not dashboard.repo.isTagsEnabled():
            abort(404)

        fields = _requestFields("entity_type", "entity_id", "tag")
        if fields is None:
            return jsonify({"error": "Invalid payload"}), 400
        entity_type, entity_id, tag = fields

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

    @requiresUser(api=True)
    def removeTagApi(username, db):
        if not dashboard.repo.isTagsEnabled():
            abort(404)

        fields = _requestFields("entity_type", "entity_id", "tag")
        if fields is None:
            return jsonify({"error": "Invalid payload"}), 400
        entity_type, entity_id, tag = fields

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

    @requiresUser(api=True)
    def listUserTagsApi(username, db):
        if not dashboard.repo.isTagsEnabled():
            abort(404)

        tags = db.repo.getUserTags(username)
        return jsonify({"tags": tags})

    app.add_url_rule("/api/tags", "listUserTagsApi", listUserTagsApi, methods=["GET"])

    @requiresUser(api=True)
    def getEntityTagsApi(username, db):
        # FOLLOW-UP B (2026-09-02 review, from UT-3's 701191b): tags.js's
        # overlapping-add refetch used to re-fetch and re-parse the whole
        # detail PAGE just to get an authoritative tag list back out of its
        # .tag-widget - the same read every add/remove response already
        # carries, exposed here on its own so a refetch costs one small JSON
        # response instead of a full page render.
        if not dashboard.repo.isTagsEnabled():
            abort(404)

        entity_type = request.args.get("entity_type", "").strip()
        entity_id = request.args.get("entity_id", "").strip()
        if not entity_type or not entity_id:
            return jsonify({"error": "Missing entity_type or entity_id"}), 400

        tags = db.repo.getTagsForEntity(username, entity_type, entity_id)
        return jsonify({"tags": tags})

    app.add_url_rule("/api/tags/entity", "getEntityTagsApi", getEntityTagsApi, methods=["GET"])

    @requiresUser(api=True)
    def renameTagApi(username, db):
        if not dashboard.repo.isTagsEnabled():
            abort(404)

        fields = _requestFields("old_tag", "new_tag")
        if fields is None:
            return jsonify({"error": "Invalid payload"}), 400
        old_tag, new_tag = fields

        if not old_tag or not new_tag:
            return jsonify({"error": "Missing old_tag or new_tag"}), 400

        try:
            cnt = db.repo.renameTag(username, old_tag, new_tag)
            db.repo.commit()
            return jsonify({"success": True, "count": cnt})
        except Exception as e:
            logger.error("Error renaming tag: %s", e)
            return jsonify({"error": "Failed to rename tag"}), 500

    app.add_url_rule("/api/tags/rename", "renameTagApi", renameTagApi, methods=["POST"])

    @requiresUser(api=True)
    def deleteTagApi(username, db, tag):
        if not dashboard.repo.isTagsEnabled():
            abort(404)

        try:
            cnt = db.repo.deleteTag(username, tag)
            db.repo.commit()
            return jsonify({"success": True, "count": cnt})
        except Exception as e:
            logger.error("Error deleting tag: %s", e)
            return jsonify({"error": "Failed to delete tag"}), 500

    # <path:tag> rather than the default string converter, which rejects
    # slashes: normalizeTag allows them, so a tag like "rock/metal" could be
    # created but never deleted - the client's %2F decodes back to a slash
    # before routing and no rule matched, so Delete silently 404'd.
    app.add_url_rule("/api/tags/<path:tag>", "deleteTagApi", deleteTagApi, methods=["DELETE"])

    @requiresUser
    def playlistsPage(username, db):
        if not dashboard.repo.isTagsEnabled():
            abort(404)

        user_tags = db.repo.getUserTags(username)
        return render_template("playlists.html", section="playlists", username=username, user_tags=user_tags)

    app.add_url_rule("/playlists", "playlistsPage", playlistsPage, methods=["GET"])

    @requiresUser(api=True)
    def playlistPreviewApi(username, db):
        if not dashboard.repo.isTagsEnabled():
            abort(404)

        tags = _requestedTags()
        match_mode = request.args.get("match", "any")

        if not tags:
            return jsonify({"track_count": 0})

        tracks = db.getTaggedTracks(tags, match_mode=match_mode)
        return jsonify({"track_count": len(tracks)})

    app.add_url_rule("/api/playlists/preview", "playlistPreviewApi", playlistPreviewApi, methods=["GET"])

    @requiresUser
    def playlistExport(username, db):

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

            ctx = dashboard._buildWrappedContext(db, year, groupBy="week", limit=WRAPPED_TOP_SONGS_EXPORT_LIMIT, sortBy="plays",
                                                  includeGenres=False)
            tracks = ctx["topSongs"]
            filename = f"wrapped_top100_{year}.{fmt}"
            title = f"Wrapped {year} Top 100"
        else:
            if not dashboard.repo.isTagsEnabled():
                abort(404)
            tags = _requestedTags()
            match_mode = request.args.get("match", "any")
            sortBy = request.args.get("sort", "plays")
            if sortBy not in ("plays", "recent", "name"):
                sortBy = "plays"

            tracks = db.getTaggedTracks(tags, match_mode=match_mode, sortBy=sortBy)

            tag_summary = FILENAME_UNSAFE_RE.sub("_", "_".join(tags[:3])).strip("_") if tags else "all"
            filename = f"playlist_{tag_summary or 'all'}.{fmt}"
            title = f"Playlist ({', '.join(tags)})" if tags else "Playlist"

        generator, mimetype = resolvePlaylistFormat(tracks, fmt, title)
        response = Response(stream_with_context(generator), mimetype=mimetype)
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    app.add_url_rule("/playlist/export", "playlistExport", playlistExport, methods=["GET"])
