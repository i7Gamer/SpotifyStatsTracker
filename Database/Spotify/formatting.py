# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Written clean-room from two shapes (dependencyRewritePlan.md Phase 1.3): the
# pathfinder GraphQL wire format on one side, and what this codebase's own
# consumers read on the other (Database/Formatters/spotifyClient.py's
# Client.formatTrack, the importers, the listener). Deliberately NOT derived
# from spotipyFree's Formatter - the field mapping is an interop fact, the code
# performing it is not.

"""Maps Spotify's pathfinder GraphQL responses onto the spotipy-style dicts the
rest of this codebase consumes.

The output contract lives in tests/test_spotify_client_contract.py and is
derived from the consumers, so the important reads are:

  Client.formatTrack hard-subscripts track["id"] and
  track["external_urls"]["spotify"] - those two keys are ALWAYS present on
  every track this module emits, including degraded ones. Everything else is
  read via .get() and may be absent when Spotify didn't say.
"""

OPEN_SPOTIFY_BASE = "https://open.spotify.com"


def idFromUri(uri):
    """The bare id out of a spotify:<kind>:<id> URI ("" for anything else -
    a missing id must not masquerade as a real one)."""
    if not isinstance(uri, str) or not uri.startswith("spotify:"):
        return ""
    return uri.rsplit(":", 1)[-1]


def openSpotifyUrl(kind: str, entityId: str) -> str:
    """The public web-player URL for an entity - the app's "Open in Spotify"
    links. Empty for an empty id: only real ids get real links (fabricated ids
    carry an empty url by convention, see spotifyClient._formatArtists)."""
    return f"{OPEN_SPOTIFY_BASE}/{kind}/{entityId}" if entityId else ""


def formatContext(contextUri):
    """The play-context dict Client.formatTrack reads .get("uri") from, or
    None when the source of a play is unknown - which is not an error, just an
    unattributed play."""
    return {"uri": contextUri} if contextUri else None


def _formatArtistItems(items) -> list:
    """Artist references ({uri, profile.name} on the wire) to the shape
    Client._formatArtists consumes: name, id, external_urls.spotify."""
    artists = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        artistId = idFromUri(item.get("uri"))
        artists.append({
            "name": (item.get("profile") or {}).get("name", ""),
            "id": artistId,
            "external_urls": {"spotify": openSpotifyUrl("artist", artistId)},
        })
    return artists


def _formatAlbumOfTrack(albumOfTrack: dict) -> dict:
    """The album block, spotipy-shaped. images[] is sorted LARGEST first (see
    _sortedImages) - trusting wire order would ship 64px covers whenever
    Spotify happens to list small-to-large."""
    albumId = idFromUri(albumOfTrack.get("uri"))
    images = _sortedImages((albumOfTrack.get("coverArt") or {}).get("sources"))

    album = {
        "name": albumOfTrack.get("name", ""),
        "id": albumId,
        "external_urls": {"spotify": openSpotifyUrl("album", albumId)},
        "images": images,
        "total_tracks": ((albumOfTrack.get("tracks") or {}).get("totalCount")) or 0,
    }
    releaseDate = (albumOfTrack.get("date") or {}).get("isoString")
    if releaseDate:
        # Left absent otherwise: Client._formatAlbum's own "NA" default maps a
        # missing date to the epoch, and inventing one here would claim a fact.
        # Date-only (the "T00:00:00Z" tail dropped): convertToDatetime reads a
        # bare date as instance-zone midnight, so the full instant would render
        # a day earlier anywhere west of UTC - and the Web-API backfill path
        # stores Spotify's own YYYY-MM-DD, so the two sources must agree.
        album["release_date"] = str(releaseDate).split("T")[0]
    return album


def _isExplicit(trackData: dict) -> bool:
    return ((trackData.get("contentRating") or {}).get("label")) == "EXPLICIT"


def _formatTrackCommon(trackData: dict, artists: list) -> dict:
    trackId = idFromUri(trackData.get("uri"))
    return {
        "name": trackData.get("name", ""),
        "id": trackId,
        "external_urls": {"spotify": openSpotifyUrl("track", trackId)},
        "duration_ms": ((trackData.get("duration") or {}).get("totalMilliseconds")) or 0,
        "artists": artists,
        "album": _formatAlbumOfTrack(trackData.get("albumOfTrack") or {}),
        "explicit": _isExplicit(trackData),
        # pathfinder does not expose ISRCs; the key still exists so consumers
        # can .get() a string instead of branching on presence.
        "external_ids": {"isrc": ""},
        "disc_number": trackData.get("discNumber", 0),
        "track_number": trackData.get("trackNumber", 0),
        # Carried through untouched so downstream can record WHY a track is
        # unplayable (e.g. COUNTRY_RESTRICTED) - see spotifyClient.formatTrack's
        # availability_reason.
        "playability": trackData.get("playability"),
    }


