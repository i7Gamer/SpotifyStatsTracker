# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

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
        - one batched lookup for the whole list (see
        resolveGenresForTracks/Albums/Artists' degrade-to-{} contract, which
        keeps this safe against stubbed test dbs too). It used to be two
        queries per item (the genre rows, plus a fresh read of the
        inherited-genres setting), which an artist detail page paid for every
        song the artist has - hundreds of round trips on one render.
        Truncated here rather than in the template so every caller (including
        detail pages, which wrap a single item) gets the same cap without
        threading a constant through every render_template() call.

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
        genresById = resolver(db, [item["id"] for item in items if item.get("id")])
        for item in items:
            genres = genresById.get(item["id"], []) if item.get("id") else []
            item["genres"] = genres[:TRACK_CARD_GENRE_LIMIT]
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

    @staticmethod
    def _classifyPlay(play: dict, durationMs: int, completePercentThreshold: int) -> dict:
        """Sets playType ("full"/"partial"/"skip"), playTypeLabel and
        percentPlayed on one play, from how much of `durationMs` it reached.

        Shared by the two surfaces that label individual plays - the song
        detail timeline and the /history list - rather than spelled twice. The
        boundary here is the admin's completion-complete percent, which the
        "Full plays only" filter, getCompletionStats and the Forgotten
        Favorite trend all read as well, so a second copy would start
        disagreeing with the filter the moment that setting moved. A row the
        filter keeps must not be labelled one the filter drops.

        A duration of 0 means the track's metadata never arrived: the play
        reads as full, not as a 0% partial, matching FULL_PLAY_PREDICATE's
        `duration_ms <= 0` arm, which keeps those rows for the same reason.

        A skip carries no percentage in its label. It is a skip whether it ran
        one second or four, and a number there invites reading it as a partial
        listen - percentPlayed is still set for callers that want the figure."""
        timePlayed = play.get("timePlayed", 0)
        if play.get("isSkip", False):
            play["playType"] = "skip"
            play["playTypeLabel"] = "Skipped"
            play["percentPlayed"] = round((timePlayed / durationMs * 100), 1) if durationMs > 0 else 0
        elif durationMs > 0:
            pct = (timePlayed / durationMs) * 100
            play["percentPlayed"] = round(pct, 1)
            if pct >= completePercentThreshold:
                play["playType"] = "full"
                play["playTypeLabel"] = "Full Play"
            else:
                play["playType"] = "partial"
                play["playTypeLabel"] = f"Partial • {round(pct)}%"
        else:
            play["playType"] = "full"
            play["playTypeLabel"] = "Full Play"
            play["percentPlayed"] = 100
        return play

    def _attachPlayTypes(self, plays: list[dict], completePercentThreshold: int | None = None) -> list[dict]:
        """playType/playTypeLabel for a play-HISTORY list, where every row is a
        different track and so carries its own duration - the song timeline is
        one track's plays and gets told the duration once.

        Feeds the Partial/Skipped chips on /history's cards
        (templates/_track_card.html). They matter because unticking "Full plays
        only" makes that list the raw log, and the card describes the TRACK: a
        one-second skip and a real listen are otherwise the same row.

        Reads the threshold once for the whole page rather than per row, the
        same reason _attachGenres batches its lookup."""
        if completePercentThreshold is None:
            completePercentThreshold = self.repo.getCompletionCompletePercent()
        for play in plays:
            self._classifyPlay(play, play.get("duration") or 0, completePercentThreshold)
        return plays

    def _enrichSongTimelineEntries(self, plays: list[dict], trackDurationMs: int | None = None,
                                    completePercentThreshold: int | None = None) -> list[dict]:
        """Enriches play entries for the song detail timeline with playType,
        percentPlayed, monthYearHeader, and timePassedText.

        The full-vs-partial cutoff defaults to the admin's instance-wide
        completion-complete percent (getCompletionCompletePercent) - the same
        boundary Top Songs' "Full plays only" filter, getCompletionStats, and
        the Forgotten Favorite trend use - so the timeline's "Full Play" vs
        "Partial" label never diverges from them (it was previously hardcoded
        to 80, which silently disagreed once an admin changed the setting).
        Callers may pass an explicit threshold."""
        from flask import has_app_context, g
        from Database.utils import convertToDatetime, formatTimeGap
        if completePercentThreshold is None:
            completePercentThreshold = self.repo.getCompletionCompletePercent()
        db = g.get("db", None) if has_app_context() else None
        tz = db.tz if db else None

        lastMonthYear = None

        for i, play in enumerate(plays):
            self._classifyPlay(play, play.get("duration") or trackDurationMs or 0,
                               completePercentThreshold)

            played_at_dt = convertToDatetime(play.get("playedAt", 0), tz=tz)
            month_year_str = played_at_dt.strftime("%B %Y")
            if month_year_str != lastMonthYear:
                play["monthYearHeader"] = month_year_str
                lastMonthYear = month_year_str
            else:
                play["monthYearHeader"] = None

            # The gap badge renders directly above this play's card, between it and the
            # previous card in the list, so it must always describe the gap to the
            # previous entry regardless of sort direction (see _play_log.html).
            play["timePassedText"] = None
            if i > 0:
                current_ts = play.get("playedAt", 0)
                previous_ts = plays[i - 1].get("playedAt", 0)
                delta_sec = float(current_ts) - float(previous_ts)
                play["timePassedText"] = formatTimeGap(abs(delta_sec), earlier=(delta_sec < 0))

        return plays

