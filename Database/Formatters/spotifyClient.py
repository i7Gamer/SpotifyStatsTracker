from Database.utils import timeToInt, convertToDatetime

class Client:
    @staticmethod
    def _formatArtists(albumRaw):
        artists = []

        for artist in (albumRaw.get("artists") or []):
            imageUrl = "https://i.scdn.co/image/52c2a824e84f4e8adf0b12418f9f8306b4b5b77a"
            if ("images" in artist) and (len(artist["images"]) > 0):
                imageUrl = artist["images"][0]["url"]
            artists.append(
                {
                    "name": artist.get("name", ""),
                    "url": artist.get("external_urls", {}).get("spotify", "https://open.spotify.com/artist/6FXMGgJwohJLUSr5nVlf9X"),
                    "imageUrl": imageUrl,
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
        track["timePlayed"] = timePlayed
        return track
    
    @staticmethod
    def formatTrack(track, timestamp=-1, msPlayed=-1):
        track = track or {}
        album = track.get("album") or {}

        images = album.get("images") or []
        firstImage = images[0] if images else {}

        duration = track.get("duration_ms") or 0

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
        return Client.embedPlayInfo(track, timestamp, msPlayed)