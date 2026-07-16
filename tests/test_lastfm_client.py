"""Last.fm client module: the process-wide rate limiter, tag->genre filtering
against the bundled whitelist, and response classification into the worker's
outcome taxonomy. All HTTP is mocked (conftest blocks real sockets anyway)."""
import sys
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.lastfm as lastfm
from Database.lastfm import (
    LastfmClient, LastfmRateLimiter, FetchOutcome,
    OUTCOME_OK, OUTCOME_NOT_FOUND, OUTCOME_TRANSIENT, OUTCOME_INVALID_KEY,
    LASTFM_API_ROOT, LASTFM_RATE_LIMIT_BACKOFF_SECONDS, GENRE_MAX_TAGS_PER_ENTITY,
    normalizeGenreTag, loadGenreWhitelist, filterTagsToGenres,
)


class _FakeClock:
    """Deterministic stand-in for the time module inside Database.lastfm:
    sleep() advances monotonic() instead of blocking."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class RateLimiterTestCase(unittest.TestCase):
    def _limiterWithClock(self, requestsPerSecond):
        clock = _FakeClock()
        patcher = patch.object(lastfm, "time", clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return LastfmRateLimiter(requestsPerSecond), clock

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
        limiter, clock = self._limiterWithClock(4)
        self.assertTrue(limiter.acquire())
        stopEvent = threading.Event()
        stopEvent.set()
        self.assertFalse(limiter.acquire(stop_event=stopEvent))

    def test_timeout_gives_up_without_a_slot(self):
        limiter, clock = self._limiterWithClock(4)
        limiter.applyBackoff(60)
        self.assertFalse(limiter.acquire(timeout=1.0))
        self.assertLessEqual(clock.now, 1.5)   #< gave up near the deadline, not after 60s

    def test_concurrent_threads_share_the_budget(self):
        """4 threads x 3 acquires against a real clock: 12 grants at 100 req/s
        can't complete faster than 11 intervals."""
        limiter = LastfmRateLimiter(100)
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


class TagFilteringTestCase(unittest.TestCase):
    def test_normalization_folds_case_hyphens_and_whitespace(self):
        self.assertEqual(normalizeGenreTag("Hip-Hop"), "hip hop")
        self.assertEqual(normalizeGenreTag("  Synth   Pop "), "synth pop")
        self.assertEqual(normalizeGenreTag("ROCK"), "rock")

    def test_whitelist_loads_from_the_bundled_file(self):
        whitelist = loadGenreWhitelist()
        self.assertGreater(len(whitelist), 1000)
        self.assertIn("rock", whitelist)
        self.assertIn("hip hop", whitelist)
        self.assertNotIn("seen live", whitelist)   #< classic Last.fm junk tag

    def test_filter_drops_non_genres_and_ranks_by_count(self):
        tags = [
            {"name": "seen live", "count": 100},
            {"name": "rock", "count": 80},
            {"name": "favorites", "count": 70},
            {"name": "indie rock", "count": 90},
        ]
        self.assertEqual(filterTagsToGenres(tags), ["indie rock", "rock"])

    def test_filter_caps_at_the_top_five(self):
        tags = [{"name": name, "count": 100 - index} for index, name in enumerate(
            ["rock", "pop", "jazz", "blues", "metal", "ambient", "techno"])]
        result = filterTagsToGenres(tags)
        self.assertEqual(len(result), GENRE_MAX_TAGS_PER_ENTITY)
        self.assertEqual(result, ["rock", "pop", "jazz", "blues", "metal"])

    def test_ties_break_alphabetically_for_determinism(self):
        tags = [{"name": "pop", "count": 100}, {"name": "jazz", "count": 100},
                {"name": "rock", "count": 100}]
        self.assertEqual(filterTagsToGenres(tags), ["jazz", "pop", "rock"])

    def test_variants_dedupe_after_normalization_keeping_the_best_count(self):
        tags = [
            {"name": "Hip-Hop", "count": 60},
            {"name": "hip hop", "count": 90},
            {"name": "Hip Hop", "count": 30},
            {"name": "rock", "count": 70},
        ]
        self.assertEqual(filterTagsToGenres(tags), ["hip hop", "rock"])

    def test_malformed_tags_are_tolerated(self):
        tags = [
            {"name": "rock", "count": "50"},   #< string count
            {"name": "pop"},                   #< missing count
            {"count": 10},                     #< missing name
            {"name": None, "count": 5},
            "not-a-dict",
        ]
        self.assertEqual(filterTagsToGenres(tags), ["rock", "pop"])


def _response(statusCode=200, payload=None, jsonError=False):
    response = MagicMock()
    response.status_code = statusCode
    if jsonError:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = payload if payload is not None else {}
    return response


