import json
from pathlib import Path
from io import BytesIO

import requests
from PIL import Image

try:
    from Database.Formatters.spotifyClient import Client
    from Database.Importers.StreamingHistoryImporter import Importer
except ModuleNotFoundError:
    from Formatters.spotifyClient import Client
    from Importers.StreamingHistoryImporter import Importer

USER = "Tzur"
baseDir = Path(__file__).resolve().parent
imgDir = baseDir / "img" / USER / "tracks"
downloadedImagesPath = imgDir / "metadata.json"
historyPath = baseDir / "history.json"
progressPath = baseDir / "progress.json"


def ensure_json_file(path: Path, default):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps(default, indent=4), encoding="utf-8")
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        path.write_text(json.dumps(default, indent=4), encoding="utf-8")
        return default


downloadedImages = []
if downloadedImagesPath.exists():
    try:
        downloadedImages = json.loads(downloadedImagesPath.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        downloadedImages = []


def write_progress(status, current=0, total=0, message="", error=False):
    payload = {
        "status": status,
        "current": current,
        "total": total,
        "percentage": round((current / total * 100) if total else 0),
        "message": message,
        "error": error,
    }
    progressPath.parent.mkdir(parents=True, exist_ok=True)
    progressPath.write_text(json.dumps(payload, indent=4), encoding="utf-8")


def read_progress():
    if not progressPath.exists():
        return {
            "status": "idle",
            "current": 0,
            "total": 0,
            "percentage": 0,
            "message": "",
            "error": False,
        }
    try:
        return json.loads(progressPath.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "status": "idle",
            "current": 0,
            "total": 0,
            "percentage": 0,
            "message": "",
            "error": False,
        }


def load_history() -> list:
    return ensure_json_file(historyPath, [])


def save_history(history):
    historyPath.parent.mkdir(parents=True, exist_ok=True)
    historyPath.write_text(json.dumps(history, indent=4), encoding="utf-8")


def save_img(url, id):
    imgDir.mkdir(parents=True, exist_ok=True)
    if id in downloadedImages:
        print(f"Image for {id} already downloaded.")
        return
    try:
        response = requests.get(url)
        response.raise_for_status()
        img = Image.open(BytesIO(response.content))
        ext = img.format.lower() if img.format else "jpg"
        img.save(imgDir / f"{id}.{ext}")
        downloadedImages.append(id)
        downloadedImagesPath.parent.mkdir(parents=True, exist_ok=True)
        downloadedImagesPath.write_text(json.dumps(downloadedImages, indent=4), encoding="utf-8")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching image from {url}: {e}")
    except Exception as e:
        print(f"Error saving image: {e}")


def add_to_history_from_data(meta):
    save_img(meta["imageUrl"], meta["imageId"])
    history = load_history()
    history.append(meta)
    save_history(history)


def add_to_history_from_track_data(timestamp, track):
    add_to_history_from_data(Client.formatTrack(timestamp, track))


def import_spotify_history(exportedHistory):
    history = load_history()
    importer = Importer()
    total = len(exportedHistory) if isinstance(exportedHistory, list) else 0
    write_progress("running", 0, total, "Starting import")
    try:
        for index, meta in enumerate(importer.importHistory(exportedHistory), start=1):
            save_img(meta["imageUrl"], meta["imageId"])
            history.append(meta)
            write_progress("running", index, total, f"Imported {index} of {total}")
        save_history(history)
        write_progress("complete", total, total, "Import complete")
    except Exception as e:
        write_progress("failed", index if "index" in locals() else 0, total, f"Import failed: {e}", error=True)
        raise


if __name__ == "__main__":
    import SpotipyFree
    import datetime
    import pysole

    sp = SpotipyFree.Spotify()
    sp.login()

    # pysole.probe(runRemainingCode=True, printStartupCode=True)
    # track = sp.track("67Hna13dNDkZvBpTXRIaOJ")
    # with open("track.json", "r") as f:
    #     track = json.load(f)
    # addToHistoryFromRaw(str(datetime.datetime.now().timestamp()), track)

    with open("importMe.json", "r", encoding="utf-8") as f:
        history_data = json.load(f)
    import_spotify_history(history_data)
