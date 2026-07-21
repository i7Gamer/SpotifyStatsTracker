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
    LastfmClient, LastfmRateLimiter, FetchOutcome, ArtistInfoOutcome, AlbumInfoOutcome,
    OUTCOME_OK, OUTCOME_NOT_FOUND, OUTCOME_TRANSIENT, OUTCOME_INVALID_KEY,
    LASTFM_API_ROOT, LASTFM_RATE_LIMIT_BACKOFF_SECONDS, GENRE_MAX_TAGS_PER_ENTITY,
    normalizeGenreTag, loadGenreWhitelist, filterTagsToGenres,
    GENRE_TAG_ALIASES, cleanLookupName, foldStylizedArtistName,
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

    def test_alias_tags_map_to_their_whitelisted_genre(self):
        tags = [
            {"name": "rap", "count": 100},
            {"name": "Alternative", "count": 90},
            {"name": "indie", "count": 80},
            {"name": "rnb", "count": 70},
        ]
        self.assertEqual(filterTagsToGenres(tags),
                         ["hip hop", "alternative rock", "indie rock", "r&b"])

    def test_alias_and_direct_tag_merge_keeping_the_best_count(self):
        tags = [
            {"name": "rap", "count": 90},       #< alias carries the better count
            {"name": "Hip-Hop", "count": 40},
            {"name": "rock", "count": 50},
        ]
        self.assertEqual(filterTagsToGenres(tags), ["hip hop", "rock"])

    def test_alias_sources_are_absent_from_the_whitelist_and_targets_resolve(self):
        """The alias map is only consulted on a whitelist miss - a source that
        is also a whitelisted genre would silently never alias; a target
        outside the whitelist would silently drop the tag."""
        whitelist = loadGenreWhitelist()
        for source, target in GENRE_TAG_ALIASES.items():
            self.assertEqual(source, normalizeGenreTag(source))   #< keys stored pre-normalized
            self.assertNotIn(source, whitelist)
            self.assertIn(normalizeGenreTag(target), whitelist)


class LookupNameCleaningTestCase(unittest.TestCase):
    def test_version_suffixes_after_a_dash_are_stripped(self):
        for name, cleaned in [
            ("Alors on danse - Radio Edit", "Alors on danse"),
            ("Just In Time - 1998 Remaster", "Just In Time"),
            ("Can't Take My Eyes Off You - Original Extended Version", "Can't Take My Eyes Off You"),
            ("Be Like That - feat. Swae Lee & Khalid", "Be Like That"),
            ("Breaking Free - High School Mix", "Breaking Free"),
            ("Song Title - Live - Remastered 2011", "Song Title"),
        ]:
            self.assertEqual(cleanLookupName(name), cleaned, name)

    def test_real_dash_subtitles_are_kept(self):
        self.assertEqual(cleanLookupName("Party - Ich will abgehn"),
                         "Party - Ich will abgehn")

    def test_decoration_parentheticals_and_brackets_are_stripped(self):
        for name, cleaned in [
            ("Ain't Nobody (Loves Me Better) (feat. Jasmine Thompson)",
             "Ain't Nobody (Loves Me Better)"),
            ("Blood (with Foy Vance) [Drezo Remix]", "Blood"),
            ("Save Your Tears (with Ariana Grande) (Remix)", "Save Your Tears"),
            ("OK Computer (Deluxe Edition)", "OK Computer"),
        ]:
            self.assertEqual(cleanLookupName(name), cleaned, name)

    def test_meaningful_parentheticals_are_kept(self):
        for name in ("You Spin Me Round (Like a Record)",
                     "(I Can't Get No) Satisfaction",
                     "Alive"):   #< 'live' inside a word must not trigger
            self.assertEqual(cleanLookupName(name), name)

    def test_undecorated_names_come_back_identical(self):
        self.assertEqual(cleanLookupName("Wrecked"), "Wrecked")

    def test_cleaning_never_returns_an_empty_name(self):
        for name in ("(feat. Someone)", " - Remastered", "Remix"):
            self.assertEqual(cleanLookupName(name), name)


