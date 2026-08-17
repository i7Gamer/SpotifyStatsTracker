# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Waiting for a thread to ARRIVE, instead of sleeping and hoping it did.

The serialization tests all have the same shape: hold thread A inside a
critical section, get thread B to try to enter it, and assert that B could not.
Proving that B could not requires knowing B actually TRIED - and the tests used
to establish it with `time.sleep(0.05)` inside the critical section, i.e. "B has
surely got there by now".

That is a false-negative machine, and in the safe-looking direction. Under CI
load B may not have reached the lock inside the window at all; A then finishes,
B runs afterwards, nothing ever overlaps, and the test passes while reporting
nothing. It fails to detect, rather than failing loudly, which is the worse of
the two ways for a concurrency test to be wrong.

WaiterCountingLock replaces the sleep with the actual signal. It wraps the real
lock and counts everyone who has entered `acquire`/`__enter__` - the holder plus
everyone blocked behind it - so a test can wait for "N threads are now at this
lock" and proceed the instant it is true. On an idle machine that is faster than
the sleep it replaces; on a loaded one it waits as long as it has to. There is
no duration anywhere that has to be guessed.

The one bounded wait left is `waitFor`'s timeout, and it is a FAILURE path: it
expires only if the threads never arrive, which means the lock under test is
gone rather than merely contended. Generous on purpose - it costs nothing on a
passing run, because a passing run never reaches the end of it.
"""
import threading

#< only reached when the waiters never arrive at all - i.e. the code under test
#  stopped taking the lock. Long enough that no amount of CI load reaches it.
ARRIVAL_TIMEOUT_SECONDS = 30


class WaiterCountingLock:
    """A lock that reports how many DISTINCT THREADS are in or waiting on it.

    Delegates to `inner`, which must be the SAME object every thread under test
    resolves to, or they are not contending for anything. Supports both the
    context-manager form and the bare `acquire(blocking=False)`/`release()` pair,
    because the code under test uses both (see _checkAndRecalculateWrapped's
    non-blocking probe).

    Threads, not acquisitions, because the locks this wraps are RLocks taken
    re-entrantly: Database._importLock is held by importHistoryBatch and taken
    AGAIN by importHistory inside it. Counting acquisitions would let one thread
    satisfy `waitFor(2)` by itself - a test that passes without a second thread
    ever existing, which is precisely the failure mode this file exists to
    remove.
    """

    def __init__(self, inner):
        self._inner = inner
        self._depthByThread = {}
        self._guard = threading.Lock()
        self._changed = threading.Condition(self._guard)

    def _enter(self):
        """Counted BEFORE the acquire, so a thread BLOCKED on the lock counts
        as arrived - which is the whole question these tests ask."""
        ident = threading.get_ident()
        with self._changed:
            self._depthByThread[ident] = self._depthByThread.get(ident, 0) + 1
            self._changed.notify_all()

    def _leave(self):
        ident = threading.get_ident()
        with self._changed:
            depth = self._depthByThread.get(ident, 0) - 1
            if depth > 0:
                self._depthByThread[ident] = depth   #< still holds an outer acquisition
            else:
                self._depthByThread.pop(ident, None)
            self._changed.notify_all()

    def waitFor(self, threadCount, timeout=ARRIVAL_TIMEOUT_SECONDS):
        """Block until `threadCount` distinct threads have arrived. True if
        they did, False if the timeout expired - which means they never came."""
        with self._changed:
            return self._changed.wait_for(
                lambda: len(self._depthByThread) >= threadCount, timeout=timeout)

    @property
    def arrived(self):
        with self._guard:
            return len(self._depthByThread)

    def acquire(self, *args, **kwargs):
        self._enter()
        acquired = self._inner.acquire(*args, **kwargs)
        if not acquired:
            #< a refused non-blocking probe never held it, so it never leaves
            self._leave()
        return acquired

    def release(self):
        self._inner.release()
        self._leave()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False
