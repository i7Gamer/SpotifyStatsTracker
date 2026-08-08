# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

# Imported for the type hints below. Everything used at RUNTIME still goes
# through _dbmod, so the suite's patch("Database.database.X") targets are
# unaffected - these names were simply never defined here, leaving the hints
# unresolvable for tooling and for typing.get_type_hints(). All leaf modules
# (stdlib, or Database/lastfm.py, which imports nothing of ours), so a real
# import costs nothing and cannot cycle.
import threading

# Module-global names (LastfmClient, requests, Importer, logger, time, Path, ...)
# are reached through the database module, so the suite's
# patch("Database.database.X") targets keep working here. Late-bound rather than
# imported: database.py imports this file's mixin, so importing it back by name
# made the cycle break whichever module was imported first (see Database/dbmodule.py).
from Database.dbmodule import dbmod as _dbmod
#< a direct import, unlike _dbmod above: Database.utils imports nothing but the
#  standard library, so it cannot take part in the cycle _dbmod exists to break
from Database.utils import flaskDebugEnabled


# The Web API's GET /v1/albums hard cap on ids per request; also bounds the
# cookie-client fallback's per-cycle batch so both paths pace identically.
SPOTIFY_BULK_ALBUM_LIMIT = 20

# The Web API's GET /v1/tracks hard cap on ids per request. Higher than the
# album cap because it is a different endpoint, not because ISRCs are cheaper.
SPOTIFY_BULK_TRACK_LIMIT = 50

# How much of a failed response's body reaches the log. A Spotify error object
# is ~90 characters; this is room for several, and a bound for everything else.
ERROR_BODY_LOG_LIMIT = 500


def _responseDetail(resp) -> str:
    """`status <code>: <body>` for a log line - on one line, and bounded.

    The status alone is not a diagnosis. Both endpoints here take no scope at
    all, so a 403 on them is not "you asked for too much": Spotify answers
    "Insufficient client scope", which is what tells you the token is being
    rejected wholesale rather than the request being malformed. Two unrelated
    problems behind one number, and the number was all the log carried - the
    live instance's 403 could only be read at all by digging out a body the
    LISTENER had logged, on a different endpoint, weeks earlier.

    Collapsed onto one line because Spotify pretty-prints its error objects over
    five, and a log this size is read through grep, where a five-line entry
    means the message is invisible unless you already knew to ask for context.
    Bounded because a proxy or a captive portal in front of the API answers with
    a page rather than an object, and a log line is not the place to find that
    out. str() rather than a bare read: a response is not always a real one."""
    body = " ".join(str(getattr(resp, "text", "") or "").split())
    if len(body) > ERROR_BODY_LOG_LIMIT:
        body = body[:ERROR_BODY_LOG_LIMIT] + "..."
    return f"status {resp.status_code}: {body}" if body else f"status {resp.status_code}"


class _CycleAccessToken:
    """The one Web-API access token a backfill cycle uses, minted on first ask.

    Both Web-API steps in a cycle want a token, and each used to mint its own -
    so a cycle spent two round-trips on Spotify's token endpoint to be told the
    same thing twice. Minting one eagerly at the top of the cycle fixes that but
    buys a worse problem: a round-trip on every IDLE cycle, and idle is the
    steady state here. The album queue drains for good, and the ISRC queue only
    refills every TRACK_ISRC_RETRY_SECONDS, so a settled instance would spend a
    request every five minutes forever to produce a token neither step reads.

    Lazy keeps both properties at once: at most one refresh per cycle, and none
    at all when there is nothing to spend it on. Callers ask by CALLING it, so a
    plain `lambda: token` substitutes for it anywhere a real one is awkward.

    None is a real answer and is cached like any other: a refresh that failed
    must not be retried by the next caller in the same cycle."""

    __slots__ = ("_mint", "_token", "_minted")

    def __init__(self, mint):
        self._mint = mint
        self._token = None
        self._minted = False

    def __call__(self) -> str | None:
        if not self._minted:
            self._minted = True
            self._token = self._mint()
        return self._token


