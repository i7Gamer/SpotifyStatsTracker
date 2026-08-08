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


# How many albums a cycle works through, on the Web API and on the cookie-client
# fallback alike, so both paths pace identically.
#
# These two were SPOTIFY_BULK_ALBUM_LIMIT and SPOTIFY_BULK_TRACK_LIMIT, named
# for the `?ids=` endpoints' hard caps on ids per request. Those endpoints are
# gone (see the note above the pacing constants below), so the numbers no longer
# describe an API limit at all - they are purely our own per-cycle pacing, and
# the old names would have had a reader looking for a bulk request that is not
# there. The values are unchanged: the drain rate was always ids-per-cycle.
ALBUM_BATCH_SIZE = 20

# The same, for tracks. Higher than the album batch because it always was - a
# different endpoint, not because ISRCs are cheaper.
TRACK_BATCH_SIZE = 50

# How much of a failed response's body reaches the log. A Spotify error object
# is ~90 characters; this is room for several, and a bound for everything else.
ERROR_BODY_LOG_LIMIT = 500

# Spotify withdrew the bulk `?ids=` catalog endpoints on 2026-07-31: they answer
# 403 Forbidden for a single id as readily as for fifty, while /v1/<kind>/<id>
# answers 200 (measured against this instance's own credentials, with plain
# requests, so it is Spotify's behaviour and not something here). A batch is
# therefore N requests rather than one.
#
# It costs no wall-clock: pacing was never request-bound. A cycle has always
# been TRACK_BATCH_SIZE ids per worker, so the whole catalog still
# drains in the same ~14 hours across three workers - it is the same 50 tracks,
# asked for 50 times instead of once.
SPOTIFY_REQUEST_PACE_SECONDS = 0.2   #< between per-id requests; ~10s of a 300s cycle

# Give up on a batch after this many failures IN A ROW. With one request per
# batch a systemic failure cost one request per cycle; with fifty it costs
# fifty, from each of three workers, every five minutes, forever - which is
# exactly the shape of the 403 this rewrite was done under. Consecutive rather
# than cumulative: an intermittent 503 among successes is the API being busy,
# not the API being gone.
CONSECUTIVE_FAILURE_ABORT = 5

# Consecutive cycles whose token refresh failed before the account is told its
# Spotify authorization needs redoing. A refresh returns None for a revoked
# grant AND for a DNS blip, and only the first is the user's to fix - the same
# reason the listener waits out SCOPE_ERROR_CONFIRM_THRESHOLD polls before
# prompting. At one cycle per BACKFILLER_IDLE_WAIT_SECONDS this is a quarter of
# an hour of sustained failure.
TOKEN_REFRESH_REAUTH_THRESHOLD = 3

