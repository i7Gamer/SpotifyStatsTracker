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


def _iterKeysetChunks(fetch):
    """Stream every row `fetch(afterTs)` can return, oldest first, paging by
    position in time instead of by OFFSET.

    OFFSET pagination assumed the rows behind the cursor never move. Inserts
    can't move them (a new play has the newest played_at), but DELETES can: an
    overwrite import's covered-range wipe, or the listener's Web-API
    reconciliation removing a play, shifts every later row left by one - and
    the next OFFSET then steps straight over that many entries, silently
    dropping them from the file the user downloads.

    played_at is not unique on its own (two different tracks can carry the same
    timestamp), so each chunk starts AT the last timestamp seen and the entries
    already emitted at exactly that timestamp are filtered out."""
    afterTs = None
    emittedAtCursor = set()
    while True:
        entries = fetch(afterTs)
        if not entries:
            return
        fresh = [e for e in entries if (e.get("id"), e.get("playedAt")) not in emittedAtCursor]
        if not fresh:
            # Only reachable if a whole chunk shares one timestamp, which would
            # need EXPORT_CHUNK_SIZE distinct tracks played in the same second.
            return
        yield from fresh
        afterTs = fresh[-1].get("playedAt")
        emittedAtCursor = {(e.get("id"), e.get("playedAt")) for e in fresh
                           if e.get("playedAt") == afterTs}
        if len(entries) < EXPORT_CHUNK_SIZE:
            return


def iterExportEntries(db, includeSkips=False):
    """Every play (oldest first) with hydrated track metadata, fetched in
    EXPORT_CHUNK_SIZE batches so an export never holds the whole history
    in memory.

    includeSkips: skip events (plays.is_skip=1) follow after every play (their
    sub-threshold ms_played re-imports as is_skip=1). JSON only - the CSV stays
    real-plays-only for spreadsheet use."""
    yield from _iterKeysetChunks(
        lambda afterTs: db.getEntriesFromOld(count=EXPORT_CHUNK_SIZE, afterTs=afterTs))
    if not includeSkips:
        return
    yield from _iterKeysetChunks(
        lambda afterTs: db.getSkipEntriesFromOld(count=EXPORT_CHUNK_SIZE, afterTs=afterTs))


# Spreadsheet apps (Excel, Sheets, LibreOffice) treat a cell whose text starts
# with one of these as a formula. Track/artist/album names come from Spotify's
# catalog rather than this app, but a name crafted to start with one of these
# could execute when the exported CSV is opened ("CSV formula injection"), so
# such a cell is prefixed with an apostrophe to force literal-text rendering.
# Only the human-readable text columns are guarded; the machine-readable
# URI/URL/ISRC columns importers match on are left byte-for-byte identical.
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csvSafeCell(value) -> str:
    text = "" if value is None else str(value)
    if text and text[0] in CSV_FORMULA_PREFIXES:
        return "'" + text
    return text


def isoUtc(timestamp: float) -> str:
    """Spotify's `ts` shape, keeping any sub-second part.

    Flooring to whole seconds meant a play stored at ...565.7 exported as
    ...565 and re-imported 0.7s away from the original - which
    UNIQUE (username, track_id, played_at) does not catch, so the round trip
    ADDED a row instead of being a no-op.

    The fractional part is emitted only when there is one, so the ordinary
    whole-second case stays byte-identical to what Spotify itself writes and
    third-party consumers of this file see no change."""
    moment = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if moment.microsecond:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


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
            _csvSafeCell(entry.get("name") or ""),
            _csvSafeCell(", ".join(a.get("name", "") for a in artists)),
            _csvSafeCell(album.get("name") or "" if album else ""),
            entry["timePlayed"],
            f"spotify:track:{entry['id']}",
            _csvSafeCell(entry.get("playedFrom") or ""),
        ])
        if buffer.tell() >= 64 * 1024:   #< flush in ~64KB chunks instead of per row or all at once
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
    yield buffer.getvalue()


PLAYLIST_CSV_COLUMNS = ("Spotify URI", "Track Name", "Artist Name", "Album Name", "ISRC", "Spotify URL")


def generatePlaylistCsv(tracks: list[dict]):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(PLAYLIST_CSV_COLUMNS)
    for track in tracks:
        artists = track.get("artists") or []
        album = track.get("album") or {}
        artist_names = ", ".join(a.get("name", "") for a in artists)
        album_name = album.get("name", "") if isinstance(album, dict) else ""
        spotify_uri = f"spotify:track:{track['id']}"
        spotify_url = track.get("url") or f"https://open.spotify.com/track/{track['id']}"
        isrc = track.get("isrc") or ""
        writer.writerow([
            spotify_uri,
            _csvSafeCell(track.get("name", "")),
            _csvSafeCell(artist_names),
            _csvSafeCell(album_name),
            isrc,
            spotify_url,
        ])
        if buffer.tell() >= 64 * 1024:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
    yield buffer.getvalue()


def _m3uSafeText(value: str) -> str:
    """M3U is line-oriented: a CR/LF inside a track or artist name would end the
    #EXTINF line early and let the rest be read as further playlist entries
    (an arbitrary extra URI). The CSV and XSPF exports escape their formats'
    metacharacters; this is the M3U equivalent."""
    return (value or "").replace("\r", " ").replace("\n", " ")


def generatePlaylistM3u(tracks: list[dict]):
    yield "#EXTM3U\n"
    for track in tracks:
        artists = track.get("artists") or []
        artist_names = _m3uSafeText(", ".join(a.get("name", "") for a in artists))
        title = _m3uSafeText(track.get("name", ""))
        spotify_uri = f"spotify:track:{track['id']}"
        yield f"#EXTINF:-1,{artist_names} - {title}\n{spotify_uri}\n"


def generatePlaylistXspf(tracks: list[dict], title: str = "Spotify Tracker Playlist"):
    import xml.sax.saxutils as xml_escape
    yield '<?xml version="1.0" encoding="UTF-8"?>\n'
    yield '<playlist version="1" xmlns="http://xspf.org/ns/0/">\n'
    yield f'  <title>{xml_escape.escape(title)}</title>\n'
    yield '  <trackList>\n'
    for track in tracks:
        artists = track.get("artists") or []
        artist_names = ", ".join(a.get("name", "") for a in artists)
        track_title = track.get("name", "")
        album = track.get("album") or {}
        album_name = album.get("name", "") if isinstance(album, dict) else ""
        spotify_uri = f"spotify:track:{track['id']}"
        yield '    <track>\n'
        yield f'      <location>{xml_escape.escape(spotify_uri)}</location>\n'
        yield f'      <title>{xml_escape.escape(track_title)}</title>\n'
        if artist_names:
            yield f'      <creator>{xml_escape.escape(artist_names)}</creator>\n'
        if album_name:
            yield f'      <album>{xml_escape.escape(album_name)}</album>\n'
        yield '    </track>\n'
    yield '  </trackList>\n'
    yield '</playlist>\n'
