"""Behavior tests for Database.Spotify.client - the retry ladder, the
degraded-response fallback, limiter wiring, and the dependency sanity checks
the client's design rests on.

These began life in tests/test_patches.py, written against the monkey-patched
spotipyFree wrapper; the Phase 1.5 cutover re-pointed them at the owned client
(dependencyRewritePlan.md). The shape contract itself lives in
tests/test_spotify_client_contract.py - this file covers behavior under
failure, concurrency and rate limiting.
"""

import unittest
from unittest.mock import MagicMock, patch
import concurrent.futures
import spotapi
import spotapi.public

import Database.rate_limit as rateLimitModule
from Database.Spotify.client import Spotify


def fakeTrackUnion(trackId):
    """Minimal raw trackUnion shape (spotapi's GraphQL response format) - just
    enough fields for SpotifyFormatter.formatTrack/formatArtists to succeed."""
    return {
        "uri": f"spotify:track:{trackId}",
        "name": f"Song {trackId}",
        "duration": {"totalMilliseconds": 200000},
        "contentRating": {"label": "NONE"},
        "firstArtist": {"items": []},
        "otherArtists": {"items": []},
    }


class TestClientDependencySanity(unittest.TestCase):
    """Checks on spotapi itself that the client's design rests on - if an
    upgrade changes either, the matching workaround needs revisiting."""

    def test_config_client_default_is_shared_singleton(self):
        """Sanity check on the dependency itself: spotapi.Config's `client`
        field is declared as `field(default=TLSClient(...))` rather than
        `field(default_factory=...)`. dataclasses only rejects known mutable
        defaults (list/dict/set), so this TLSClient instance is built once at
        import time and silently reused as the default for every Config()
        call that omits client= - the exact footgun Spotify.login()
        works around. If a future spotapi upgrade switches this to a
        default_factory, this test should fail to flag that the workaround
        is no longer needed."""
        cfgA = spotapi.Config(logger=spotapi.Logger())
        cfgB = spotapi.Config(logger=spotapi.Logger())
        self.assertIs(cfgA.client, cfgB.client)

    def test_public_song_info_uses_locked_pool_not_shared_default(self):
        """Sanity check on the dependency itself: spotapi.public.Pooler (what
        spotapi.Public.song_info checks clients out of) must hand out distinct
        objects until one is returned, rather than one shared instance. If a
        future spotapi upgrade changes this, the thread-safety assumption behind
        track() no longer holds and this test should fail to flag it."""
        pool = spotapi.public.Pooler(factory=object)
        first = pool.get()
        second = pool.get()
        self.assertIsNot(first, second)

        pool.put(first)
        third = pool.get()
        self.assertIs(third, first)


class TestTrackFetchPath(unittest.TestCase):
    """track() must use the race-free pooled fetch path."""

    def _newSpotifyInstance(self):
        return Spotify()  #< no cookies file: builds offline, user_auth stays False

    @patch("spotapi.Song")
    @patch("spotapi.Public")
    def test_spotify_track_uses_public_song_info_not_song(self, mock_public, mock_song):
        """track() must fetch metadata via spotapi.Public's locked client pool,
        not spotapi.Song()'s process-wide shared-default client."""
        mock_public.song_info.return_value = {"data": {"trackUnion": fakeTrackUnion("abc123")}}

        instance = self._newSpotifyInstance()
        result = instance.track("abc123")

        mock_public.song_info.assert_called_once_with("abc123")
        mock_song.assert_not_called()
        self.assertEqual(result["id"], "abc123")
        self.assertEqual(result["name"], "Song abc123")

    @patch("spotapi.Song")
    @patch("spotapi.Public")
    def test_spotify_track_concurrent_calls_do_not_cross_contaminate(self, mock_public, mock_song):
        """Regression test for the race this patch fixes: the original
        implementation shared one spotapi.Song() client across every thread, so
        concurrent track() calls (as the importer's ThreadPoolExecutor pre-fetch
        issues) could authenticate/return data for the wrong track. With the
        pooled path, each call must still resolve to exactly the track it asked for,
        and the unsafe spotapi.Song() path must never be touched."""
        mock_public.song_info.side_effect = lambda trackId: {
            "data": {"trackUnion": fakeTrackUnion(trackId)}
        }

        instance = self._newSpotifyInstance()
        trackIds = [f"track{i}" for i in range(50)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(instance.track, trackIds))

        for trackId, result in zip(trackIds, results):
            self.assertEqual(result["id"], trackId)
        mock_song.assert_not_called()


