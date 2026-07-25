import hashlib

from Database.utils import timeToInt, convertToDatetime

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
    the metadata backfiller can replace."""
    digest = hashlib.sha1(f"{kind}:{name}".encode("utf-8")).hexdigest()[:16]
    return f"{SYNTHETIC_ID_PREFIX}{kind}_{digest}"


class Client:
    @staticmethod
    def _formatArtists(albumRaw):
        artists = []

        for artist in (albumRaw.get("artists") or []):
            name = artist.get("name", "")
            artistId = artist.get("id") or _fallbackId("artist", name)
            artists.append(
                {
                    "name": name,
                    # No url for a fabricated id: an "Open in Spotify" link
                    # built from one would point at the wrong entity (the app's
                    # convention is that fabricated ids carry an empty url).
                    "url": artist.get("external_urls", {}).get("spotify", "") if artist.get("id") else "",
                    "imageUrl": "",
                    "imageId": artistId,
                    "id": artistId,
                }
            )

        return artists

    @staticmethod
    def _formatAlbum(albumRaw):
        images = albumRaw.get("images") or []
        firstImage = images[0] if images else {}
        name = albumRaw.get("name", "Unknown album")
        albumId = albumRaw.get("id") or _fallbackId("album", name)

        return {
            "name": name,
            "url": albumRaw.get("external_urls", {}).get("spotify", "") if albumRaw.get("id") else "",
            "id": albumId,
            "imageId": albumId,
            "imageUrl": firstImage.get("url", ""),
            "totalTracks": albumRaw.get("total_tracks", 0),
            "releaseDate": convertToDatetime(albumRaw.get("release_date", "NA")).timestamp(),
        }
    
    @staticmethod
    def embedPlayInfo(track, timestamp, timePlayed, capAtDuration=True):
        playedAtTimestamp = timeToInt(timestamp)

        track["playedAt"] = playedAtTimestamp
        if capAtDuration and track.get("duration", 0) > 0:
            track["timePlayed"] = min(timePlayed, track["duration"])   #< sometimes spotipyFree returns extremely large (wrong) values
        else:
            track["timePlayed"] = timePlayed
        return track
    
    @staticmethod
    def formatTrack(track, timestamp=-1, msPlayed=-1, context=None, embedPlaybackInfo=True):
        track = track or {}
        album = track.get("album") or {}

        images = album.get("images") or []
        firstImage = images[0] if images else {}

        duration = track.get("duration_ms") or 0

        artists = Client._formatArtists(track)
        if not artists:
            artists = Client._formatArtists(album)
        album = Client._formatAlbum(album)

        # Spotify reports why a track can't be played (e.g. COUNTRY_RESTRICTED,
        # PAYWALL_CONTENT); only recorded when playability explicitly says
        # unplayable - unknown/absent playability means no claim either way.
        playability = track.get("playability") or {}
        availabilityReason = playability.get("reason") if playability.get("playable") is False else None

        track = {
            "name": track.get("name", "Unknown Track"),
            "releaseDate": album["releaseDate"],
            "id": track["id"],
            "url": track["external_urls"]["spotify"],
            "artists": artists,
            "album": album,
            "imageUrl": firstImage.get("url", ""),
            "imageId": album["id"],
            "duration": duration,
            "explicit": bool(track.get("explicit", False)),
            "isrc": track.get("external_ids", {}).get("isrc", ""),
            "discNumber": track.get("disc_number", 0),
            "trackNumber": track.get("track_number", 0),
            "availability_reason": availabilityReason,
            # Carried through rather than dropped: a source that already knows
            # its record is a degraded stand-in (patches._fallbackTrackRecord)
            # has no other way to say so, and upsertTrack's guard against a
            # fallback clobbering real metadata keys off exactly this field.
            # None for every ordinary track, which is what upsertTrack already
            # expects when no caller sets it.
            "created_reason": track.get("created_reason"),
        }

        if not embedPlaybackInfo:
            return track

        playedFrom = None
        if context:
            # A context without a usable uri is not an error - the play still counts,
            # it just has no known source. Returning None here would crash callers
            # (e.g. the listener callback) that expect a track dict.
            uri = context.get("uri") or ""
            uri = uri.removeprefix("spotify:").removeprefix("internal:recs:")
            if uri.startswith("album") or uri.startswith("playlist"):
                playedFrom = uri
        track["playedFrom"] = playedFrom

        return Client.embedPlayInfo(track, timestamp, msPlayed)