# Spotify's "you are asking too often". Distinguished from the other failures
# because it is the one that says stopping now helps.
SPOTIFY_RATE_LIMIT_STATUS = 429


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

    __slots__ = ("_mint", "_token", "_minted", "noted")

    def __init__(self, mint):
        self._mint = mint
        self._token = None
        self._minted = False
        #< set by _noteTokenHealth, so a cycle is counted once whichever of the
        #  two steps happened to ask for the token first
        self.noted = False

    def __call__(self) -> str | None:
        if not self._minted:
            self._minted = True
            self._token = self._mint()
        return self._token

    @property
    def attempted(self) -> bool:
        """Whether anything in this cycle asked for a token at all. A cycle with
        no work asks for none, and that is not evidence either way."""
        return self._minted


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

    def _noteTokenHealth(self, getAccessToken, hasWebApiCreds: bool) -> None:
        """Count consecutive cycles whose token refresh failed, and ask the
        account to re-authorize once that is clearly not a blip.

        A refresh returning None is the one failure on this path a USER can fix:
        the stored grant is dead, so re-authorizing restores it.

        Deliberately NOT wired to a 403 from the lookups themselves, however
        many times it repeats. That was tested for us on 2026-07-31, when
        Spotify withdrew the bulk `?ids=` catalog endpoints and every call began
        answering 403 Forbidden - with a refresh that succeeded, scopes intact,
        and /v1/me answering 200. Prompting on that would have told all three
        accounts their Spotify authorization had failed, over a platform change
        no login could fix; one of them re-consented anyway and it changed
        nothing. A 403 says "not allowed", not "not you". The 403 that DOES mean
        re-authorization carries "Insufficient client scope" in its body and
        arrives on a SCOPED endpoint - the listener's recently-played poll owns
        that one (see _SCOPE_ERROR), and nothing here asks for a scope at all.

        A cycle that asked for no token is not evidence either way, so it
        neither counts nor clears.

        Sets but never clears. Clearing belongs to a definitive scoped success
        (the listener's on_scope_status_change) or to the user finishing the
        re-auth flow: a token refresh working again says nothing about the scope
        the flag may have been raised for."""
        if getAccessToken.noted or not (hasWebApiCreds and getAccessToken.attempted):
            return
        getAccessToken.noted = True

        if getAccessToken() is not None:
            self._backfillTokenFailures = 0
            return

        self._backfillTokenFailures += 1
        #< exactly at the threshold, not past it: setSpotifyNeedsReauth queues
        #  an email every time it is called with True, so a streak that keeps
        #  running must not keep asking
        if self._backfillTokenFailures == TOKEN_REFRESH_REAUTH_THRESHOLD:
            _dbmod.logger.error(
                "[Backfiller-%s] Spotify token refresh has failed %d cycles running - "
                "the stored authorization looks revoked; prompting for re-authorization",
                self.user, self._backfillTokenFailures)
            self.setSpotifyNeedsReauth(True)

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
                    if len(target_ids) >= TRACK_BATCH_SIZE:
                        break
            if not target_ids:
                return
            access_token = getAccessToken()
            if not access_token:
                return

            headers = {"Authorization": f"Bearer {access_token}"}
            isrcByTrackId = {}
            attempted_ids = []   #< got a DEFINITIVE answer; only these leave the queue
            consecutiveFailures = 0
            #< counted, not derived from len(target_ids) - len(attempted_ids):
            #  once the batch can stop early, the ids after the break were never
            #  ASKED about, and calling them failures reports "50 of 50 failed"
            #  over five real failures - in the line that exists to be believed
            failures = 0
            firstFailure = None

            for track_id in target_ids:
                #< between tracks, not only before the batch: a batch is up to
                #  fifty requests long now, and shutdown has to be able to land
                #  inside one rather than wait it out
                if stop_event.is_set():
                    break

                rateLimited = False
                try:
                    resp = requests.get(f"https://api.spotify.com/v1/tracks/{track_id}",
                                        headers=headers, timeout=10)
                    if resp.status_code == 200:
                        isrc = (resp.json().get("external_ids") or {}).get("isrc")
                        if isrc:
                            isrcByTrackId[track_id] = isrc
                        #< attempted either way: a track Spotify HAS but has no
                        #  ISRC for is answered, and re-asking every cycle
                        #  forever is what the bulk form's null entries meant
                        attempted_ids.append(track_id)
                        consecutiveFailures = 0
                    elif resp.status_code == 404:
                        #< equally definitive, and the bulk form said it with a
                        #  null entry: Spotify has no such track and never will
                        attempted_ids.append(track_id)
                        consecutiveFailures = 0
                    else:
                        # Not a definitive "no ISRC" - a 429/5xx stamped as
                        # attempted would park this track for the whole retry
                        # window over a transient, so it stays queued.
                        if firstFailure is None:
                            firstFailure = _responseDetail(resp)
                        failures += 1
                        consecutiveFailures += 1
                        #< the API asking for less; the rest of the batch waits
                        #  for the next cycle rather than being spent proving it
                        rateLimited = resp.status_code == SPOTIFY_RATE_LIMIT_STATUS
                except Exception as e:
                    #< caught HERE, per track, and not by the batch's handler
                    #  below: one reset connection used to cost the other
                    #  forty-nine their turn, which is the whole reason a batch
                    #  is fifty requests now rather than one
                    if firstFailure is None:
                        firstFailure = f"request failed: {e}"
                    failures += 1
                    consecutiveFailures += 1

                if rateLimited or consecutiveFailures >= CONSECUTIVE_FAILURE_ABORT:
                    break
                stop_event.wait(SPOTIFY_REQUEST_PACE_SECONDS)

            self.repo.updateTrackIsrcs(isrcByTrackId)
            self.repo.markTracksIsrcAttempted(attempted_ids)

            if firstFailure is not None:
                # One line for the batch, not one per track: fifty requests can
                # fail fifty times and the log is read by a person.
                #
                # Unconditional, unlike the album lookup's twin below: that one
                # falls back to the cookie client and says so, so its status is
                # colour on a story the log already tells. This step has no
                # fallback, which makes the status the ENTIRE story - and behind
                # a FLASK_DEBUG gate a persistent 401 was indistinguishable from
                # "still draining", the queue sitting untouched cycle after
                # cycle with nothing in the log to say why.
                _dbmod.logger.warning(
                    "[Backfiller-%s] ISRC lookup failed for %d of %d track(s); first was %s",
                    self.user, failures, len(target_ids), firstFailure)

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
        #< here rather than on the instance: the streak belongs to THIS run, and
        #  a worker restarted after a re-auth should start counting again from
        #  nothing rather than inherit the state that prompted it
        self._backfillTokenFailures = 0
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

                    #< here AND after the album branch's own use below, because
                    #  either step can be the one that asks for the token first
                    #  (this one has work in the steady state; the album queue
                    #  drains) - the token object remembers it was counted, so a
                    #  cycle is counted once either way
                    self._noteTokenHealth(getAccessToken, hasWebApiCreds)

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
                                if len(target_ids) >= ALBUM_BATCH_SIZE:
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
                    self._noteTokenHealth(getAccessToken, hasWebApiCreds)
                    if access_token:
                        import requests

                        # One request per album: the bulk `?ids=` form this used
                        # to call was withdrawn on 2026-07-31 and has answered
                        # 403 Forbidden ever since, which is why every cycle
                        # between then and now degraded to the cookie client.
                        headers = {"Authorization": f"Bearer {access_token}"}
                        consecutiveFailures = 0
                        albumFailures = 0   #< real failures, not "everything unstamped" - see the ISRC step
                        firstFailure = None

                        for album_id in target_ids:
                            if stop_event.is_set():
                                break
                            rateLimited = False
                            try:
                                resp = requests.get(f"https://api.spotify.com/v1/albums/{album_id}",
                                                    headers=headers, timeout=10)
                                if resp.status_code == 200:
                                    fetched_albums.append(resp.json())
                                    attempted_ids.append(album_id)
                                    consecutiveFailures = 0
                                elif resp.status_code == 404:
                                    #< an album Spotify has no data for. The bulk
                                    #  form said this with a null entry, and it
                                    #  counted as attempted for the same reason:
                                    #  otherwise it is re-queued every cycle forever
                                    attempted_ids.append(album_id)
                                    consecutiveFailures = 0
                                else:
                                    if firstFailure is None:
                                        firstFailure = _responseDetail(resp)
                                    albumFailures += 1
                                    consecutiveFailures += 1
                                    rateLimited = resp.status_code == SPOTIFY_RATE_LIMIT_STATUS
                            except Exception as e:
                                #< per album, so one reset does not cost the rest
                                #  of the batch its turn
                                if firstFailure is None:
                                    firstFailure = f"request failed: {e}"
                                albumFailures += 1
                                consecutiveFailures += 1

                            if rateLimited or consecutiveFailures >= CONSECUTIVE_FAILURE_ABORT:
                                break
                            stop_event.wait(SPOTIFY_REQUEST_PACE_SECONDS)

                        # Only when the Web API answered NOTHING. Falling back
                        # after a partial success would hand the cookie client
                        # the whole target list again, re-fetching albums that
                        # were just answered; the ones that failed are unstamped
                        # and come back next cycle either way.
                        use_fallback = not attempted_ids

                        if firstFailure is not None:
                            #< still behind the gate, and one line for the batch
                            #  rather than one per album: this path degrades to
                            #  the cookie client and logs that it did, so the
                            #  status is colour on a story the log already tells.
                            #  It carries the body for the same reason its twin
                            #  above does - when it IS asked for, the number on
                            #  its own answers nothing
                            if flaskDebugEnabled():
                                _dbmod.logger.warning(
                                    "[Backfiller-%s] Spotify Web API returned %s for %d of %d album(s).",
                                    self.user, firstFailure, albumFailures, len(target_ids)
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