class TestIncompleteTrackInfo(unittest.TestCase):
    """spotapi's song_info can come back degraded in three different shapes, all
    seen in Database/Data/app.log over 2026-07-16. Each one killed the whole
    recently-played callback iteration, so the play was dropped entirely:

      data=None                 -> TypeError: 'NoneType' object is not subscriptable
      trackUnion without "uri"  -> KeyError: 'uri', raised deep inside
                                   SpotipyFree/Formatter.py where the track id
                                   is no longer in scope
      spotapi's own SongError   -> raised on the FIRST attempt, because the
                                   substring classifier saw neither "rate
                                   limit" nor "session" in it

    getTrackInfoWithRetry now recognises the first two as one typed error
    at our own seam, retries it a bounded number of times, and Spotify.track()
    degrades to a fallback record only once those are exhausted - so the play
    survives either way. The third is a failed HTTP request (spotapi raises it
    only from `if resp.fail`), so it goes to the transient ladder that already
    existed for exactly that, matched by type rather than by message."""

    def setUp(self):
        """The incomplete-response retry sleeps between attempts; patch it away
        class-wide so no test in here pays real seconds, and so a test that
        forgets can't quietly add them."""
        sleepPatcher = patch("Database.Spotify.client.time.sleep")
        self.mockSleep = sleepPatcher.start()
        self.addCleanup(sleepPatcher.stop)

        # These tests are about the retry BUDGETS, but a "429 rate limit" in
        # their scripts now also opens a real process-wide backoff window (see
        # TestSharedLimiterWiring) that the next attempt would wait out. Worse,
        # the sleep patch above is the one the limiter's own wait uses, so it
        # would spin out the full timeout rather than sleep it. Grant every
        # slot here; the shared limiter has its own coverage elsewhere.
        acquirePatcher = patch.object(rateLimitModule.SPOTIFY_LIMITER, "acquire", return_value=True)
        acquirePatcher.start()
        self.addCleanup(acquirePatcher.stop)

    def _newSpotifyInstance(self):
        return Spotify()  #< no cookies file: builds offline, user_auth stays False

    @patch("spotapi.Public")
    def test_none_data_raises_typed_error_naming_the_track(self, mock_public):
        from Database.Spotify.client import getTrackInfoWithRetry, IncompleteTrackInfoError

        mock_public.song_info.return_value = {"data": None}

        with self.assertRaises(IncompleteTrackInfoError) as ctx:
            getTrackInfoWithRetry("deadbeef")
        self.assertIn("deadbeef", str(ctx.exception))

    @patch("spotapi.Public")
    def test_none_trackUnion_raises_typed_error(self, mock_public):
        from Database.Spotify.client import getTrackInfoWithRetry, IncompleteTrackInfoError

        mock_public.song_info.return_value = {"data": {"trackUnion": None}}

        with self.assertRaises(IncompleteTrackInfoError):
            getTrackInfoWithRetry("deadbeef")

    @patch("spotapi.Public")
    def test_trackUnion_without_uri_raises_typed_error(self, mock_public):
        """The KeyError case: trackUnion IS a dict, so no null check catches it.
        Only a shape check does."""
        from Database.Spotify.client import getTrackInfoWithRetry, IncompleteTrackInfoError

        union = fakeTrackUnion("abc123")
        del union["uri"]
        mock_public.song_info.return_value = {"data": {"trackUnion": union}}

        with self.assertRaises(IncompleteTrackInfoError):
            getTrackInfoWithRetry("abc123")

    @patch("spotapi.Public")
    def test_blank_uri_is_also_incomplete(self, mock_public):
        from Database.Spotify.client import getTrackInfoWithRetry, IncompleteTrackInfoError

        union = fakeTrackUnion("abc123")
        union["uri"] = ""
        mock_public.song_info.return_value = {"data": {"trackUnion": union}}

        with self.assertRaises(IncompleteTrackInfoError):
            getTrackInfoWithRetry("abc123")

    @patch("spotapi.Public")
    def test_complete_response_is_returned_unchanged(self, mock_public):
        from Database.Spotify.client import getTrackInfoWithRetry

        union = fakeTrackUnion("abc123")
        mock_public.song_info.return_value = {"data": {"trackUnion": union}}

        self.assertIs(getTrackInfoWithRetry("abc123"), union)

    @patch("spotapi.Public")
    def test_incomplete_info_is_retried_before_giving_up(self, mock_public):
        """Originally this asserted the opposite - a degraded response was
        treated as a fact about the track, not a transient failure. The log
        says otherwise: all 11 incomplete responses in 11 days fall inside one
        4m47s window (2026-07-16 12:59:44-13:04:31), alongside 14 session and
        websocket failures. That's one degraded Spotify session, not 11
        undescribable tracks, so it belongs in the same transient class the
        retry ladder already exists for."""
        from Database.Spotify.client import (
            getTrackInfoWithRetry, IncompleteTrackInfoError,
            INCOMPLETE_TRACK_INFO_RETRIES,
        )

        mock_public.song_info.return_value = {"data": None}

        with self.assertRaises(IncompleteTrackInfoError):
            getTrackInfoWithRetry("abc123")

        self.assertEqual(mock_public.song_info.call_count, INCOMPLETE_TRACK_INFO_RETRIES + 1)

    @patch("spotapi.Public")
    def test_a_retry_that_succeeds_returns_the_track(self, mock_public):
        """The whole point: a track that was undescribable during the blip is
        described a moment later, and no fallback row is ever created."""
        from Database.Spotify.client import getTrackInfoWithRetry

        union = fakeTrackUnion("abc123")
        mock_public.song_info.side_effect = [{"data": None}, {"data": {"trackUnion": union}}]

        self.assertIs(getTrackInfoWithRetry("abc123"), union)

    @patch("spotapi.Public")
    def test_incomplete_retry_uses_a_short_fixed_delay(self, mock_public):
        """Not the transient ladder's 1/2/4s exponential backoff: this runs
        inside the poll loop's callback, and a burst means every affected track
        pays the wait."""
        from Database.Spotify.client import (
            getTrackInfoWithRetry, IncompleteTrackInfoError,
            INCOMPLETE_TRACK_INFO_RETRY_DELAY_SECONDS,
        )

        mock_public.song_info.return_value = {"data": None}

        with self.assertRaises(IncompleteTrackInfoError):
            getTrackInfoWithRetry("abc123")

        self.assertTrue(self.mockSleep.called)
        for call in self.mockSleep.call_args_list:
            self.assertEqual(call.args[0], INCOMPLETE_TRACK_INFO_RETRY_DELAY_SECONDS)

    @patch("spotapi.Public")
    def test_incomplete_retries_do_not_consume_the_transient_budget(self, mock_public):
        """The two failure modes get separate budgets - an incomplete response
        followed by a rate limit must still get the transient ladder's own
        attempts, or a blip early in a fetch would silently shorten the
        recovery window for a completely different problem."""
        from Database.Spotify.client import getTrackInfoWithRetry

        union = fakeTrackUnion("abc123")
        mock_public.song_info.side_effect = [
            {"data": None},                       #< incomplete: costs an incomplete retry
            Exception("429 rate limit"),          #< transient: attempt 1
            Exception("429 rate limit"),          #< transient: attempt 2
            {"data": {"trackUnion": union}},      #< transient: attempt 3 succeeds
        ]

        self.assertIs(getTrackInfoWithRetry("abc123"), union)

    @patch("spotapi.Public")
    def test_a_failed_request_is_retried_like_any_other_transport_blip(self, mock_public):
        """The third shape in this class's docstring. spotapi raises
        SongError("Could not get song info", error=...) from exactly one place:
        `if resp.fail` in Song.get_track_info - i.e. the HTTP request failed. It
        is a transport failure, but the substring classifier matched neither
        "rate limit" nor "session" on it, so it was re-raised on the FIRST
        attempt, propagated through _addToRecentlyPlayed, and killed the whole
        poll iteration. Five plays went that way in the audited window, and the
        retry ladder that exists for precisely this never ran."""
        from spotapi.exceptions import SongError
        from Database.Spotify.client import getTrackInfoWithRetry

        union = fakeTrackUnion("abc123")
        mock_public.song_info.side_effect = [
            SongError("Could not get song info", error="502 Bad Gateway"),
            {"data": {"trackUnion": union}},
        ]

        self.assertIs(getTrackInfoWithRetry("abc123"), union)

    @patch("spotapi.Public")
    def test_a_request_that_keeps_failing_still_raises(self, mock_public):
        """Retrying is the fix, not swallowing: a genuine, persistent failure
        must still reach the caller rather than silently becoming a fallback
        record that claims the track is undescribable."""
        from spotapi.exceptions import SongError
        from Database.Spotify.client import getTrackInfoWithRetry

        mock_public.song_info.side_effect = SongError("Could not get song info", error="502")

        with self.assertRaises(SongError):
            getTrackInfoWithRetry("abc123")

    @patch("spotapi.Public")
    def test_a_non_transport_spotapi_error_is_not_retried(self, mock_public):
        """The classification stays narrow - an unrecognised error is still a
        fact about the request, raised on the first attempt."""
        from Database.Spotify.client import getTrackInfoWithRetry

        mock_public.song_info.side_effect = ValueError("something else entirely")

        with self.assertRaises(ValueError):
            getTrackInfoWithRetry("abc123")
        self.assertEqual(mock_public.song_info.call_count, 1)

    @patch("spotapi.Public")
    def test_exhausted_incomplete_retries_still_reach_the_fallback(self, mock_public):
        """Retrying reduces how often a fallback is created; it must not remove
        the safety net when Spotify really has nothing."""
        mock_public.song_info.return_value = {"data": None}

        result = self._newSpotifyInstance().track("abc123")

        self.assertEqual(result["track_id"], "abc123")



