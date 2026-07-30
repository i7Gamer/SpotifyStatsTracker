# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Process-wide request pacing for the external APIs this instance talks to.

Last.fm and Spotify both enforce their limits PER IP, and every user's
listener and background workers run inside this one process behind one
address. A budget tracked per user - or, worse, per listener object - is
therefore not tracking the thing being limited: N users simply meant N times
the traffic, and one user's backoff did nothing about the other N-1 still
hammering. One shared limiter per API is the shape that matches the
constraint.

SlotRateLimiter does two jobs:

- SPACING. acquire() hands out request slots `1/requestsPerSecond` apart
  across every thread in the process. Sized as headroom, not as a throttle:
  the Spotify default sits far above the steady-state rate (see
  SPOTIFY_REQUESTS_PER_SECOND), because squeezing the connect-state poll costs
  missed plays. Its real job at steady state is to stop every user's poll from
  landing in the same instant.

- PENALTY. applyBackoff() pushes every future slot - for every user, on every
  thread - past a cool-down window. This is the half that matters when Spotify
  starts answering with bot-check pages: the whole process pauses, rather than
  the one thread that happened to notice.

applyBackoff also counts what it saw, and snapshot() reports it. That is
deliberate: a rate-limit event used to be a log line nobody counted, so
"is this getting worse?" could only be answered by grepping app.log.
"""
import threading
import time

# Process-wide Spotify request budget. Deliberately generous - at four users
# polling connect-state every 6s the steady-state rate is ~0.7 req/s, so this
# is a no-op until roughly 24 users, and the module's value until then is the
# shared PENALTY window plus phase separation between users. Lower it if
# Spotify's bot-check pages ever become sustained rather than sporadic.
SPOTIFY_REQUESTS_PER_SECOND = 4

# Cool-down applied process-wide once Spotify answers anything with a
# bot-check page, a 429, or a throttled connect-state PUT.
#
# Shorter than the Last.fm equivalent (60s) on purpose. One window covers every
# Spotify endpoint - they share the IP, so splitting it per endpoint would
# re-open the exact hole this closes - and connect-state polling is what
# detects track changes, so the window is also how long play tracking goes
# blind. 30s bounds that to well under a track length while still being a real
# pause: at the historical rate (~1 event/day) it costs half a minute a day.
SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS = 30

# How long a polling caller waits for a slot before giving up. Every Spotify
# poll loop comes back around within seconds, so a short timeout that lets the
# loop re-check its stop flags beats a long block that a shutdown has to wait
# out (see the 2026-07-17 shutdown hang).
SPOTIFY_ACQUIRE_TIMEOUT_SECONDS = 1.0

# The one call site that waits out a whole penalty window instead: fetching a
# track's metadata. It is a one-shot on the path of recording a play that
# ALREADY happened, and giving up there drops the play entirely (see the
# SongError note in Database/patches.py). Waiting 30s costs nothing but a
# delayed write; failing costs history.
SPOTIFY_TRACK_ACQUIRE_TIMEOUT_SECONDS = SPOTIFY_RATE_LIMIT_BACKOFF_SECONDS + 5


class SpotifyLocallyRateLimitedError(Exception):
    """No slot came free within the caller's timeout: THIS process is holding
    Spotify traffic back, and the request was never sent.

    Never evidence of Spotify pushing back. The pause it reports is one we are
    already applying, so feeding it into applyBackoff re-arms the very window
    that raised it - and with one limiter shared by every listener, a single
    real rate-limit event then fans out into one bogus event per user: the
    count inflates, the recorded cause is overwritten by whichever listener
    tripped last, and the window creeps forward on each one.

    So every call site must test for this BY TYPE, ahead of any string
    heuristic. Its message deliberately still says "rate limit" - the
    listener's substring classifier needs that to bucket it as transient and
    stand the poll loop down - which is exactly why the type check has to come
    first.
    """


class SlotRateLimiter:
    """Thread-safe slot spacer on the monotonic clock: acquire() blocks until
    the next request slot is free (returning False if the stop event fires or
    the timeout expires first), applyBackoff() pushes every future slot past a
    penalty window. One shared instance paces a whole process."""

    def __init__(self, requestsPerSecond: float):
        self._interval = 1.0 / requestsPerSecond
        self._lock = threading.Lock()
        self._nextSlotAt = 0.0
        self._backoffUntil = 0.0
        self._backoffCount = 0
        self._lastBackoffAt: float | None = None
        self._lastReason: str | None = None

    def acquire(self, stop_event: threading.Event | None = None,
                timeout: float | None = None) -> bool:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            with self._lock:
                now = time.monotonic()
                readyAt = max(self._nextSlotAt, self._backoffUntil)
                if readyAt <= now:
                    self._nextSlotAt = now + self._interval
                    return True
                waitFor = readyAt - now
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                waitFor = min(waitFor, remaining)
            if stop_event is not None:
                if stop_event.wait(waitFor):
                    return False
            else:
                time.sleep(waitFor)

    def applyBackoff(self, seconds: float, reason: str | None = None) -> None:
        """Pause every future slot for `seconds`, and record that it happened.

        The WINDOW is max()ed so overlapping penalties from concurrent threads
        never shrink an already-running one - but the counter and the reason
        always take the newer event, or a burst of short penalties landing
        inside one long window would read as a single occurrence."""
        with self._lock:
            now = time.monotonic()
            self._backoffUntil = max(self._backoffUntil, now + seconds)
            self._backoffCount += 1
            self._lastBackoffAt = now
            self._lastReason = reason

    def snapshot(self) -> dict:
        """What has this limiter seen, and is it currently holding traffic?

        All durations are derived from the monotonic clock, so they are
        "how long ago" / "how much longer" rather than wall-clock timestamps -
        which is both what the admin card wants to show and what keeps this
        testable against a fake clock."""
        with self._lock:
            now = time.monotonic()
            return {
                "backoffs": self._backoffCount,
                "secondsSinceLastBackoff": (
                    None if self._lastBackoffAt is None else now - self._lastBackoffAt),
                "lastReason": self._lastReason,
                "backoffRemainingSeconds": max(0.0, self._backoffUntil - now),
            }


# Shared by every Spotify call site in the process: the per-user connect-state
# poll (Database/Spotify/recentlyPlayed.py), track metadata fetches
# (Database/Spotify/client.py's getTrackInfoWithRetry), and the
# account-settings profile/plan lookups (hooked in Database/patches.py).
SPOTIFY_LIMITER = SlotRateLimiter(SPOTIFY_REQUESTS_PER_SECOND)
