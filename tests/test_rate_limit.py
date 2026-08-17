"""The process-wide slot rate limiter shared by the Last.fm and Spotify
integrations: request spacing, penalty windows, and the observability snapshot
the /admin Worker Health card reads.

Everything here runs against a fake clock (see _FakeClock) rather than real
sleeps - the one exception is the concurrency test, which needs real threads
contending on a real lock to prove the budget is genuinely shared."""
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.rate_limit as rateLimitModule
from Database.rate_limit import SlotRateLimiter


class _FakeClock:
    """Deterministic stand-in for the time module inside Database.rate_limit:
    sleep() advances monotonic() instead of blocking."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SlotRateLimiterTestCase(unittest.TestCase):
    def _limiterWithClock(self, requestsPerSecond):
        clock = _FakeClock()
        patcher = patch.object(rateLimitModule, "time", clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return SlotRateLimiter(requestsPerSecond), clock

    def test_grants_are_spaced_by_the_request_interval(self):
        limiter, clock = self._limiterWithClock(4)
        self.assertTrue(limiter.acquire())
        self.assertEqual(clock.now, 0.0)
        self.assertTrue(limiter.acquire())
        self.assertAlmostEqual(clock.now, 0.25)
        self.assertTrue(limiter.acquire())
        self.assertAlmostEqual(clock.now, 0.5)

    def test_backoff_pushes_the_next_slot_past_the_penalty_window(self):
        limiter, clock = self._limiterWithClock(4)
        self.assertTrue(limiter.acquire())
        limiter.applyBackoff(60)
        self.assertTrue(limiter.acquire())
        self.assertGreaterEqual(clock.now, 60.0)

    def test_overlapping_backoffs_never_shrink_the_window(self):
        limiter, clock = self._limiterWithClock(4)
        limiter.applyBackoff(60)
        limiter.applyBackoff(10)   #< the shorter penalty must not override the longer one
        self.assertTrue(limiter.acquire())
        self.assertGreaterEqual(clock.now, 60.0)

    def test_stop_event_aborts_the_wait(self):
        limiter, _ = self._limiterWithClock(4)
        self.assertTrue(limiter.acquire())
        stopEvent = threading.Event()
        stopEvent.set()
        self.assertFalse(limiter.acquire(stop_event=stopEvent))

    def test_a_stop_already_set_is_honoured_even_when_a_slot_is_free(self):
        """The wait is not the only place a stop matters. The test above
        deliberately consumes the slot first so the call has to WAIT, which is
        the only path that ever consulted the event: a worker shutting down
        while the limiter happened to be idle was granted the slot anyway and
        fired one last request on the way out - and burned a slot doing it, so
        whatever ran next was paced against a request nobody wanted."""
        limiter, clock = self._limiterWithClock(4)
        stopEvent = threading.Event()
        stopEvent.set()

        self.assertFalse(limiter.acquire(stop_event=stopEvent))
        #< and the refused call left the budget untouched: the next caller is
        #  still granted immediately rather than being spaced behind a ghost
        self.assertTrue(limiter.acquire())
        self.assertEqual(clock.now, 0.0)

    def test_timeout_gives_up_without_a_slot(self):
        limiter, clock = self._limiterWithClock(4)
        limiter.applyBackoff(60)
        self.assertFalse(limiter.acquire(timeout=1.0))
        self.assertLessEqual(clock.now, 1.5)   #< gave up near the deadline, not after 60s

    def test_zero_interval_never_sleeps(self):
        """The suite (and any deployment that wants pacing off) collapses the
        interval to 0 - that must grant immediately, forever, not busy-wait."""
        limiter, clock = self._limiterWithClock(4)
        limiter._interval = 0.0
        for _ in range(5):
            self.assertTrue(limiter.acquire())
        self.assertEqual(clock.now, 0.0)


class SnapshotTestCase(unittest.TestCase):
    """snapshot() is the whole observability half: before this existed a
    rate-limit event was a log line nobody counted, so 'is it getting worse?'
    could only be answered by grepping app.log."""

    def _limiterWithClock(self, requestsPerSecond=4):
        clock = _FakeClock()
        patcher = patch.object(rateLimitModule, "time", clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return SlotRateLimiter(requestsPerSecond), clock

    def test_fresh_limiter_reports_nothing_seen(self):
        limiter, _ = self._limiterWithClock()
        snapshot = limiter.snapshot()
        self.assertEqual(snapshot["backoffs"], 0)
        self.assertIsNone(snapshot["secondsSinceLastBackoff"])
        self.assertIsNone(snapshot["lastReason"])
        self.assertEqual(snapshot["backoffRemainingSeconds"], 0.0)

    def test_each_backoff_is_counted(self):
        limiter, _ = self._limiterWithClock()
        limiter.applyBackoff(30)
        limiter.applyBackoff(30)
        self.assertEqual(limiter.snapshot()["backoffs"], 2)

    def test_snapshot_reports_the_most_recent_reason(self):
        limiter, _ = self._limiterWithClock()
        limiter.applyBackoff(30, reason="profile")
        limiter.applyBackoff(30, reason="connect-state")
        self.assertEqual(limiter.snapshot()["lastReason"], "connect-state")

    def test_remaining_window_counts_down_and_floors_at_zero(self):
        limiter, clock = self._limiterWithClock()
        limiter.applyBackoff(30)
        self.assertAlmostEqual(limiter.snapshot()["backoffRemainingSeconds"], 30.0)
        clock.sleep(10)
        self.assertAlmostEqual(limiter.snapshot()["backoffRemainingSeconds"], 20.0)
        clock.sleep(999)
        self.assertEqual(limiter.snapshot()["backoffRemainingSeconds"], 0.0)

    def test_seconds_since_last_backoff_tracks_the_clock(self):
        limiter, clock = self._limiterWithClock()
        limiter.applyBackoff(30)
        clock.sleep(45)
        self.assertAlmostEqual(limiter.snapshot()["secondsSinceLastBackoff"], 45.0)

    def test_a_shorter_later_backoff_still_counts_as_an_event(self):
        """applyBackoff max()es the WINDOW, but the event count and the reason
        must still record the newer occurrence - otherwise a burst of small
        penalties inside one long window would look like a single event."""
        limiter, _ = self._limiterWithClock()
        limiter.applyBackoff(60, reason="first")
        limiter.applyBackoff(5, reason="second")
        snapshot = limiter.snapshot()
        self.assertEqual(snapshot["backoffs"], 2)
        self.assertEqual(snapshot["lastReason"], "second")
        self.assertAlmostEqual(snapshot["backoffRemainingSeconds"], 60.0)


class SharedBudgetTestCase(unittest.TestCase):
    def test_concurrent_threads_share_the_budget(self):
        """4 threads x 3 acquires against a real clock: 12 grants at 100 req/s
        can't complete faster than 11 intervals. This is the property the whole
        module exists for - per-thread pacing would finish in a quarter of it."""
        limiter = SlotRateLimiter(100)
        threadsCount, acquiresPerThread = 4, 3

        def worker():
            for _ in range(acquiresPerThread):
                self.assertTrue(limiter.acquire())

        start = time.monotonic()
        threads = [threading.Thread(target=worker) for _ in range(threadsCount)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - start
        minimumSpan = (threadsCount * acquiresPerThread - 1) * (1.0 / 100)
        self.assertGreaterEqual(elapsed, minimumSpan * 0.9)


class SpotifyLimiterTestCase(unittest.TestCase):
    def test_the_spotify_singleton_exists_and_is_shared(self):
        """Every Spotify call site imports this one object - a second instance
        anywhere would silently split the budget it exists to unify."""
        from Database.rate_limit import SPOTIFY_LIMITER, SPOTIFY_REQUESTS_PER_SECOND

        self.assertIsInstance(SPOTIFY_LIMITER, SlotRateLimiter)
        self.assertGreater(SPOTIFY_REQUESTS_PER_SECOND, 0)

    def test_lastfm_limiter_is_a_separate_instance(self):
        """Two APIs, two budgets: a Spotify bot-check must not stall the
        Last.fm backfillers, and vice versa."""
        from Database.lastfm import RATE_LIMITER
        from Database.rate_limit import SPOTIFY_LIMITER

        self.assertIsNot(RATE_LIMITER, SPOTIFY_LIMITER)


if __name__ == "__main__":
    unittest.main()