class TestTrackFallbackOnIncompleteInfo(unittest.TestCase):
    """Spotify.track() degrades to a minimal fallback record rather than raising
    when Spotify can't describe a track. Raising propagates through
    SpotipyFree's _addToRecentlyPlayed and kills the poll iteration, losing a
    play that really happened - 11 plays went that way over 11 days.

    The record is marked RESTRICTED_FALLBACK_REASON (not SYNTHETIC): the track
    id is real, so the Spotify link is real too, and upsertTrack is explicitly
    built never to let a fallback overwrite metadata that arrives later."""

    def setUp(self):
        """The incomplete-response retry sleeps between attempts; patch it away
        class-wide so no test in here pays real seconds, and so a test that
        forgets can't quietly add them."""
        sleepPatcher = patch("Database.Spotify.client.time.sleep")
        self.mockSleep = sleepPatcher.start()
        self.addCleanup(sleepPatcher.stop)

        # These tests are about the retry BUDGETS, but a "429 rate limit" in
        # their scripts now also opens a real process-wide backoff window (see
        # TestSharedLimiterWiring) that the next attempt would wait out. Worse,
        # the sleep patch above is the one the limiter's own wait uses, so it
        # would spin out the full timeout rather than sleep it. Grant every
        # slot here; the shared limiter has its own coverage elsewhere.
        acquirePatcher = patch.object(rateLimitModule.SPOTIFY_LIMITER, "acquire", return_value=True)
        acquirePatcher.start()
        self.addCleanup(acquirePatcher.stop)

    def _newSpotifyInstance(self):
        return Spotify()  #< no cookies file: builds offline, user_auth stays False

    def _track(self, mock_public, payload, trackId="abc123"):
        mock_public.song_info.return_value = payload
        return self._newSpotifyInstance().track(trackId)

    @patch("spotapi.Public")
    def test_incomplete_track_returns_fallback_instead_of_raising(self, mock_public):
        result = self._track(mock_public, {"data": None})

        self.assertEqual(result["track_id"], "abc123")
        self.assertEqual(result["id"], "abc123")

    @patch("spotapi.Public")
    def test_fallback_keeps_the_real_spotify_link(self, mock_public):
        """The id is real even when the metadata isn't, so the link must work -
        only fabricated ids get an empty url."""
        result = self._track(mock_public, {"data": {"trackUnion": None}})

        self.assertEqual(result["external_urls"]["spotify"],
                         "https://open.spotify.com/track/abc123")

    @patch("spotapi.Public")
    def test_fallback_is_marked_as_a_fallback(self, mock_public):
        """The marker is what stops the degraded row from being mistaken for
        real metadata, both in the UI badge and in upsertTrack's overwrite
        guard."""
        from Database.db import RESTRICTED_FALLBACK_REASON

        result = self._track(mock_public, {"data": {"trackUnion": None}})

        self.assertEqual(result["created_reason"], RESTRICTED_FALLBACK_REASON)

    @patch("spotapi.Public")
    def test_fallback_records_why_it_is_unplayable(self, mock_public):
        """playability is the field Client.formatTrack turns into
        tracks.availability_reason, which is what the UI badges on."""
        from Database.Spotify.client import TRACK_INFO_UNAVAILABLE_REASON

        result = self._track(mock_public, {"data": None})

        self.assertEqual(result["playability"]["playable"], False)
        self.assertEqual(result["playability"]["reason"], TRACK_INFO_UNAVAILABLE_REASON)

    @patch("spotapi.Public")
    def test_fallback_invents_no_facts(self, mock_public):
        """No fabricated duration or artists - a made-up number reads as real
        metadata downstream."""
        union = fakeTrackUnion("abc123")
        del union["uri"]

        result = self._track(mock_public, {"data": {"trackUnion": union}})

        self.assertEqual(result["duration_ms"], 0)
        self.assertEqual(result["artists"], [])

    @patch("spotapi.Public")
    def test_fallback_title_is_the_shared_placeholder_not_blank(self, mock_public):
        """A blank name rendered as an empty row in every list the track
        appeared in. The placeholder is safe because upsertTrack replaces a
        fallback row's name unconditionally once real metadata arrives, so it
        never blocks the repair - and it's the same constant Client.formatTrack
        defaults to, so the two can't drift."""
        from Database.db import UNKNOWN_TRACK_NAME
        from Database.Formatters.spotifyClient import Client

        result = self._track(mock_public, {"data": None})

        self.assertEqual(result["name"], UNKNOWN_TRACK_NAME)
        formatted = Client.formatTrack(result, timestamp=1000, msPlayed=5000)
        self.assertEqual(formatted["name"], UNKNOWN_TRACK_NAME)

    @patch("spotapi.Public")
    def test_fallback_album_name_is_the_placeholder_not_blank(self, mock_public):
        """The same reasoning as the title above, applied to the album the record
        has to invent - and UNKNOWN_ALBUM_NAME exists for exactly this ("Companion
        for the per-track album a fallback record has to invent", Database/db.py).

        The album dict passed "" and _formatAlbum reads .get("name", "Unknown
        album"), so the DEFAULT never applied - the key was present - and
        albums.name was stored empty: a blank album name on the song's detail page
        and in every album link. migrate1_43_0 already uses the constant for this
        identical placeholder shape."""
        from Database.db import UNKNOWN_ALBUM_NAME
        from Database.Formatters.spotifyClient import Client

        result = self._track(mock_public, {"data": None})

        self.assertEqual(result["album"]["name"], UNKNOWN_ALBUM_NAME)
        formatted = Client.formatTrack(result, timestamp=1000, msPlayed=5000)
        self.assertEqual(formatted["album"]["name"], UNKNOWN_ALBUM_NAME)

    @patch("spotapi.Public")
    def test_fallback_album_is_per_track_not_shared(self, mock_public):
        """Two unrelated incomplete tracks must not be merged under one
        "Unknown album" - a shared fabricated album id would link strangers'
        tracks together on the album page."""
        first = self._track(mock_public, {"data": None}, trackId="aaa")
        second = self._track(mock_public, {"data": None}, trackId="bbb")

        self.assertNotEqual(first["album"]["id"], second["album"]["id"])

    @patch("spotapi.Public")
    def test_fallback_survives_the_apps_own_formatter(self, mock_public):
        """The record's whole purpose is to reach the database, and it gets
        there through Client.formatTrack - which indexes track["id"] and
        track["external_urls"]["spotify"] directly."""
        from Database.Formatters.spotifyClient import Client
        from Database.db import RESTRICTED_FALLBACK_REASON

        raw = self._track(mock_public, {"data": None})
        formatted = Client.formatTrack(raw, timestamp=1000, msPlayed=5000)

        self.assertEqual(formatted["id"], "abc123")
        self.assertEqual(formatted["url"], "https://open.spotify.com/track/abc123")
        self.assertEqual(formatted["created_reason"], RESTRICTED_FALLBACK_REASON)
        self.assertEqual(formatted["availability_reason"], "TRACK_INFO_UNAVAILABLE")

    @patch("spotapi.Public")
    def test_fallback_is_logged_with_the_track_id(self, mock_public):
        with self.assertLogs("Database.Spotify.client", level="WARNING") as logCapture:
            self._track(mock_public, {"data": None})

        self.assertTrue(any("abc123" in m for m in logCapture.output))

    @patch("spotapi.Public")
    def test_real_transport_failures_still_raise(self, mock_public):
        """Only an incomplete *response* degrades. A genuine exception out of
        spotapi is still an error - recording a fallback for every network blip
        would poison the catalog with rows that look definitively unavailable."""
        mock_public.song_info.side_effect = RuntimeError("connection reset")

        with self.assertRaises(RuntimeError):
            self._newSpotifyInstance().track("abc123")

    @patch("spotapi.Public")
    def test_complete_track_is_unaffected(self, mock_public):
        result = self._track(mock_public, {"data": {"trackUnion": fakeTrackUnion("abc123")}})

        self.assertEqual(result["name"], "Song abc123")
        self.assertIsNone(result.get("created_reason"))