class MetadataBackfillMixin:
    """The Spotify Web-API metadata backfiller (missing album/track dates, artistless tracks)."""

    def getSpotifyApiWorkerStatus(self) -> dict:
        """Same shape as getLastfmWorkerStatus, for the Spotify API metadata
        backfiller worker thread."""
        return self._workerStatus(
            "backfiller_thread", "spotify_api",
            configured=bool(self.repo.getUserSpotifyCredentials(self.user)))

    def startMetadataBackfiller(self) -> None:
        """Start the background thread to fill in missing album metadata."""
        self._startPeriodicWorker("backfiller_thread", "backfiller_stop_event",
                                   self._metadataBackfillLoop,
                                   f"metadata-backfiller-{self.user}", logPrefix="MetadataBackfiller")

    def stopMetadataBackfiller(self) -> None:
        """Signal and wait for the background backfiller thread to stop."""
        self._stopPeriodicWorker("backfiller_thread", "backfiller_stop_event")

    @staticmethod
    def _normalizeBackfillArtists(artistsRaw: list) -> list[dict]:
        """Repo-shaped artist dicts from an album payload's per-track artist
        list (Web API and the cookie client both expose id/name/external_urls).
        Entries without a real id or name are dropped - fabricating links
        would be worse than leaving the track for the next repair pass."""
        artists = []
        for artist in artistsRaw:
            if not isinstance(artist, dict):
                continue
            artistId = artist.get("id")
            name = artist.get("name")
            if not artistId or not name:
                continue
            url = (artist.get("external_urls") or {}).get("spotify") or \
                f"https://open.spotify.com/artist/{artistId}"
            artists.append({"id": artistId, "name": name, "url": url, "imageId": artistId})
        return artists

    def _backfillTrackIsrcs(self, getAccessToken, stop_event: threading.Event) -> None:
        """Fill in tracks.isrc from GET /v1/tracks, one batch per backfill cycle.

        Web-API only, with no cookie-client fallback - and that is a property of
        the data, not an omission. The pathfinder client cannot expose ISRCs at
        all (Database/Spotify/formatting.py), so an instance whose users have no
        Web-API credentials simply has no ISRC source; attempting the fallback
        would burn a request per track to learn nothing. Nothing is stamped in
        that case either, so the queue is intact the moment credentials arrive.
        `getAccessToken()` answers None exactly then - and also when a refresh
        failed, which wants the same treatment for the same reason.

        Asked for AFTER the queue read and the claim below, and asked for at all
        only when there is a batch to spend it on - see _CycleAccessToken for
        why a cycle with nothing to do must make no network calls.

        The ISRC is what makes "the single and the album track are the same
        recording" answerable without guessing at names: both releases carry the
        issuer's identifier for the master. (A remaster is a new master and gets
        its own ISRC, so this is a precise key, not a fuzzy one.)

        Nothing raises out of here, and that covers the repo calls as much as
        the request: a locked database is the ordinary way one of them fails on
        a live instance. This step runs FIRST in the cycle (see the loop, which
        explains why), so anything escaping is caught by the per-cycle handler
        and costs the album and artist-link repair their whole turn - a DNS blip
        or a busy database stalling unrelated work for five minutes."""
        #< the shutdown check first: a worker being torn down should stop
        #  whether or not this instance has credentials, and it is the one gate
        #  here that is about the process rather than about the data
        if stop_event.is_set():
            return

        import requests

        target_ids = []
        try:
            # The catalog is shared - one tracks table for the whole instance -
            # so every user's backfiller drains THIS queue, and two in flight at
            # once used to request the same 50 ids: three API calls to make one
            # batch of progress, and three writes of the same rows. Claimed
            # process-wide for the length of the request, exactly as the album
            # queue does it (Database._active_backfills, step 4 in the loop).
            #
            #< the pool is read four times wider than the batch, the ratio the
            #  album queue uses: reading exactly one batch would have the second
            #  worker find everything claimed and sit the cycle out, which is
            #  correct but throws away the parallelism three workers are for
            pool = self.repo.getTracksMissingIsrc(self.BACKFILLER_TRACK_QUEUE_SIZE)
            with _dbmod.Database._backfill_lock:
                for track_id in pool:
                    if track_id in _dbmod.Database._active_isrc_backfills:
                        continue
                    target_ids.append(track_id)
                    _dbmod.Database._active_isrc_backfills.add(track_id)
                    if len(target_ids) >= SPOTIFY_BULK_TRACK_LIMIT:
                        break
            if not target_ids:
                return
            access_token = getAccessToken()
            if not access_token:
                return

            resp = requests.get(f"https://api.spotify.com/v1/tracks?ids={','.join(target_ids)}",
                                headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
            if resp.status_code != 200:
                # Not a definitive "no ISRC" - a 429/5xx stamped as attempted would
                # park these tracks for the whole retry window over a transient.
                #
                # Unconditional, unlike the album lookup's twin below: that one
                # falls back to the cookie client and says so, so its status is
                # colour on a story the log already tells. This step has no
                # fallback, which makes the status the ENTIRE story - and behind
                # a FLASK_DEBUG gate a persistent 401 was indistinguishable from
                # "still draining", the queue sitting untouched cycle after
                # cycle with nothing in the log to say why.
                _dbmod.logger.warning("[Backfiller-%s] ISRC lookup returned %s",
                                       self.user, _responseDetail(resp))
                return
            payload = resp.json()

            isrcByTrackId = {}
            for track_raw in payload.get("tracks") or []:
                if not track_raw:
                    continue   #< an id Spotify has no data for; still counts as attempted below
                track_id = track_raw.get("id")
                isrc = (track_raw.get("external_ids") or {}).get("isrc")
                if track_id and isrc:
                    isrcByTrackId[track_id] = isrc

            self.repo.updateTrackIsrcs(isrcByTrackId)
            self.repo.markTracksIsrcAttempted(target_ids)

            if isrcByTrackId:
                _dbmod.logger.info("[Backfiller-%s] Recorded ISRCs for %d/%d track(s)",
                                    self.user, len(isrcByTrackId), len(target_ids))
        except Exception as e:
            #< a timeout, a reset, a 200 that isn't JSON, a 200 whose JSON is a
            #  list, a locked database. Same verdict as a 429: nothing learned,
            #  so nothing stamped, and the ids stay queued
            _dbmod.logger.warning("[Backfiller-%s] ISRC backfill failed: %s", self.user, e)
        finally:
            # In a finally because the failure paths are the ones that need it:
            # a successful batch has stamped isrc_attempted_at and left the queue
            # anyway, while a failure stamps nothing, so ids held after one would
            # be skipped as "already in flight" by every later cycle in this
            # process - a backfill that quietly stops making progress on them.
            try:
                with _dbmod.Database._backfill_lock:
                    _dbmod.Database._active_isrc_backfills.difference_update(target_ids)
            except Exception as cleanupError:
                _dbmod.logger.warning("[Backfiller-%s] Failed to release %d in-flight track ids: %s",
                                       self.user, len(target_ids), cleanupError)

    def _metadataBackfillLoop(self, stop_event: threading.Event | None = None) -> None:
        """Periodically queries Spotify for missing album release dates and tracks.

        `stop_event` is THIS run's private event (see the fresh-event note in
        startMetadataBackfiller) - a later restart can never revive this
        thread."""
        import random
        if stop_event is None:
            stop_event = self.backfiller_stop_event
        try:
            # 1. Random startup offset to prevent multiple user threads from starting at the same moment
            startup_delay = random.randint(self.BACKFILLER_MIN_START_DELAY, self.BACKFILLER_MAX_START_DELAY)
            _dbmod.logger.info("[Backfiller-%s] Starting with initial delay of %d seconds", self.user, startup_delay)
            if stop_event.wait(startup_delay):
                _dbmod.logger.info("[Backfiller-%s] Stopped during startup delay", self.user)
                return

            while not stop_event.is_set():
                target_ids = []
                try:
                    if not self.repo.isSpotifyApiBackfillEnabled():
                        if stop_event.wait(self.BACKFILLER_IDLE_WAIT_SECONDS):
                            break
                        continue

                    # 2. Get Spotify API credentials if configured, and set up
                    # the one access token this cycle's Web-API work shares.
                    # Both steps below want it and each used to refresh its own,
                    # so a cycle spent two round-trips on Spotify's token
                    # endpoint to be told the same thing twice. Minted on first
                    # ask rather than here, so a cycle with nothing to do still
                    # makes no network calls at all - see _CycleAccessToken.
                    #
                    #< all three, like the listener's own gate - see the same
                    #  comment on _fetchArtistImageUrl's copy in media_fetch.py
                    creds = self.getUserSpotifyCredentials()
                    hasWebApiCreds = bool(creds and creds.get("client_id") and creds.get("client_secret")
                                          and creds.get("refresh_token"))

                    def mintAccessToken():
                        if not hasWebApiCreds:
                            return None
                        from Database.Listeners.spotifyListener import _refresh_spotify_access_token
                        return _refresh_spotify_access_token(
                            creds["client_id"], creds["client_secret"], creds["refresh_token"])

                    getAccessToken = _CycleAccessToken(mintAccessToken)

                    # 2b. One ISRC batch per cycle. Deliberately ahead of the
                    # album queue's early-out below: albums run dry long before
                    # tracks do (the album queue only holds rows with MISSING
                    # metadata, while every track in the catalog starts without
                    # an ISRC), so placing this after the `continue` would have
                    # meant the ISRC backfill silently stopped the moment album
                    # metadata was complete - which is the steady state. That
                    # ordering is also why it swallows its own failures.
                    self._backfillTrackIsrcs(getAccessToken, stop_event)

                    # 3. Query up to N missing album IDs. Albums whose tracks
                    # lack artist links piggyback on the same fetch: the album
                    # payload carries per-track artists, repairing tracks that
                    # were saved from degraded payloads without artist data.
                    missing_ids = self.repo.getAlbumsMissingMetadata(limit=self.BACKFILLER_ALBUM_QUEUE_SIZE)
                    if len(missing_ids) < self.BACKFILLER_ALBUM_QUEUE_SIZE:
                        known_ids = set(missing_ids)
                        missing_ids.extend(
                            albumId for albumId in self.repo.getAlbumsWithArtistlessTracks(
                                self.BACKFILLER_ALBUM_QUEUE_SIZE - len(missing_ids))
                            if albumId not in known_ids)
                    if not missing_ids:
                        if stop_event.wait(self.BACKFILLER_IDLE_WAIT_SECONDS):
                            break
                        continue

                    # 4. Process-wide deduplication: filter out already active backfills
                    with _dbmod.Database._backfill_lock:
                        for album_id in missing_ids:
                            if album_id not in _dbmod.Database._active_backfills:
                                target_ids.append(album_id)
                                _dbmod.Database._active_backfills.add(album_id)
                                if len(target_ids) >= SPOTIFY_BULK_ALBUM_LIMIT:
                                    break

                    # 5. If nothing eligible remains, wait and try next iteration
                    if not target_ids:
                        if stop_event.wait(self.BACKFILLER_IDLE_WAIT_SECONDS):
                            break
                        continue

                    # 6. Fetch detailed metadata
                    _dbmod.logger.info("[Backfiller-%s] Fetching metadata for %d albums", self.user, len(target_ids))
                    fetched_albums = []
                    attempted_ids = []  #< albums that got a definitive response (incl. "gone") - rate-limits their next retry
                    use_fallback = True

                    #< the cycle's shared token, minted here only if the ISRC
                    #  step above did not already need it (step 2). Asked for
                    #  behind the early-out above, so a drained album queue
                    #  costs nothing
                    access_token = getAccessToken()
                    if access_token:
                        import requests

                        headers = {"Authorization": f"Bearer {access_token}"}
                        ids_str = ",".join(target_ids)
                        url = f"https://api.spotify.com/v1/albums?ids={ids_str}"
                        resp = requests.get(url, headers=headers, timeout=10)
                        if resp.status_code == 200:
                            albums_data = resp.json().get("albums") or []
                            for album_raw in albums_data:
                                if album_raw:
                                    fetched_albums.append(album_raw)
                            # Null entries are albums Spotify has no data for -
                            # count those as attempted too, or they'd be re-queued
                            # every cycle forever.
                            attempted_ids = list(target_ids)
                            use_fallback = False
                        else:
                            #< still behind the gate: this path degrades to the
                            #  cookie client and logs that it did, so the status
                            #  is colour on a story the log already tells. It
                            #  carries the body for the same reason its twin
                            #  above does - when it IS asked for, the number on
                            #  its own answers nothing
                            if flaskDebugEnabled():
                                _dbmod.logger.warning(
                                    "[Backfiller-%s] Spotify Web API returned %s. Falling back to the cookie client.",
                                    self.user, _responseDetail(resp)
                                )
                    elif hasWebApiCreds:
                        _dbmod.logger.warning("[Backfiller-%s] Failed to refresh access token. Falling back to the cookie client.", self.user)

                    if use_fallback:
                        import Database.Spotify
                        # No cookiesFile on purpose: album() is a public lookup
                        # through spotapi's pooled client and never touches the
                        # login. Constructing with cookies ran a full login()
                        # whose TLSClient atexit-pinned one live curl session
                        # per backfill cycle (every 5 minutes, for every user
                        # without Web-API credentials) - the same leak
                        # _pooledPublicClient was built to close.
                        sp = Database.Spotify.Spotify()
                        for album_id in target_ids:
                            if stop_event.is_set():
                                break
                            try:
                                album_raw = sp.album(album_id)
                                if album_raw:
                                    fetched_albums.append(album_raw)
                                attempted_ids.append(album_id)  #< a clean "no data" reply is definitive; exceptions stay unmarked for a next-cycle retry
                            except Exception as fe:
                                _dbmod.logger.warning("[Backfiller-%s] Cookie client failed for album %s: %s", self.user, album_id, fe)
                            stop_event.wait(1.0)

                        if fetched_albums:
                            _dbmod.logger.info("[Backfiller-%s] Cookie client fetched %d album(s)", self.user, len(fetched_albums))
                        else:
                            _dbmod.logger.warning("[Backfiller-%s] Cookie-client fallback failed to fetch any albums", self.user)

                    from Database.utils import convertToDatetime
                    updated_count = 0
                    for album_raw in fetched_albums:
                        album_id = album_raw.get("id")
                        release_date_str = album_raw.get("release_date")
                        total_tracks = album_raw.get("total_tracks", 0)
                        album_name = album_raw.get("name")

                        if release_date_str == "0000-00-00" or not release_date_str:
                            release_date = 0.0
                        else:
                            try:
                                dt = convertToDatetime(release_date_str)
                                release_date = dt.timestamp() if dt else 0.0
                            except Exception:
                                release_date = 0.0

                        # A blank name isn't data - passing None skips the name update
                        # so a blanked response can't overwrite a name the importer
                        # already filled from the user's export.
                        self.repo.updateAlbumMetadata(album_id, release_date, total_tracks,
                                                      name=album_name if album_name else None)

                        # Update names (and durations, when provided) for the tracks
                        # in this album if returned - the album response is the only
                        # duration source for tracks whose own lookup came back blanked.
                        tracks_data = album_raw.get("tracks", {}).get("items") or []
                        for track_raw in tracks_data:
                            track_id = track_raw.get("id") or track_raw.get("track_id")
                            if not track_id:
                                continue
                            track_name = track_raw.get("name")
                            if track_name:
                                duration_ms = track_raw.get("duration_ms") or 0
                                self.repo.updateTrackName(track_id, track_name,
                                                          duration_ms=duration_ms if duration_ms > 0 else None)
                            # Repair path: link artists for tracks that have none
                            # (addMissingTrackArtists never touches existing links).
                            repair_artists = self._normalizeBackfillArtists(track_raw.get("artists") or [])
                            if repair_artists:
                                self.repo.addMissingTrackArtists(track_id, repair_artists)

                        updated_count += 1

                    if attempted_ids:
                        self.repo.markAlbumsBackfillAttempted(attempted_ids)

                    if updated_count > 0:
                        _dbmod.logger.info(
                            "[Backfiller-%s] Updated metadata for %d album(s)",
                            self.user, updated_count
                        )

                    # 7. Release lock on the processed IDs
                    with _dbmod.Database._backfill_lock:
                        for album_id in target_ids:
                            _dbmod.Database._active_backfills.discard(album_id)

                except Exception as e:
                    self._recordWorkerCycle("spotify_api", success=False, error=_dbmod.parseError(e))
                    _dbmod.logger.error("[Backfiller-%s] Error in metadata backfiller loop: %s", self.user, e)
                    # Cleanup registry if error occurred mid-process
                    try:
                        with _dbmod.Database._backfill_lock:
                            for album_id in target_ids:
                                _dbmod.Database._active_backfills.discard(album_id)
                    except Exception as cleanupError:
                        # Losing this cleanup leaks the ids in _active_backfills
                        # for the life of the process, and every later cycle
                        # skips those albums as "already in flight" - a backfill
                        # that quietly stops making progress on them.
                        _dbmod.logger.warning("[Backfiller-%s] Failed to release %d in-flight album ids: %s",
                                               self.user, len(target_ids), cleanupError)
                else:
                    self._recordWorkerCycle("spotify_api", success=True)

                if stop_event.wait(self.BACKFILLER_IDLE_WAIT_SECONDS):
                    break

        finally:
            _dbmod.logger.info("[Backfiller-%s] Exited gracefully", self.user)