class StylizedArtistNameFoldingTestCase(unittest.TestCase):
    def test_stylized_letters_fold_to_plain_ascii(self):
        for name, folded in [
            ("HUGØ", "HUGO"),
            ("LUNDØN", "LUNDON"),
            ("NIGHTMÆR", "NIGHTMAER"),
            ("Schættes", "Schaettes"),
            ("Đogani", "Dogani"),
        ]:
            self.assertEqual(foldStylizedArtistName(name), folded, name)

    def test_decorative_marks_are_stripped(self):
        for name, folded in [
            ("Jinka †", "Jinka"),
            ("Lavatera★", "Lavatera"),
            ("MAIA⠀", "MAIA"),
        ]:
            self.assertEqual(foldStylizedArtistName(name), folded, name)

    def test_stray_whitespace_alone_is_folded(self):
        self.assertEqual(foldStylizedArtistName("Travis Van Hoff "), "Travis Van Hoff")

    def test_real_diacritics_are_left_untouched(self):
        """Last.fm resolves genuine accents fine on the first try (these are
        already-tagged artists in the catalog) - folding must not touch them,
        only the lookalike-letter/decorative-mark cases above."""
        for name in ("Emilíana Torrini", "Alfred García", "Die Ärzte", "René LaVice"):
            self.assertEqual(foldStylizedArtistName(name), name)

    def test_undecorated_names_come_back_identical(self):
        self.assertEqual(foldStylizedArtistName("Radiohead"), "Radiohead")


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


