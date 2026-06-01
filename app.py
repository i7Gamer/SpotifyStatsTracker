import os
import json
import threading
from pathlib import Path

from flask import Flask, render_template, redirect, request, url_for, jsonify, send_from_directory

from Database.database import import_spotify_history, read_progress

app = Flask(__name__)
baseDir = Path(__file__).resolve().parent
USERNAME = "Tzur"

@app.route('/img/<username>/tracks/<filename>')
def serve_track_image(username, filename):
    imageDir = os.path.join(baseDir, "Database", "img", username, "tracks")
    return send_from_directory(imageDir, filename)

def load_history(start=None, end=None) -> list:
    history_path = baseDir / "Database" / "history.json"
    if not history_path.exists():
        return []

    try:
        with history_path.open("r", encoding="utf-8") as f:
            tracks = json.load(f)
            if start is not None and end is not None:
                tracks = tracks[start:end]
            return tracks
    except json.JSONDecodeError:
        return []
    except Exception:
        return []


def get_latest_history(limit=None):
    tracks = load_history()
    if limit is not None:
        size = len(tracks)
        tracks = tracks[max(size - limit, 0) : size]
    return tracks


def run_import_background(history_data):
    try:
        import_spotify_history(history_data)
    except Exception:
        # The import helper writes failure state to progress.json
        pass


@app.route("/", methods=["GET"])
def dashboard():
    tracks = get_latest_history(50)
    total_duration_ms = sum(track.get("duration", 0) for track in tracks)
    duration_hours = total_duration_ms // 3_600_000
    duration_minutes = (total_duration_ms % 3_600_000) // 60_000
    total_duration = (
        f"{duration_hours}h {duration_minutes}m"
        if duration_hours
        else f"{duration_minutes}m"
    )

    unique_artists = len({track.get("artist") for track in tracks if track.get("artist")})

    return render_template(
        "tracks.html",
        tracks=tracks,
        total=len(tracks),
        uniqueArtists=unique_artists,
        totalDuration=total_duration,
        username = USERNAME
    )


@app.route("/import-history", methods=["POST"])
def import_history():
    if read_progress().get("status") == "running":
        return redirect(url_for("import_page"))

    upload = request.files.get("history_file")
    if upload is None or upload.filename == "":
        return redirect(url_for("import_page"))

    try:
        history_data = json.load(upload)
    except json.JSONDecodeError:
        return redirect(url_for("import_page"))

    thread = threading.Thread(target=run_import_background, args=(history_data,), daemon=True)
    thread.start()
    return redirect(url_for("import_page"))


@app.route("/import", methods=["GET"])
def import_page():
    return render_template("import.html", importProgress=read_progress())


@app.route("/import-progress", methods=["GET"])
def import_progress():
    return jsonify(read_progress())


if __name__ == "__main__":
    app.run(debug=True, port=5000)
