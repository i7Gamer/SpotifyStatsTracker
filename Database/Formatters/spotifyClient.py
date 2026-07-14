from Database.utils import timeToInt, convertToDatetime

class Client:
    @staticmethod
    def _formatArtists(albumRaw):
        artists = []

        for artist in (albumRaw.get("artists") or []):
            artists.append(
                {
                    "name": artist.get("name", ""),
                    "url": artist.get("external_urls", {}).get("spotify", "https://open.spotify.com/artist/6FXMGgJwohJLUSr5nVlf9X"),
                    "imageUrl": "",
                    "imageId": artist.get("id", 0),
                    "id": artist.get("id", "6FXMGgJwohJLUSr5nVlf9X"),
                }
            )

        return artists

    @staticmethod
    def _formatAlbum(albumRaw):
        images = albumRaw.get("images") or []
        firstImage = images[0] if images else {}

        return {
            "name": albumRaw.get("name", "Unknown album"),
            "url": albumRaw.get("external_urls", {}).get("spotify", "https://open.spotify.com/album/49MNmJhZQewjt06rpwp6QR"),
            "id": albumRaw.get("id", 0),
            "imageId": albumRaw.get("id", 0),
            "imageUrl": firstImage.get("url", ""),
            "totalTracks": albumRaw.get("total_tracks", 0),
            "releaseDate": convertToDatetime(albumRaw.get("release_date", "NA")).timestamp(),
        }
    
    @staticmethod
    def embedPlayInfo(track, timestamp, timePlayed):
        playedAtTimestamp = timeToInt(timestamp)

        track["playedAt"] = playedAtTimestamp
        if track.get("duration", 0) > 0:
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