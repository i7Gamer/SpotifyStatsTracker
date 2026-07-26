from __future__ import annotations

"""One start/stop/status lifecycle for every periodic background worker.

Five workers each had their own copy: three Last.fm backfillers sharing a pair
of helpers, plus the Spotify metadata backfiller and the Wrapped recalculator
with a hand-rolled copy each. The copies had drifted - the Last.fm pair had
been hardened to take a lock around the check-and-assign, and the other two
never got that fix, so they still had the race it was written for (below).

Everything a periodic worker needs is here; a worker supplies only its loop,
its attribute names, and optionally a precondition.
"""
import Database.database as _dbmod  # noqa: F401 - threading/logger are reached
# through the database module so the suite's patch("Database.database.X")
# targets keep working.

WORKER_STOP_JOIN_TIMEOUT_SECONDS = 3


class PeriodicWorkerMixin:
    """Start/stop/status for a thread that loops until its event is set.

    Two invariants are the whole reason this is shared rather than copied:

    A FRESH EVENT PER RUN. stop() joins with a timeout, so a worker blocked in
    a slow HTTP call can outlive it. Reusing (and clearing) the old event would
    revive that zombie thread alongside the new one; with its own still-set
    event it exits at its next loop check instead. The event is passed into the
    loop rather than read off self for the same reason - a later restart
    reassigns the attribute, and a loop reading it would pick up the new one.

    CHECK AND ASSIGN UNDER ONE LOCK. The profile page's key save starts workers
    from a request thread: two concurrent saves (a double-clicked form) could
    both see no live thread, and the second's assignments would overwrite the
    first's - orphaning a running thread on an Event no reference survives for,
    so no stop path could ever reach it.
    """

    def _workerRunning(self, threadAttr: str) -> bool:
        thread = getattr(self, threadAttr, None)
        return thread is not None and thread.is_alive()

    def _startPeriodicWorker(self, threadAttr: str, eventAttr: str, loop, threadName: str,
                              precondition=None, logPrefix: str = "Worker") -> None:
        """Start `loop` on a daemon thread, unless it is already running.

        `precondition` is called under the lock and may veto the start (a
        keyless user gets no idle thread). It returns True to proceed; raising
        is the caller's business, not this one's.

        A Database missing the attributes entirely (a partially built instance,
        or one constructed with startWorkers=False) is a silent no-op, matching
        what every copy of this did before."""
        if not all(hasattr(self, attr) for attr in (threadAttr, eventAttr, "_worker_lock")):
            return
        with self._worker_lock:
            if self._workerRunning(threadAttr):
                return
            # Refuse once a stop has been requested, exactly as startListener
            # does ("a signaled instance never starts a listener again"), and
            # under the same lock as the check-and-assign so a signalStop can't
            # slip between them. Shutdown signals then joins a SNAPSHOT of
            # user_databases: a start landing after phase 1 - a /profile key
            # save, or _checkLoginLoop constructing a new Database after the
            # snapshot - would otherwise create its worker on a fresh, unset
            # event that nothing sets and nothing joins, leaving Last.fm HTTP
            # and SQLite writes running through teardown.
            if getattr(self, "_stopRequested", None) is not None and self._stopRequested():
                _dbmod.logger.debug("[%s-%s] not starting: this instance is stopping",
                                    logPrefix, getattr(self, "user", "?"))
                return
            if precondition is not None and not precondition():
                return
            stopEvent = _dbmod.threading.Event()
            setattr(self, eventAttr, stopEvent)
            thread = _dbmod.threading.Thread(target=loop, args=(stopEvent,), name=threadName, daemon=True)
            setattr(self, threadAttr, thread)
            thread.start()
            _dbmod.logger.debug("[%s-%s] started", logPrefix, getattr(self, "user", "?"))

    def _stopPeriodicWorker(self, threadAttr: str, eventAttr: str) -> None:
        """Signal under the lock so a concurrent start can't interleave, then
        join outside it - a join can block for seconds and nothing else needs
        to wait on that."""
        if not all(hasattr(self, attr) for attr in (threadAttr, eventAttr, "_worker_lock")):
            return
        with self._worker_lock:
            thread = getattr(self, threadAttr)
            if thread is None:
                return
            getattr(self, eventAttr).set()
            setattr(self, threadAttr, None)
        thread.join(timeout=WORKER_STOP_JOIN_TIMEOUT_SECONDS)

    def _workerStatus(self, threadAttr: str, telemetryKey: str, configured: bool) -> dict:
        """The {configured, running, ...telemetry} dict the Overview widgets
        read. `configured` is the worker's own question (has an API key, has
        credentials, or simply always) - everything else is uniform."""
        return {
            "configured": configured,
            "running": self._workerRunning(threadAttr),
            **self._getWorkerTelemetry(telemetryKey),
        }
