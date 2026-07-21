"""System / utility routes: health check, history import + export, import
progress, version status, and the small listener/now-playing JSON APIs.

Extracted verbatim from app.py. Streaming export uses services/export; the
app-level MAX_UPLOAD_MB / EXPORT_FORMATS constants are aliased from the app
module. The 413 upload-too-large error handler stays app-global in app.py.
"""
import logging
import threading

from flask import (
    render_template, redirect, request, url_for, jsonify, Response, stream_with_context,
)

import app as appmod
from Database.utils import versionTuple, now
from services.export import generateJsonExport, generateCsvExport

logger = logging.getLogger(__name__)


def register(app, dashboard):
    MAX_UPLOAD_MB = appmod.MAX_UPLOAD_MB
    EXPORT_FORMATS = appmod.EXPORT_FORMATS

    def _is_version_newer(remote: str, local: str) -> bool:
        try:
            return versionTuple(remote) > versionTuple(local)
        except Exception:
            return False

    def health():
        """Cheap, unauthenticated liveness/readiness check for container
        orchestration and uptime monitoring - does a trivial query rather
        than just returning 200 unconditionally, so it can tell "process
        alive" apart from "process alive but the database is unreachable"
        (the single point of failure for this app)."""
        try:
            dashboard.repo.connection().execute("SELECT 1").fetchone()
            return jsonify({"status": "ok"}), 200
        except Exception as e:
            logger.error("Health check failed: %s", e)
            return jsonify({"status": "error", "detail": str(e)}), 503
    app.add_url_rule("/health", "health", health, methods=["GET"])

    def importHistory():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login"))

        if db.readProgress().get("status") == "running":
            return redirect(url_for("importPage"))

        uploads = [f for f in request.files.getlist("history_file") if f and f.filename]
        if not uploads:
            return redirect(url_for("importPage"))

        contents = []
        for upload in uploads:
            try:
                contents.append(upload.read().decode("utf-8"))
            except UnicodeDecodeError:
                # Mirrors AutoImporter._handleImport's per-file resilience
                # (see its try/except around open(..., encoding="utf-8"))
                # - one unreadable file must not 500 the whole request and
                # drop every other file in the same upload.
                logger.warning("Skipping upload %r for user %s: not valid UTF-8 text", upload.filename, username)
        if not contents:
            return redirect(url_for("importPage"))

        # Marked "running" here, synchronously, rather than via a
        # post-thread-start time.sleep(1) "give it a moment" delay - that
        # blocked a Waitress worker thread on every submission and still
        # couldn't fully guarantee the background thread's own first
        # writeProgress() call (inside Database.importHistory, gated on
        # parsing the export first) had actually landed by the time it
        # returned.
        # Captured before the thread starts - no request context inside it.
        overwriteRange = request.form.get("overwrite_range") is not None
        db.writeProgress("running", 0, 0, "Starting import")
        thread = threading.Thread(target=db.importHistoryBatch, args=(contents,),
                                  kwargs={"overwriteRange": overwriteRange}, daemon=True)
        thread.start()
        return redirect(url_for("importPage"))
    app.add_url_rule("/import-history", "importHistory", importHistory, methods=["POST"])

    def importPage():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login"))
        return render_template(
            "import.html",
            importProgress=db.readProgress(),
            maxUploadMb=MAX_UPLOAD_MB,
            uploadTooLarge=request.args.get("error") == "upload_too_large",
            section="import",
        )
    app.add_url_rule("/import", "importPage", importPage, methods=["GET"])

    def exportHistory():
        """Stream the current user's full play history as a download.
        JSON is shaped like Spotify's own extended export (re-importable
        through /import-history); CSV is for spreadsheets."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=request.path))

        exportFormat = request.args.get("format", "json")
        if exportFormat not in EXPORT_FORMATS:
            exportFormat = "json"

        dateText = now(tz=db.tz).strftime("%Y-%m-%d")
        filename = f"spotify_stats_export_{username}_{dateText}.{exportFormat}"
        if exportFormat == "csv":
            generator, mimetype = generateCsvExport(db), "text/csv; charset=utf-8"
        else:
            generator, mimetype = generateJsonExport(db), "application/json"

        response = Response(stream_with_context(generator), mimetype=mimetype)
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
    app.add_url_rule("/export-history", "exportHistory", exportHistory, methods=["GET"])

    def importProgress():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify(db.readProgress())
    app.add_url_rule("/import-progress", "importProgress", importProgress, methods=["GET"])

    def version_status():
        # Return the current and latest versions (latest is null if not newer)
        with dashboard._version_lock:
            latest = dashboard.latestVersion
        if latest and _is_version_newer(latest, dashboard.currentVersion):
            return jsonify({"current": dashboard.currentVersion, "latest": latest})
        else:
            return jsonify({"current": dashboard.currentVersion, "latest": None})
    app.add_url_rule("/version_status", "version_status", version_status, methods=["GET"])

    def listenerStatus():
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return jsonify({"error": "Not logged in"}), 401
        health = db.getListenerHealth()
        return jsonify(health)
    app.add_url_rule("/api/listener-status", "listenerStatus", listenerStatus, methods=["GET"])

    def nowPlayingStatus():
        """What the user is playing right now, from the listener's cached
        connect state (no Spotify calls) - polled by the dashboard."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return jsonify({"error": "Not logged in"}), 401
        return jsonify({"nowPlaying": db.getNowPlaying()})
    app.add_url_rule("/api/now-playing", "nowPlayingStatus", nowPlayingStatus, methods=["GET"])
