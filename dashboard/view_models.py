from __future__ import annotations

from flask import g
from Database.utils import convertToDatetime, dateToString, formatDuration, msToString
from config import TRACK_CARD_GENRE_LIMIT


class ViewModelMixin:
    """Per-item view-model builders: text embedders for songs/albums/artists, genre attachment, and change-text formatting."""

    def _getPercentPlayedText(self, item, sortBy, totalPlays, totalMs):
        if sortBy == "plays":
            percent = round((item.get("plays", 0) / totalPlays * 100), 1) if totalPlays else 0
            return f"{percent}% of all plays"
        elif sortBy == "totalTimeListened":
            percent =  round((item.get("totalTimeListened", 0) / totalMs * 100), 1) if totalMs else 0
            return f"{percent}% of all time played"
        else:
            return ""

    def _embedSongTextElements(self, song) -> dict:
        if "playedAt" in song:   #< some tracks just dont have it (top tracks)
            db = g.get("db", None)
            tz = db.tz if db else None
            playedAt = convertToDatetime(song["playedAt"], tz=tz)
            song["playedAtText"] = playedAt.strftime("%d %b %Y, %H:%M")
            song["timePlayedText"] = msToString(song["timePlayed"])

        song["contextName"] = None
        if "playedFrom" in song:
            db = g.get("db", None)
            if db:
                song["contextName"] = db.playlistName(song["playedFrom"])

        artistsText = ", ".join(a.get("name", "") for a in song["artists"])
        album = song.get("album")   #< can be None - see Repository._songRowToDict()'s LEFT JOIN fallback
        # releaseDate 0/None is the app-wide "unknown" sentinel (synthetic
        # tracks, albums the metadata backfiller hasn't reached yet - see
        # Repository.upsertTrack/_createSyntheticTrack) - dateToString would
        # otherwise render it as the Unix epoch date instead of blank.
        releaseDateText = dateToString(album["releaseDate"]) if album and album.get("releaseDate") else ""
        song["releaseDateText"] = releaseDateText
        song["artistsText"] = artistsText
        song["durationText"] = formatDuration(song["duration"])
        if album:
            album["releaseDateText"] = releaseDateText
        return song

    def _embedTopSongTextElements(self, song, sortBy=None, totalPlays=0, totalMs=0) -> dict:
        song["totalTimeListenedText"] = msToString(song.get("totalTimeListened", 0))
        db = g.get("db", None)
        tz = db.tz if db else None
        song["firstListenedText"] = convertToDatetime(song.get("firstListenedAt", 0), tz=tz).strftime("%b %d, %Y")
        song["sortPercentText"] = self._getPercentPlayedText(song, sortBy, totalPlays, totalMs)
        return song

    def _embedAlbumTextElements(self, album, sortBy=None, totalPlays=0, totalMs=0) -> dict:
        album["totalTimeListenedText"] = msToString(album.get("totalTimeListened", 0))
        db = g.get("db", None)
        tz = db.tz if db else None
        album["firstListenedText"] = convertToDatetime(album.get("firstListenedAt", 0), tz=tz).strftime("%b %d, %Y")
        album["sortPercentText"] = self._getPercentPlayedText(album, sortBy, totalPlays, totalMs)
        # See _embedSongTextElements()'s comment: releaseDate 0/None means unknown.
        releaseDate = album.get("releaseDate")
        album["releaseDateText"] = dateToString(releaseDate) if releaseDate else ""
        album["artistsText"] = ", ".join(a.get("name", "") for a in album.get("artists", []))
        return album

    def _embedAlbumsTextElements(self, albums, sortBy=None, totalPlays=0, totalMs=0) -> list[dict]:
        return [self._embedAlbumTextElements(album, sortBy, totalPlays, totalMs) for album in albums]

    def _embedArtistTextElement(self, artist, sortBy=None, totalPlays=0, totalMs=0) -> dict:
        artist["totalTimeListenedText"] = msToString(artist.get("totalTimeListened", 0))
        db = g.get("db", None)
        tz = db.tz if db else None
        artist["firstListenedText"] = convertToDatetime(artist.get("firstListenedAt", 0), tz=tz).strftime("%b %d, %Y")
        artist["sortPercentText"] = self._getPercentPlayedText(artist, sortBy, totalPlays, totalMs)
        return artist

    def _embedSongsTextElements(self, songs) -> list[dict]:
        return [self._embedSongTextElements(song) for song in songs]

    def _embedTopSongsTextElements(self, songs, sortBy=None, totalPlays=0, totalMs=0) -> list[dict]:
        return [self._embedTopSongTextElements(song, sortBy, totalPlays, totalMs) for song in songs]

    def _embedArtistsTextElements(self, songs, sortBy=None, totalPlays=0, totalMs=0) -> list[dict]:
        return [self._embedArtistTextElement(song, sortBy, totalPlays, totalMs) for song in songs]

    def _attachGenres(self, db, items: list[dict], kind: str) -> list[dict]:
        """Sets item['genres'] (a list of genre name strings, [] when none,
        capped to TRACK_CARD_GENRE_LIMIT) for _track_card.html's genre badge
        - one indexed per-item lookup per item, cheap enough against the
        local SQLite file that no batch query is warranted (see
        resolveGenresForTrack/Album/Artist's degrade-to-[] contract, which
        keeps this safe against stubbed test dbs too). Truncated here rather
        than in the template so every caller (including detail pages, which
        wrap a single item) gets the same cap without threading a constant
        through every render_template() call.

        These per-item badges bypass the charts/wrapped/compare coverage-
        unlock gate by design (they show whatever's known regardless of
        aggregate confidence) - but the admin's instance-wide kill switch
        still applies: disabled means no genre lookups at all, matching every
        other genre surface."""
        if not self.repo.isLastfmGenreBackfillEnabled():
            for item in items:
                item["genres"] = []
            return items
        resolver = self._GENRE_RESOLVERS[kind]
        for item in items:
            item["genres"] = resolver(db, item["id"])[:TRACK_CARD_GENRE_LIMIT] if item.get("id") else []
        return items

    def _getChangeText(self, currentValue, previousValue):
        if previousValue is None or previousValue == 0:
            if currentValue == 0:
                return None, ""
            return f"New this period", "change-positive"

        change = ((currentValue - previousValue) / previousValue) * 100
        if round(change, 1) == 0:
            return "No change from the previous period", ""

        formatted = f"{abs(round(change, 1))}% {'more' if change > 0 else 'less'} than the previous period"
        cssClass = "change-positive" if change > 0 else "change-negative"
        return formatted, cssClass
