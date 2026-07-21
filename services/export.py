"""Streaming CSV/JSON export of a user's play history.

Extracted verbatim from app.py (behavior-preserving). Each play is re-emitted in
Spotify's own extended-streaming-history shape so an export re-imports cleanly
through the existing pipeline. app.py's /export-history route consumes
generateJsonExport / generateCsvExport, which stream in bounded chunks so an
export never holds the whole history in memory.
"""
import csv
import io
import json
from datetime import datetime, timezone

EXPORT_CHUNK_SIZE = 5000         #< plays hydrated per round-trip while streaming an export
EXPORT_CSV_COLUMNS = ("played_at_utc", "track_name", "artists", "album", "ms_played", "spotify_track_uri", "played_from")

# Behavioral columns emitted as-is vs. as booleans - under Spotify's own
# key names (incognito is stored under the column name but exported as
# incognito_mode), so the export re-imports through _extractExtras.
EXPORT_TEXT_EXTRAS = ("platform", "conn_country", "reason_start", "reason_end")
EXPORT_BOOL_EXTRAS = (("shuffle", "shuffle"), ("skipped", "skipped"),
                      ("offline", "offline"), ("incognito", "incognito_mode"))


def iterExportEntries(db, includeSkips=False):
    """Every play (oldest first) with hydrated track metadata, fetched in
    EXPORT_CHUNK_SIZE batches so an export never holds the whole history
    in memory. Plays recorded while the export streams have the newest
    played_at, so they can only appear at the very end - earlier chunks
    can't shift underneath the OFFSET pagination.

    includeSkips: skip events follow after every play (their sub-threshold
    ms_played routes them back into play_skips on reimport). JSON only -
    the CSV stays plays-only for spreadsheet use."""
    startIndex = 0
    while True:
        entries = db.getEntriesFromOld(count=EXPORT_CHUNK_SIZE, startIndex=startIndex)
        if not entries:
            break
        yield from entries
        startIndex += EXPORT_CHUNK_SIZE
    if not includeSkips:
        return
    startIndex = 0
    while True:
        entries = db.getSkipEntriesFromOld(count=EXPORT_CHUNK_SIZE, startIndex=startIndex)
        if not entries:
            return
        yield from entries
        startIndex += EXPORT_CHUNK_SIZE


def isoUtc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def exportEntryToDict(entry) -> dict:
    """One play in Spotify's own extended-streaming-history shape, so the
    export re-imports through the existing pipeline. `ts` is the play's
    END time - Spotify's convention, which importExtendedHistory converts
    back to a start time by subtracting ms_played. Behavioral fields are
    emitted only when stored; offline plays also carry offline_timestamp
    (their corrected start), which the importer prefers over ts."""
    artists = entry.get("artists") or []
    album = entry.get("album") or {}
    item = {
        "ts": isoUtc(entry["playedAt"] + entry["timePlayed"] // 1000),
        "ms_played": entry["timePlayed"],
        "master_metadata_track_name": entry.get("name"),
        "master_metadata_album_artist_name": artists[0].get("name") if artists else None,
        "master_metadata_album_album_name": album.get("name") if album else None,
        "spotify_track_uri": f"spotify:track:{entry['id']}",
        "played_from": entry.get("playedFrom"),   #< extra field; the importer ignores it
    }
    extras = entry.get("extras") or {}
    for column in EXPORT_TEXT_EXTRAS:
        if extras.get(column) is not None:
            item[column] = extras[column]
    for column, exportKey in EXPORT_BOOL_EXTRAS:
        if extras.get(column) is not None:
            item[exportKey] = bool(extras[column])
    if extras.get("offline"):
        item["offline_timestamp"] = int(entry["playedAt"])
    return item


def generateJsonExport(db):
    yield "[\n"
    first = True
    for entry in iterExportEntries(db, includeSkips=True):
        prefix = "" if first else ",\n"
        first = False
        yield prefix + json.dumps(exportEntryToDict(entry), ensure_ascii=False)
    yield "\n]\n"


def generateCsvExport(db):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EXPORT_CSV_COLUMNS)
    for entry in iterExportEntries(db):
        artists = entry.get("artists") or []
        album = entry.get("album") or {}
        writer.writerow([
            isoUtc(entry["playedAt"]),   #< the START time - more intuitive for spreadsheet use
            entry.get("name") or "",
            ", ".join(a.get("name", "") for a in artists),
            album.get("name") or "" if album else "",
            entry["timePlayed"],
            f"spotify:track:{entry['id']}",
            entry.get("playedFrom") or "",
        ])
        if buffer.tell() >= 64 * 1024:   #< flush in ~64KB chunks instead of per row or all at once
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
    yield buffer.getvalue()