class ArtistNameFoldFallbackTestCase(unittest.TestCase):
    """A definitive-empty artist.gettoptags result retries once with
    foldStylizedArtistName's stylized-letter/decorative-mark folding applied
    - confirmed live against the API to recover real tag data for some
    artists ("HUGO" where the stored "HUGO" has none)."""

    def _client(self, acquireResult=True):
        limiter = MagicMock()
        limiter.acquire.return_value = acquireResult
        return LastfmClient("test-key", rateLimiter=limiter), limiter

    @patch("Database.lastfm.requests.get")
    def test_fallback_triggers_on_empty_result_and_uses_the_folded_name(self, mockGet):
        client, _ = self._client()
        mockGet.side_effect = [
            _response(payload={"toptags": {"tag": []}}),
            _response(payload={"toptags": {"tag": [{"name": "rock", "count": 10}]}}),
        ]
        outcome = client.getArtistTopTags("HUGØ")
        self.assertEqual(outcome.status, OUTCOME_OK)
        self.assertEqual([t["name"] for t in outcome.tags], ["rock"])
        self.assertEqual(mockGet.call_count, 2)
        secondParams = mockGet.call_args_list[1].kwargs["params"]
        self.assertEqual(secondParams["method"], "artist.gettoptags")
        self.assertEqual(secondParams["artist"], "HUGO")

    @patch("Database.lastfm.requests.get")
    def test_fallback_not_triggered_when_gettoptags_succeeds(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = _response(payload={"toptags": {"tag": [{"name": "rock", "count": 10}]}})
        outcome = client.getArtistTopTags("Radiohead")
        self.assertEqual(outcome.status, OUTCOME_OK)
        mockGet.assert_called_once()   #< name doesn't fold, no second request

    @patch("Database.lastfm.requests.get")
    def test_fallback_not_triggered_when_the_name_does_not_fold(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = _response(payload={"toptags": {"tag": []}})
        outcome = client.getArtistTopTags("Pikayzo")
        self.assertEqual(outcome.status, OUTCOME_OK)
        self.assertEqual(outcome.tags, [])
        mockGet.assert_called_once()   #< "Pikayzo" == foldStylizedArtistName("Pikayzo")

    @patch("Database.lastfm.requests.get")
    def test_both_attempts_empty_stays_a_definitive_empty_result(self, mockGet):
        client, _ = self._client()
        mockGet.side_effect = [
            _response(payload={"toptags": {"tag": []}}),
            _response(payload={"toptags": {"tag": []}}),
        ]
        outcome = client.getArtistTopTags("LUNDØN")
        self.assertEqual(outcome.status, OUTCOME_OK)
        self.assertEqual(outcome.tags, [])
        self.assertEqual(mockGet.call_count, 2)

    @patch("Database.lastfm.requests.get")
    def test_fallback_abort_propagates_none_not_the_stale_empty_result(self, mockGet):
        client, limiter = self._client()
        limiter.acquire.side_effect = [True, False]   #< first lookup succeeds, folded retry's slot never granted
        mockGet.return_value = _response(payload={"toptags": {"tag": []}})
        outcome = client.getArtistTopTags("HUGØ", stop_event=threading.Event())
        self.assertIsNone(outcome)
        mockGet.assert_called_once()   #< only the first (verbatim) request went out

    @patch("Database.lastfm.requests.get")
    def test_fallback_transient_error_is_not_definitive(self, mockGet):
        client, _ = self._client()
        mockGet.side_effect = [
            _response(payload={"toptags": {"tag": []}}),
            _response(statusCode=500, jsonError=True),
        ]
        outcome = client.getArtistTopTags("HUGØ")
        self.assertEqual(outcome.status, OUTCOME_TRANSIENT)

    @patch("Database.lastfm.requests.get")
    def test_fallback_not_found_is_still_definitive(self, mockGet):
        client, _ = self._client()
        mockGet.side_effect = [
            _response(payload={"toptags": {"tag": []}}),
            _response(statusCode=400, payload={"error": 6, "message": "not found"}),
        ]
        outcome = client.getArtistTopTags("HUGØ")
        self.assertEqual(outcome.status, OUTCOME_NOT_FOUND)


class AlbumGetInfoFallbackTestCase(unittest.TestCase):
    """album.gettoptags is confirmed unreliable for some albums - getAlbumTopTags
    falls back to album.getinfo's embedded tags on a definitive-empty result."""

    def _client(self, acquireResult=True):
        limiter = MagicMock()
        limiter.acquire.return_value = acquireResult
        return LastfmClient("test-key", rateLimiter=limiter), limiter

    @patch("Database.lastfm.requests.get")
    def test_fallback_triggers_on_empty_gettoptags_and_uses_getinfo_tags(self, mockGet):
        client, _ = self._client()
        mockGet.side_effect = [
            _response(payload={"toptags": {"tag": []}}),
            _response(payload={"album": {"tags": {"tag": [
                {"name": "rock"}, {"name": "indie rock"}]}}}),
        ]
        outcome = client.getAlbumTopTags("Imagine Dragons", "Wrecked")
        self.assertEqual(outcome.status, OUTCOME_OK)
        self.assertEqual([t["name"] for t in outcome.tags], ["rock", "indie rock"])
        self.assertEqual(mockGet.call_count, 2)
        secondParams = mockGet.call_args_list[1].kwargs["params"]
        self.assertEqual(secondParams["method"], "album.getinfo")
        self.assertEqual(secondParams["artist"], "Imagine Dragons")
        self.assertEqual(secondParams["album"], "Wrecked")

    @patch("Database.lastfm.requests.get")
    def test_fallback_not_triggered_when_gettoptags_succeeds(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = _response(payload={"toptags": {"tag": [{"name": "rock", "count": 10}]}})
        outcome = client.getAlbumTopTags("Metallica", "Master of Puppets")
        self.assertEqual(outcome.status, OUTCOME_OK)
        self.assertEqual(len(outcome.tags), 1)
        mockGet.assert_called_once()   #< getinfo never called - gettoptags already succeeded

    @patch("Database.lastfm.requests.get")
    def test_both_endpoints_empty_stays_a_definitive_empty_result(self, mockGet):
        client, _ = self._client()
        mockGet.side_effect = [
            _response(payload={"toptags": {"tag": []}}),
            _response(payload={"album": {"tags": {"tag": []}}}),
        ]
        outcome = client.getAlbumTopTags("Pikayzo", "Some Album")
        self.assertEqual(outcome.status, OUTCOME_OK)
        self.assertEqual(outcome.tags, [])
        self.assertEqual(mockGet.call_count, 2)

    @patch("Database.lastfm.requests.get")
    def test_getinfo_bare_tag_dict_is_normalized_to_a_list(self, mockGet):
        client, _ = self._client()
        mockGet.side_effect = [
            _response(payload={"toptags": {"tag": []}}),
            _response(payload={"album": {"tags": {"tag": {"name": "soundtrack"}}}}),
        ]
        outcome = client.getAlbumTopTags("Joe Hisaishi", "Spirited Away Soundtrack")
        self.assertEqual(outcome.tags, [{"name": "soundtrack"}])

    @patch("Database.lastfm.requests.get")
    def test_getinfo_string_tags_field_reads_as_no_tags(self, mockGet):
        """Last.fm sometimes returns `"tags": ""` instead of a dict when an
        album has no info-page tags either - observed for "Encanto"."""
        client, _ = self._client()
        mockGet.side_effect = [
            _response(payload={"toptags": {"tag": []}}),
            _response(payload={"album": {"tags": ""}}),
        ]
        outcome = client.getAlbumTopTags("Stephanie Beatriz", "Encanto")
        self.assertEqual(outcome.status, OUTCOME_OK)
        self.assertEqual(outcome.tags, [])

    @patch("Database.lastfm.requests.get")
    def test_fallback_abort_propagates_none_not_the_stale_empty_result(self, mockGet):
        client, limiter = self._client()
        limiter.acquire.side_effect = [True, False]   #< gettoptags succeeds, getinfo's slot never granted
        mockGet.return_value = _response(payload={"toptags": {"tag": []}})
        outcome = client.getAlbumTopTags("Imagine Dragons", "Wrecked", stop_event=threading.Event())
        self.assertIsNone(outcome)
        mockGet.assert_called_once()   #< only the first (gettoptags) request went out

    @patch("Database.lastfm.requests.get")
    def test_fallback_transient_error_is_not_definitive(self, mockGet):
        client, _ = self._client()
        mockGet.side_effect = [
            _response(payload={"toptags": {"tag": []}}),
            _response(statusCode=500, jsonError=True),
        ]
        outcome = client.getAlbumTopTags("Imagine Dragons", "Wrecked")
        self.assertEqual(outcome.status, OUTCOME_TRANSIENT)

    @patch("Database.lastfm.requests.get")
    def test_fallback_not_found_is_still_definitive(self, mockGet):
        client, _ = self._client()
        mockGet.side_effect = [
            _response(payload={"toptags": {"tag": []}}),
            _response(statusCode=400, payload={"error": 6, "message": "not found"}),
        ]
        outcome = client.getAlbumTopTags("Imagine Dragons", "Wrecked")
        self.assertEqual(outcome.status, OUTCOME_NOT_FOUND)


class ArtistInfoBioTestCase(unittest.TestCase):
    """getArtistInfo (artist.getinfo) for the artist-bio feature: HTML
    stripping, dead "Read more" link text removal, and the "+"-name
    incorrect-tag merge-redirect guard."""

    def _client(self, acquireResult=True):
        limiter = MagicMock()
        limiter.acquire.return_value = acquireResult
        return LastfmClient("test-key", rateLimiter=limiter), limiter

    @staticmethod
    def _infoResponse(bioSummary):
        return _response(payload={"artist": {"name": "Some Artist",
                                              "bio": {"summary": bioSummary}}})

    @patch("Database.lastfm.requests.get")
    def test_request_carries_key_format_autocorrect_and_artist(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = self._infoResponse("A short bio.")
        client.getArtistInfo("Radiohead")
        params = mockGet.call_args.kwargs["params"]
        self.assertEqual(params["method"], "artist.getinfo")
        self.assertEqual(params["artist"], "Radiohead")
        self.assertEqual(params["api_key"], "test-key")
        self.assertEqual(params["autocorrect"], "1")

    @patch("Database.lastfm.requests.get")
    def test_plain_bio_is_returned_as_is(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = self._infoResponse("A rock band from Oxford.")
        outcome = client.getArtistInfo("Radiohead")
        self.assertEqual(outcome.status, OUTCOME_OK)
        self.assertEqual(outcome.bio, "A rock band from Oxford.")

    @patch("Database.lastfm.requests.get")
    def test_embedded_html_is_stripped_to_plain_text(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = self._infoResponse(
            'A rock band. <a href="https://last.fm/x">Read more on Last.fm</a>. '
            "User-contributed text is available under the Creative Commons "
            "By-SA License; additional terms may apply.")
        outcome = client.getArtistInfo("Radiohead")
        self.assertNotIn("<a", outcome.bio)
        self.assertNotIn("</a>", outcome.bio)
        # The dead "Read more" link text is dropped - it points nowhere once
        # the anchor tag itself is stripped.
        self.assertNotIn("Read more on Last.fm", outcome.bio)
        # The CC attribution sentence is kept - it's the license notice itself.
        self.assertIn("Creative Commons By-SA License", outcome.bio)
        self.assertIn("A rock band.", outcome.bio)

    @patch("Database.lastfm.requests.get")
    def test_html_entities_are_unescaped(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = self._infoResponse("Florence &amp; the Machine&#39;s sound.")
        outcome = client.getArtistInfo("Florence + The Machine")
        self.assertEqual(outcome.bio, "Florence & the Machine's sound.")

    @patch("Database.lastfm.requests.get")
    def test_incorrect_tag_merge_redirect_bio_is_discarded(self, mockGet):
        """Some "+"-containing artist names resolve to Last.fm's own
        "incorrect tag" merge-redirect entity - its "bio" is a boilerplate
        explanation of the mismatch, not a real biography."""
        client, _ = self._client()
        mockGet.return_value = self._infoResponse(
            "This is an incorrect tag for the band Florence + The Machine. "
            "Songs scrobbled to this incorrect tag will automatically redirect.")
        outcome = client.getArtistInfo("Florence + The Machine")
        self.assertEqual(outcome.status, OUTCOME_OK)
        self.assertIsNone(outcome.bio)

    @patch("Database.lastfm.requests.get")
    def test_prefers_content_over_summary_when_both_present(self, mockGet):
        """bio.content is the full, untruncated biography - bio.summary is
        Last.fm's own ~300-char excerpt that cuts off mid-sentence with no
        regard for sentence boundaries (confirmed against the live API)."""
        client, _ = self._client()
        mockGet.return_value = _response(payload={"artist": {"name": "x", "bio": {
            "summary": "A truncated excerpt that stops",
            "content": "The full biography text, complete and unabridged.",
        }}})
        outcome = client.getArtistInfo("x")
        self.assertEqual(outcome.bio, "The full biography text, complete and unabridged.")

    @patch("Database.lastfm.requests.get")
    def test_falls_back_to_summary_when_content_is_missing_or_blank(self, mockGet):
        client, _ = self._client()
        for bioObj in ({"summary": "A short bio."},
                       {"summary": "A short bio.", "content": ""},
                       {"summary": "A short bio.", "content": "   "}):
            with self.subTest(bioObj=bioObj):
                mockGet.return_value = _response(payload={"artist": {"name": "x", "bio": bioObj}})
                outcome = client.getArtistInfo("x")
                self.assertEqual(outcome.bio, "A short bio.")

    @patch("Database.lastfm.requests.get")
    def test_content_under_max_length_is_returned_in_full(self, mockGet):
        client, _ = self._client()
        text = "A short but complete biography of the artist."
        mockGet.return_value = _response(payload={"artist": {"name": "x", "bio": {"content": text}}})
        outcome = client.getArtistInfo("x")
        self.assertEqual(outcome.bio, text)

    @patch("Database.lastfm.requests.get")
    def test_long_bio_truncates_to_the_last_sentence_boundary_within_max_length(self, mockGet):
        client, _ = self._client()
        sentences = [f"This is sentence number {i} in a long biography." for i in range(1, 30)]
        fullText = " ".join(sentences)
        self.assertGreater(len(fullText), lastfm.BIO_MAX_LENGTH)   #< sanity check on the fixture itself
        mockGet.return_value = _response(payload={"artist": {"name": "x", "bio": {"content": fullText}}})

        outcome = client.getArtistInfo("x")

        self.assertLessEqual(len(outcome.bio), lastfm.BIO_MAX_LENGTH)
        self.assertTrue(outcome.bio.endswith("."))
        self.assertTrue(fullText.startswith(outcome.bio))   #< a clean prefix - no mid-sentence cut

    @patch("Database.lastfm.requests.get")
    def test_content_boilerplate_is_stripped_without_a_double_period_artifact(self, mockGet):
        content = ('The artist released several acclaimed albums over a long career. '
                  '<a href="https://www.last.fm/music/Some+Artist">Read more on Last.fm</a>. '
                  'User-contributed text is available under the Creative Commons By-SA License; '
                  'additional terms may apply.')
        client, _ = self._client()
        mockGet.return_value = _response(payload={"artist": {"name": "x", "bio": {"content": content}}})

        outcome = client.getArtistInfo("x")

        self.assertNotIn(" . ", outcome.bio)   #< stripping the link must not leave "sentence. ."
        self.assertNotIn("Read more on Last.fm", outcome.bio)
        self.assertIn("Creative Commons By-SA License", outcome.bio)
        self.assertTrue(outcome.bio.startswith(
            "The artist released several acclaimed albums over a long career."))

    @patch("Database.lastfm.requests.get")
    def test_attribution_is_appended_after_truncation_not_counted_toward_the_budget(self, mockGet):
        client, _ = self._client()
        sentences = [f"This is sentence number {i} in a long biography." for i in range(1, 30)]
        prose = " ".join(sentences)
        content = (prose + ' <a href="https://last.fm/x">Read more on Last.fm</a>. '
                  'User-contributed text is available under the Creative Commons By-SA License; '
                  'additional terms may apply.')
        mockGet.return_value = _response(payload={"artist": {"name": "x", "bio": {"content": content}}})

        outcome = client.getArtistInfo("x")

        self.assertTrue(outcome.bio.endswith(
            "User-contributed text is available under the Creative Commons By-SA License; "
            "additional terms may apply."))
        prosePart = outcome.bio.rsplit("User-contributed", 1)[0].strip()
        self.assertLessEqual(len(prosePart), lastfm.BIO_MAX_LENGTH)
        self.assertTrue(prosePart.endswith("."))

    @patch("Database.lastfm.requests.get")
    def test_missing_or_empty_bio_reads_as_none(self, mockGet):
        client, _ = self._client()
        for payload in ({"artist": {"name": "x"}}, {"artist": {"name": "x", "bio": {}}},
                        {"artist": {"name": "x", "bio": {"summary": ""}}}, {}):
            mockGet.return_value = _response(payload=payload)
            outcome = client.getArtistInfo("x")
            self.assertEqual(outcome.status, OUTCOME_OK)
            self.assertIsNone(outcome.bio)

    @patch("Database.lastfm.requests.get")
    def test_error_6_is_not_found(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = _response(statusCode=400, payload={"error": 6, "message": "not found"})
        self.assertEqual(client.getArtistInfo("zzzz"), ArtistInfoOutcome(OUTCOME_NOT_FOUND, None))

    @patch("Database.lastfm.requests.get")
    def test_invalid_key_and_transient_errors_propagate(self, mockGet):
        client, limiter = self._client()
        mockGet.return_value = _response(statusCode=403, payload={"error": 10})
        self.assertEqual(client.getArtistInfo("x").status, OUTCOME_INVALID_KEY)

        mockGet.return_value = _response(statusCode=500, jsonError=True)
        self.assertEqual(client.getArtistInfo("x").status, OUTCOME_TRANSIENT)

        limiter.applyBackoff.reset_mock()
        mockGet.return_value = _response(payload={"error": 29})
        self.assertEqual(client.getArtistInfo("x").status, OUTCOME_TRANSIENT)
        limiter.applyBackoff.assert_called_once_with(LASTFM_RATE_LIMIT_BACKOFF_SECONDS)

    @patch("Database.lastfm.requests.get")
    def test_aborted_rate_limit_acquire_makes_no_request(self, mockGet):
        client, limiter = self._client()
        limiter.acquire.return_value = False
        self.assertIsNone(client.getArtistInfo("x", stop_event=threading.Event()))
        mockGet.assert_not_called()

    @patch("Database.lastfm.requests.get")
    def test_network_failure_is_transient(self, mockGet):
        client, _ = self._client()
        import requests as requestsModule
        mockGet.side_effect = requestsModule.exceptions.ConnectionError("boom")
        self.assertEqual(client.getArtistInfo("x"), ArtistInfoOutcome(OUTCOME_TRANSIENT, None))


class AlbumInfoBioTestCase(unittest.TestCase):
    """getAlbumInfo (album.getinfo) for the album-bio feature: same cleaning
    pipeline as ArtistInfoBioTestCase, reading the wiki field instead of
    bio, plus the artist+album request shape."""

    def _client(self, acquireResult=True):
        limiter = MagicMock()
        limiter.acquire.return_value = acquireResult
        return LastfmClient("test-key", rateLimiter=limiter), limiter

    @staticmethod
    def _infoResponse(wikiSummary):
        return _response(payload={"album": {"name": "Some Album",
                                             "wiki": {"summary": wikiSummary}}})

    @patch("Database.lastfm.requests.get")
    def test_request_carries_key_format_autocorrect_artist_and_album(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = self._infoResponse("A short wiki entry.")
        client.getAlbumInfo("Radiohead", "OK Computer")
        params = mockGet.call_args.kwargs["params"]
        self.assertEqual(params["method"], "album.getinfo")
        self.assertEqual(params["artist"], "Radiohead")
        self.assertEqual(params["album"], "OK Computer")
        self.assertEqual(params["api_key"], "test-key")
        self.assertEqual(params["autocorrect"], "1")

    @patch("Database.lastfm.requests.get")
    def test_plain_bio_is_returned_as_is(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = self._infoResponse("A landmark rock album.")
        outcome = client.getAlbumInfo("Radiohead", "OK Computer")
        self.assertEqual(outcome.status, OUTCOME_OK)
        self.assertEqual(outcome.bio, "A landmark rock album.")

    @patch("Database.lastfm.requests.get")
    def test_embedded_html_is_stripped_to_plain_text(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = self._infoResponse(
            'A landmark album. <a href="https://last.fm/x">Read more on Last.fm</a>. '
            "User-contributed text is available under the Creative Commons "
            "By-SA License; additional terms may apply.")
        outcome = client.getAlbumInfo("Radiohead", "OK Computer")
        self.assertNotIn("<a", outcome.bio)
        self.assertNotIn("Read more on Last.fm", outcome.bio)
        self.assertIn("Creative Commons By-SA License", outcome.bio)
        self.assertIn("A landmark album.", outcome.bio)

    @patch("Database.lastfm.requests.get")
    def test_incorrect_tag_merge_redirect_bio_is_discarded(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = self._infoResponse(
            "This is an incorrect tag for the album Some Album. "
            "Songs scrobbled to this incorrect tag will automatically redirect.")
        outcome = client.getAlbumInfo("x", "Some Album")
        self.assertEqual(outcome.status, OUTCOME_OK)
        self.assertIsNone(outcome.bio)

    @patch("Database.lastfm.requests.get")
    def test_prefers_content_over_summary_when_both_present(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = _response(payload={"album": {"name": "x", "wiki": {
            "summary": "A truncated excerpt that stops",
            "content": "The full album wiki text, complete and unabridged.",
        }}})
        outcome = client.getAlbumInfo("x", "x")
        self.assertEqual(outcome.bio, "The full album wiki text, complete and unabridged.")

    @patch("Database.lastfm.requests.get")
    def test_long_bio_truncates_to_the_last_sentence_boundary_within_max_length(self, mockGet):
        client, _ = self._client()
        sentences = [f"This is sentence number {i} about the album." for i in range(1, 30)]
        fullText = " ".join(sentences)
        self.assertGreater(len(fullText), lastfm.BIO_MAX_LENGTH)
        mockGet.return_value = _response(payload={"album": {"name": "x", "wiki": {"content": fullText}}})

        outcome = client.getAlbumInfo("x", "x")

        self.assertLessEqual(len(outcome.bio), lastfm.BIO_MAX_LENGTH)
        self.assertTrue(outcome.bio.endswith("."))
        self.assertTrue(fullText.startswith(outcome.bio))

    @patch("Database.lastfm.requests.get")
    def test_missing_or_empty_bio_reads_as_none(self, mockGet):
        client, _ = self._client()
        for payload in ({"album": {"name": "x"}}, {"album": {"name": "x", "wiki": {}}},
                        {"album": {"name": "x", "wiki": {"summary": ""}}}, {}):
            mockGet.return_value = _response(payload=payload)
            outcome = client.getAlbumInfo("x", "x")
            self.assertEqual(outcome.status, OUTCOME_OK)
            self.assertIsNone(outcome.bio)

    @patch("Database.lastfm.requests.get")
    def test_error_6_is_not_found(self, mockGet):
        client, _ = self._client()
        mockGet.return_value = _response(statusCode=400, payload={"error": 6, "message": "not found"})
        self.assertEqual(client.getAlbumInfo("x", "zzzz"), AlbumInfoOutcome(OUTCOME_NOT_FOUND, None))

    @patch("Database.lastfm.requests.get")
    def test_invalid_key_and_transient_errors_propagate(self, mockGet):
        client, limiter = self._client()
        mockGet.return_value = _response(statusCode=403, payload={"error": 10})
        self.assertEqual(client.getAlbumInfo("x", "x").status, OUTCOME_INVALID_KEY)

        mockGet.return_value = _response(statusCode=500, jsonError=True)
        self.assertEqual(client.getAlbumInfo("x", "x").status, OUTCOME_TRANSIENT)

        limiter.applyBackoff.reset_mock()
        mockGet.return_value = _response(payload={"error": 29})
        self.assertEqual(client.getAlbumInfo("x", "x").status, OUTCOME_TRANSIENT)
        limiter.applyBackoff.assert_called_once_with(LASTFM_RATE_LIMIT_BACKOFF_SECONDS)

    @patch("Database.lastfm.requests.get")
    def test_aborted_rate_limit_acquire_makes_no_request(self, mockGet):
        client, limiter = self._client()
        limiter.acquire.return_value = False
        self.assertIsNone(client.getAlbumInfo("x", "x", stop_event=threading.Event()))
        mockGet.assert_not_called()

    @patch("Database.lastfm.requests.get")
    def test_network_failure_is_transient(self, mockGet):
        client, _ = self._client()
        import requests as requestsModule
        mockGet.side_effect = requestsModule.exceptions.ConnectionError("boom")
        self.assertEqual(client.getAlbumInfo("x", "x"), AlbumInfoOutcome(OUTCOME_TRANSIENT, None))


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