class ClientTestCase(unittest.TestCase):
    def _client(self):
        limiter = MagicMock()
        limiter.acquire.return_value = True
        return LastfmClient("test-key", rateLimiter=limiter), limiter

    @patch("Database.lastfm.requests.get")
    def test_top_tags_request_carries_key_format_and_autocorrect(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = _response(payload={"toptags": {"tag": []}})
        outcome = client.getArtistTopTags("Radiohead")
        self.assertEqual(outcome.status, OUTCOME_OK)

        args, kwargs = mockGet.call_args
        self.assertEqual(args[0], LASTFM_API_ROOT)
        params = kwargs["params"]
        self.assertEqual(params["method"], "artist.gettoptags")
        self.assertEqual(params["artist"], "Radiohead")
        self.assertEqual(params["api_key"], "test-key")
        self.assertEqual(params["format"], "json")
        self.assertEqual(params["autocorrect"], "1")
        self.assertIn("timeout", kwargs)

    @patch("Database.lastfm.requests.get")
    def test_album_and_track_lookups_send_both_names(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = _response(payload={"toptags": {"tag": []}})
        client.getAlbumTopTags("Radiohead", "OK Computer")
        self.assertEqual(mockGet.call_args.kwargs["params"]["album"], "OK Computer")
        client.getTrackTopTags("Radiohead", "Karma Police")
        self.assertEqual(mockGet.call_args.kwargs["params"]["track"], "Karma Police")

    @patch("Database.lastfm.requests.get")
    def test_tag_list_response_is_ok_with_tags(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = _response(payload={"toptags": {"tag": [
            {"name": "rock", "count": 100}, {"name": "indie", "count": 50}]}})
        outcome = client.getArtistTopTags("Radiohead")
        self.assertEqual(outcome.status, OUTCOME_OK)
        self.assertEqual([t["name"] for t in outcome.tags], ["rock", "indie"])

    @patch("Database.lastfm.requests.get")
    def test_single_tag_dict_response_is_normalized_to_a_list(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = _response(payload={"toptags": {"tag": {"name": "rock", "count": 3}}})
        outcome = client.getArtistTopTags("Some Band")
        self.assertEqual(outcome.status, OUTCOME_OK)
        self.assertEqual(outcome.tags, [{"name": "rock", "count": 3}])

    @patch("Database.lastfm.requests.get")
    def test_empty_or_missing_toptags_is_a_definitive_ok(self, mockGet):
        client, _ = self._client()
        for payload in ({"toptags": {}}, {"toptags": {"tag": []}}, {}):
            mockGet.return_value = _response(payload=payload)
            outcome = client.getArtistTopTags("Obscure Artist")
            self.assertEqual(outcome.status, OUTCOME_OK)
            self.assertEqual(outcome.tags, [])

    @patch("Database.lastfm.requests.get")
    def test_error_6_is_not_found(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = _response(statusCode=400, payload={
            "error": 6, "message": "The artist you supplied could not be found"})
        self.assertEqual(client.getArtistTopTags("zzzz").status, OUTCOME_NOT_FOUND)

    @patch("Database.lastfm.requests.get")
    def test_error_29_is_transient_and_applies_backoff(self, mockGet):
        client, limiter = self._client()
        mockGet.return_value = _response(payload={"error": 29, "message": "Rate limit exceeded"})
        self.assertEqual(client.getArtistTopTags("x").status, OUTCOME_TRANSIENT)
        limiter.applyBackoff.assert_called_once_with(LASTFM_RATE_LIMIT_BACKOFF_SECONDS)

    @patch("Database.lastfm.requests.get")
    def test_http_429_is_transient_and_applies_backoff(self, mockGet):
        client, limiter = self._client()
        mockGet.return_value = _response(statusCode=429, jsonError=True)
        self.assertEqual(client.getArtistTopTags("x").status, OUTCOME_TRANSIENT)
        limiter.applyBackoff.assert_called_once_with(LASTFM_RATE_LIMIT_BACKOFF_SECONDS)

    @patch("Database.lastfm.requests.get")
    def test_invalid_or_suspended_key_is_its_own_outcome(self, mockGet):
        client, _ = self._client()
        for code in (10, 26):
            mockGet.return_value = _response(statusCode=403, payload={"error": code})
            self.assertEqual(client.getArtistTopTags("x").status, OUTCOME_INVALID_KEY)

    @patch("Database.lastfm.requests.get")
    def test_server_errors_bad_json_and_network_failures_are_transient(self, mockGet):
        client, _ = self._client()
        import requests as requestsModule

        mockGet.return_value = _response(statusCode=500, jsonError=True)
        self.assertEqual(client.getArtistTopTags("x").status, OUTCOME_TRANSIENT)

        mockGet.return_value = _response(statusCode=200, jsonError=True)
        self.assertEqual(client.getArtistTopTags("x").status, OUTCOME_TRANSIENT)

        mockGet.side_effect = requestsModule.exceptions.ConnectionError("boom")
        self.assertEqual(client.getArtistTopTags("x").status, OUTCOME_TRANSIENT)

    @patch("Database.lastfm.requests.get")
    def test_aborted_rate_limit_acquire_makes_no_request(self, mockGet):
        client, limiter = self._client()
        limiter.acquire.return_value = False
        self.assertIsNone(client.getArtistTopTags("x", stop_event=threading.Event()))
        mockGet.assert_not_called()


class ValidateApiKeyTestCase(unittest.TestCase):
    def _client(self, acquireResult=True):
        limiter = MagicMock()
        limiter.acquire.return_value = acquireResult
        return LastfmClient("test-key", rateLimiter=limiter)

    @patch("Database.lastfm.requests.get")
    def test_valid_key(self, mockGet):
        mockGet.return_value = _response(payload={"toptags": {"tag": [{"name": "pop", "count": 100}]}})
        self.assertEqual(self._client().validateApiKey(), {"ok": True, "error": None})

    @patch("Database.lastfm.requests.get")
    def test_invalid_key(self, mockGet):
        mockGet.return_value = _response(statusCode=403, payload={"error": 10})
        self.assertEqual(self._client().validateApiKey(), {"ok": False, "error": "invalid_key"})

    @patch("Database.lastfm.requests.get")
    def test_unreachable_service(self, mockGet):
        import requests as requestsModule
        mockGet.side_effect = requestsModule.exceptions.ConnectionError("down")
        self.assertEqual(self._client().validateApiKey(), {"ok": False, "error": "unreachable"})

    @patch("Database.lastfm.requests.get")
    def test_busy_limiter_gives_up_instead_of_blocking_the_request_thread(self, mockGet):
        client = self._client(acquireResult=False)
        self.assertEqual(client.validateApiKey(), {"ok": False, "error": "busy"})
        mockGet.assert_not_called()


if __name__ == "__main__":
    unittest.main()