def formatTrackUnion(trackUnion: dict) -> dict:
    """A getTrack trackUnion to the spotipy track shape. getTrack splits the
    artist credit into firstArtist/otherArtists; order is preserved as
    first-then-others because the first credit is the primary artist."""
    artists = _formatArtistItems(
        ((trackUnion.get("firstArtist") or {}).get("items") or [])
        + ((trackUnion.get("otherArtists") or {}).get("items") or []))
    return _formatTrackCommon(trackUnion, artists)


def formatSearchTrackData(trackData: dict) -> dict:
    """A searchDesktop result's item.data to the same spotipy track shape.
    Search results carry one flat artists collection instead of getTrack's
    first/other split - that wire difference is absorbed here so callers see
    one shape."""
    artists = _formatArtistItems((trackData.get("artists") or {}).get("items") or [])
    return _formatTrackCommon(trackData, artists)


def _sortedImages(sources) -> list:
    """coverArt/avatarImage sources to spotipy-style images, LARGEST first -
    consumers take images[0].url as THE artwork, and wire order is not a
    documented promise.

    `s.get("width", 0)` alone is not enough: that default only covers an
    ABSENT key, and a pathfinder payload can carry "width": null (key
    present, value None; no recorded fixture shows it, but the API is
    undocumented and the sibling reads guard for it) - which sorts against
    an int and raises TypeError.
    The trailing `or 0` catches the present-but-null case too, matching the
    module's other numeric reads (see :76, :100, :166, :178)."""
    return sorted(
        ({"url": s.get("url", ""), "width": s.get("width") or 0, "height": s.get("height") or 0}
         for s in (sources or []) if isinstance(s, dict)),
        key=lambda img: img["width"], reverse=True)


def formatAlbumUnion(albumUnion: dict) -> dict:
    """A getAlbum albumUnion. The listener reads .get("name"); the metadata
    backfiller reads release_date, total_tracks and tracks.items[]'s
    {id, name, duration_ms} - the album response is the only duration source
    for tracks whose own lookup came back blanked."""
    albumId = idFromUri(albumUnion.get("uri"))
    tracksV2 = albumUnion.get("tracksV2") or {}

    trackItems = []
    for item in (tracksV2.get("items") or []):
        if not isinstance(item, dict):
            continue
        # Entries arrive wrapped in {"track": ...}; tolerate a bare track dict
        # too so a wrapper removal upstream degrades to "still works".
        trackData = item.get("track") if isinstance(item.get("track"), dict) else item
        trackId = idFromUri(trackData.get("uri"))
        if not trackId:
            continue
        trackItems.append({
            "id": trackId,
            "name": trackData.get("name", ""),
            "duration_ms": ((trackData.get("duration") or {}).get("totalMilliseconds")) or 0,
            # The whole reason the backfiller queues albums with artistless
            # tracks: this payload is where the repair links come from
            # (_normalizeBackfillArtists reads id/name/external_urls).
            "artists": _formatArtistItems((trackData.get("artists") or {}).get("items")),
        })

    album = {
        "name": albumUnion.get("name", ""),
        "id": albumId,
        "external_urls": {"spotify": openSpotifyUrl("album", albumId)},
        "images": _sortedImages((albumUnion.get("coverArt") or {}).get("sources")),
        "total_tracks": tracksV2.get("totalCount") or 0,
        "tracks": {"items": trackItems},
    }
    releaseDate = (albumUnion.get("date") or {}).get("isoString")
    if releaseDate:
        # Absent otherwise: the backfiller maps a missing date to 0.0 itself;
        # inventing one here would claim a fact Spotify didn't state.
        # Date-only, for the same two reasons as _formatAlbumOfTrack: a full
        # instant reads a day earlier west of UTC, and the Web-API path
        # stores YYYY-MM-DD.
        album["release_date"] = str(releaseDate).split("T")[0]
    return album


def formatArtistUnion(artistUnion: dict) -> dict:
    """A queryArtistOverview artistUnion. media_fetch's lazy image fallback
    reads .get("images")[0]["url"]; an artist with no avatar yields an empty
    list (= "no artwork available"), never a KeyError."""
    artistId = idFromUri(artistUnion.get("uri"))
    sources = (((artistUnion.get("visuals") or {}).get("avatarImage")) or {}).get("sources")
    return {
        "name": (artistUnion.get("profile") or {}).get("name", ""),
        "id": artistId,
        "external_urls": {"spotify": openSpotifyUrl("artist", artistId)},
        "images": _sortedImages(sources),
    }


def formatPlaylistV2(playlistV2: dict) -> dict:
    """A fetchPlaylist playlistV2. Same single consumer read as albums."""
    playlistId = idFromUri(playlistV2.get("uri"))
    return {
        "name": playlistV2.get("name", ""),
        "id": playlistId,
        "external_urls": {"spotify": openSpotifyUrl("playlist", playlistId)},
    }


def formatProfile(profileResponse: dict) -> dict:
    """The account-settings profile to the {id, email} pair the listener's
    contamination check reads. Some account types carry no username, and the
    listener's own comments record that Spotify then returns the email AS the
    id - so id falls back to email rather than to None."""
    profile = (profileResponse or {}).get("profile") or {}
    email = profile.get("email")
    return {
        "id": profile.get("username") or email,
        "email": email,
    }