class TestTrackFetchLimiting(unittest.TestCase):
    """Track metadata rides the same budget, but with a much longer patience:
    giving up here loses a play that really happened."""

    def test_track_fetch_takes_a_slot(self):
        from Database.Spotify.client import getTrackInfoWithRetry
        from Database.rate_limit import SPOTIFY_LIMITER

        with patch("spotapi.Public.song_info", return_value={"data": {"trackUnion": fakeTrackUnion("t1")}}):
            with patch.object(SPOTIFY_LIMITER, "acquire", return_value=True) as acquire:
                getTrackInfoWithRetry("t1")

        acquire.assert_called_once()

    def test_track_fetch_waits_out_a_whole_penalty_window(self):
        """A short polling timeout here would drop plays, so this call site
        gets the longer one."""
        from Database.Spotify.client import getTrackInfoWithRetry
        from Database.rate_limit import SPOTIFY_LIMITER, SPOTIFY_TRACK_ACQUIRE_TIMEOUT_SECONDS

        with patch("spotapi.Public.song_info", return_value={"data": {"trackUnion": fakeTrackUnion("t1")}}):
            with patch.object(SPOTIFY_LIMITER, "acquire", return_value=True) as acquire:
                getTrackInfoWithRetry("t1")

        self.assertEqual(acquire.call_args.kwargs["timeout"], SPOTIFY_TRACK_ACQUIRE_TIMEOUT_SECONDS)

    def test_a_refused_slot_is_retried_not_raised_immediately(self):
        """SpotifyLocallyRateLimitedError is the most transient failure there
        is - nothing was even sent - so it must ride the existing ladder."""
        from Database.Spotify.client import getTrackInfoWithRetry
        from Database.rate_limit import SPOTIFY_LIMITER

        songInfo = MagicMock(return_value={"data": {"trackUnion": fakeTrackUnion("t1")}})
        with patch("spotapi.Public.song_info", songInfo):
            with patch.object(SPOTIFY_LIMITER, "acquire", side_effect=[False, True]):
                with patch("time.sleep"):
                    track = getTrackInfoWithRetry("t1")

        self.assertEqual(track["uri"], "spotify:track:t1")
        songInfo.assert_called_once_with("t1")   #< the refused attempt sent nothing

    def test_a_refused_slot_never_re_arms_the_window_it_tripped_over(self):
        """Regression: the refusal message says "rate limit" too, so the
        substring classifier here counted our own open window as Spotify
        pushback and extended it - the same feedback loop the listener had
        (2026-07-29 17:43). Matched by type, ahead of the strings."""
        from Database.Spotify.client import getTrackInfoWithRetry
        from Database.rate_limit import SPOTIFY_LIMITER

        songInfo = MagicMock(return_value={"data": {"trackUnion": fakeTrackUnion("t1")}})
        with patch("spotapi.Public.song_info", songInfo):
            with patch.object(SPOTIFY_LIMITER, "acquire", side_effect=[False, True]):
                with patch("time.sleep"):
                    getTrackInfoWithRetry("t1")

        self.assertEqual(SPOTIFY_LIMITER.snapshot()["backoffs"], 0)

    def test_a_spotify_rate_limit_pauses_every_spotify_request(self):
        from Database.Spotify.client import getTrackInfoWithRetry, ENDPOINT_TRACK_INFO
        from Database.rate_limit import SPOTIFY_LIMITER

        with patch("spotapi.Public.song_info", side_effect=Exception("429 rate limit exceeded")):
            with patch("time.sleep"):
                with self.assertRaisesRegex(Exception, "429"):
                    getTrackInfoWithRetry("t1", max_retries=1)

        snapshot = SPOTIFY_LIMITER.snapshot()
        self.assertGreaterEqual(snapshot["backoffs"], 1)
        self.assertEqual(snapshot["lastReason"], ENDPOINT_TRACK_INFO)

    def test_a_real_404_still_raises_without_pausing_anything(self):
        from Database.Spotify.client import getTrackInfoWithRetry
        from Database.rate_limit import SPOTIFY_LIMITER

        with patch("spotapi.Public.song_info", side_effect=Exception("404 not found")):
            with self.assertRaisesRegex(Exception, "404"):
                getTrackInfoWithRetry("t1")

        self.assertEqual(SPOTIFY_LIMITER.snapshot()["backoffs"], 0)



if __name__ == "__main__":
    unittest.main()
