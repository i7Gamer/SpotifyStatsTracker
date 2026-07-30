# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import hashlib

from Database.utils import convertToDatetime, timeToInt
from Database.db import UNKNOWN_TRACK_NAME

# Prefix for ids fabricated when a response carries none, matching the
# importer's synthetic-track convention (see StreamingHistoryImporter).
SYNTHETIC_ID_PREFIX = "synth_"


def _fallbackId(kind: str, name: str) -> str:
    """A stable, obviously-fabricated id for an entity a response returned
    without one.

    The previous fallbacks were a real artist id and a real album id (plus a
    literal 0 for imageId), so every id-less entity of a kind collapsed into
    ONE catalog row - merging unrelated artists together - and linked out to a
    stranger's Spotify page. Deriving from the name keeps distinct names
    distinct, and the synth_ prefix keeps them recognizable as placeholders
    the metadata backfiller can replace.

    usedforsecurity=False: this is a surrogate key, not a security primitive.
    The flag leaves the digest untouched (the ids below are already persisted)
    and keeps the call working on FIPS-mode hosts, matching the importer's md5
    fallbacks in StreamingHistoryImporter."""
    digest = hashlib.sha1(
        f"{kind}:{name}".encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return f"{SYNTHETIC_ID_PREFIX}{kind}_{digest}"


def _primaryImageUrl(entityRaw) -> str:
    """The first (largest, by the client's contract) image url, "" when the
    entity carries no artwork at all."""
    images = entityRaw.get("images") or []
    return images[0].get("url", "") if images else ""


def _formatArtistRef(artistRaw) -> dict:
    name = artistRaw.get("name", "")
    realId = artistRaw.get("id")
    artistId = realId or _fallbackId("artist", name)
    # No url for a fabricated id: an "Open in Spotify" link built from one
    # would point at the wrong entity (the app's convention is that fabricated
    # ids carry an empty url).
    url = (artistRaw.get("external_urls") or {}).get("spotify", "") if realId else ""
    return {"name": name, "url": url, "imageUrl": "", "imageId": artistId, "id": artistId}


def _formatArtists(entityRaw) -> list:
    return [Client._formatArtistRef(artist) for artist in (entityRaw.get("artists") or [])]


def _formatAlbum(albumRaw) -> dict:
    name = albumRaw.get("name", "Unknown album")
    realId = albumRaw.get("id")
    albumId = realId or _fallbackId("album", name)
    return {
        "id": albumId,
        "name": name,
        "url": (albumRaw.get("external_urls") or {}).get("spotify", "") if realId else "",
        "imageId": albumId,
        "imageUrl": Client._primaryImageUrl(albumRaw),
        "totalTracks": albumRaw.get("total_tracks", 0),
        "releaseDate": convertToDatetime(albumRaw.get("release_date", "NA")).timestamp(),
    }


def embedPlayInfo(formatted, timestamp, timePlayed, capAtDuration=True):
    if capAtDuration and formatted.get("duration", 0) > 0:
        timePlayed = min(timePlayed, formatted["duration"])   #< the feed sometimes reports absurdly large values
    formatted["playedAt"] = timeToInt(timestamp)
    formatted["timePlayed"] = timePlayed
    return formatted


def formatTrack(track, timestamp=-1, msPlayed=-1, context=None, embedPlaybackInfo=True) -> dict:
    raw = track or {}
    albumRaw = raw.get("album") or {}

    # A track credited to nobody inherits its album's credit ([] or x -> x).
    artistCredits = Client._formatArtists(raw) or Client._formatArtists(albumRaw)
    albumOut = Client._formatAlbum(albumRaw)

    # Spotify reports why a track can't be played (e.g. COUNTRY_RESTRICTED,
    # PAYWALL_CONTENT); only recorded when playability explicitly says
    # unplayable - unknown/absent playability means no claim either way.
    playability = raw.get("playability") or {}
    availabilityReason = playability.get("reason") if playability.get("playable") is False else None

    formatted = {
        # The two hard-contract keys first: their absence is a crash, not a
        # blank cell (see tests/test_spotify_client_contract.py).
        "id": raw["id"],
        "url": raw["external_urls"]["spotify"],
        "name": raw.get("name", UNKNOWN_TRACK_NAME),
        "artists": artistCredits,
        "album": albumOut,
        "releaseDate": albumOut["releaseDate"],
        "imageUrl": Client._primaryImageUrl(albumRaw),
        "imageId": albumOut["id"],
        "duration": raw.get("duration_ms") or 0,
        "explicit": bool(raw.get("explicit", False)),
        "isrc": (raw.get("external_ids") or {}).get("isrc", ""),
        "discNumber": raw.get("disc_number", 0),
        "trackNumber": raw.get("track_number", 0),
        "availability_reason": availabilityReason,
        # Carried through rather than dropped: a source that already knows
        # its record is a degraded stand-in (fallbackTrackRecord in
        # Database/Spotify/client.py) has no other way to say so, and
        # upsertTrack's guard against a fallback clobbering real metadata
        # keys off exactly this field. None for every ordinary track,
        # which is what upsertTrack already expects when no caller sets it.
        "created_reason": raw.get("created_reason"),
    }

    if not embedPlaybackInfo:
        return formatted

    playedFrom = None
    if context:
        # A context without a usable uri is not an error - the play still counts,
        # it just has no known source. Returning None here would crash callers
        # (e.g. the listener callback) that expect a track dict.
        uri = context.get("uri") or ""
        uri = uri.removeprefix("spotify:").removeprefix("internal:recs:")
        if uri.startswith("album") or uri.startswith("playlist"):
            playedFrom = uri
    formatted["playedFrom"] = playedFrom

    return Client.embedPlayInfo(formatted, timestamp, msPlayed)


class Client:   #< kept as a class purely for the call sites' Client.formatTrack spelling
    """Compatibility namespace - every call site (and one test's patch target)
    says Client.formatTrack / Client.embedPlayInfo, so the module functions
    above are republished here. Cross-calls between them also go through this
    class, which is what keeps patch("...Client.embedPlayInfo") effective
    inside formatTrack."""
    _primaryImageUrl = staticmethod(_primaryImageUrl)
    _formatArtistRef = staticmethod(_formatArtistRef)
    _formatArtists = staticmethod(_formatArtists)
    _formatAlbum = staticmethod(_formatAlbum)
    embedPlayInfo = staticmethod(embedPlayInfo)
    formatTrack = staticmethod(formatTrack)
