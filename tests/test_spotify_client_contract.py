"""Contract tests for Database.Spotify - the owned replacement for spotipyFree.

Written BEFORE the implementation (dependencyRewritePlan.md Phase 1.1). Every
expectation here is derived from a CONSUMER of the client, not from what the
current spotipyFree wrapper happens to return - a snapshot of today's output
would enshrine today's bugs. The consumers, and what each one reads:

  Database/Formatters/spotifyClient.py  Client.formatTrack hard-subscripts
                                        track["id"] and
                                        track["external_urls"]["spotify"]; reads
                                        name, duration_ms, artists[], album{},
                                        explicit, external_ids.isrc,
                                        disc_number, track_number, playability,
                                        created_reason via .get().
  Database/workers/listener.py          item["track"].get("duration_ms"/"id"/
                                        "name"), item["played_at"],
                                        item["ms_played"], item["context"].
  Database/Listeners/spotifyListener.py current_user().get("id"/"email"),
                                        album()/playlist().get("name"),
                                        current_user_recently_played() items'
                                        "played_at" membership (getNewItems),
                                        sp.user_auth bool sentinel,
                                        sp.lastPlayedManager.{manager,run,thread}.
  Database/Importers/StreamingHistoryImporter.py
                                        search(...)["tracks"]["items"],
                                        track(<spotify:track:URI or id>).

The GraphQL fixtures below describe Spotify's wire format (getTrack's
trackUnion, searchDesktop items, account-settings profile) - interop facts,
NOT spotipyFree's code. Field names come from the pathfinder API itself.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent

from Database.db import UNKNOWN_TRACK_NAME, UNKNOWN_ALBUM_NAME, RESTRICTED_FALLBACK_REASON


TRACK_ID = "6rqhFgbbKwnb9MLmUQDhG6"
ALBUM_ID = "4LH4d3cOWNNsVw41Gqt2kv"
ARTIST_ID = "0OdUWJ0sBjDrqHygGUXeCF"
PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"


def fakeTrackUnion(trackId=TRACK_ID, *, explicitLabel="NONE", playable=True):
    """A getTrack response's trackUnion, as the pathfinder API shapes it.

    coverArt sources are deliberately ordered smallest-first: the contract
    requires images[0] to be the LARGEST (Client.formatTrack takes images[0]
    as the artwork), so the formatter must sort rather than trust wire order.
    """
    return {
        "__typename": "Track",
        "uri": f"spotify:track:{trackId}",
        "name": "Fixture Song",
        "duration": {"totalMilliseconds": 200040},
        "trackNumber": 5,
        "discNumber": 1,
        "contentRating": {"label": explicitLabel},
        "playability": {"playable": playable,
                        "reason": "PLAYABLE" if playable else "COUNTRY_RESTRICTED"},
        "albumOfTrack": {
            "uri": f"spotify:album:{ALBUM_ID}",
            "name": "Fixture Album",
            "date": {"isoString": "2021-05-07T00:00:00Z", "precision": "DAY"},
            "coverArt": {"sources": [
                {"url": "https://i.scdn.co/image/small", "width": 64, "height": 64},
                {"url": "https://i.scdn.co/image/large", "width": 640, "height": 640},
                {"url": "https://i.scdn.co/image/mid", "width": 300, "height": 300},
            ]},
            "tracks": {"totalCount": 12},
        },
        "firstArtist": {"items": [
            {"uri": f"spotify:artist:{ARTIST_ID}", "profile": {"name": "First Artist"}},
        ]},
        "otherArtists": {"items": [
            {"uri": "spotify:artist:2ndArtistId0000000000x", "profile": {"name": "Second Artist"}},
        ]},
    }


def fakeSearchPage(trackId=TRACK_ID):
    """One page of searchDesktop results, as Public().song_search yields them:
    a list of {"item": {"data": <track>}} wrappers. Search tracks carry a flat
    "artists" collection (not firstArtist/otherArtists) - that difference in
    wire shape is exactly why the formatter, not the caller, absorbs it."""
    track = fakeTrackUnion(trackId)
    track["artists"] = {"items": track.pop("firstArtist")["items"] + track.pop("otherArtists")["items"]}
    notATrack = {"item": {"data": {"__typename": "Album", "uri": f"spotify:album:{ALBUM_ID}"}}}
    return [{"item": {"data": track}}, notATrack]


def buildClient():
    """A Spotify client with no cookies file: user_auth stays False, nothing
    touches the network. Mirrors test_patches' _newSpotifyInstance."""
    from Database.Spotify import Spotify
    return Spotify()


class TestTrackContract(unittest.TestCase):
    """track() output must satisfy Client.formatTrack's reads - the hard
    subscripts especially - and its input must accept every id form the app
    passes: bare ids (listener), spotify:track: URIs (importer, straight from
    export files), and open.spotify.com URLs."""

    def _fetch(self, mock_public, union=None, trackId=TRACK_ID):
        mock_public.song_info.return_value = {"data": {"trackUnion": union or fakeTrackUnion(trackId)}}
        return buildClient().track(trackId)

    @patch("spotapi.Public")
    def test_hard_required_keys(self, mock_public):
        """Client.formatTrack subscripts these without .get(); their absence is
        a crash in the play-recording path."""
        track = self._fetch(mock_public)
        self.assertEqual(track["id"], TRACK_ID)
        self.assertEqual(track["external_urls"]["spotify"],
                         f"https://open.spotify.com/track/{TRACK_ID}")

    @patch("spotapi.Public")
    def test_full_consumer_key_set(self, mock_public):
        track = self._fetch(mock_public)
        self.assertEqual(track["name"], "Fixture Song")
        self.assertEqual(track["duration_ms"], 200040)
        self.assertEqual(track["disc_number"], 1)
        self.assertEqual(track["track_number"], 5)
        self.assertIsInstance(track["explicit"], bool)
        self.assertIn("isrc", track["external_ids"])
        # Passed through for availability_reason recording (spotifyClient.py).
        self.assertEqual(track["playability"], {"playable": True, "reason": "PLAYABLE"})

    @patch("spotapi.Public")
    def test_album_shape(self, mock_public):
        album = self._fetch(mock_public)["album"]
        self.assertEqual(album["id"], ALBUM_ID)
        self.assertEqual(album["name"], "Fixture Album")
        self.assertEqual(album["external_urls"]["spotify"],
                         f"https://open.spotify.com/album/{ALBUM_ID}")
        self.assertEqual(album["total_tracks"], 12)
        # convertToDatetime must be able to parse this (spotifyClient.py
        # _formatAlbum calls .timestamp() on the result).
        from Database.utils import convertToDatetime
        self.assertEqual(convertToDatetime(album["release_date"]).year, 2021)

    @patch("spotapi.Public")
    def test_album_images_largest_first(self, mock_public):
        """Client.formatTrack takes images[0].url as THE artwork; spotipy's
        convention is widest-first. The fixture is wire-ordered smallest-first
        on purpose - trusting wire order would ship 64px artwork."""
        images = self._fetch(mock_public)["album"]["images"]
        self.assertEqual(images[0]["url"], "https://i.scdn.co/image/large")
        self.assertEqual([i["width"] for i in images], [640, 300, 64])

    @patch("spotapi.Public")
    def test_artists_merged_in_order_with_ids(self, mock_public):
        """firstArtist then otherArtists, ids recovered from the artist uri -
        _formatArtists derives catalog rows and Spotify links from artist.id."""
        artists = self._fetch(mock_public)["artists"]
        self.assertEqual([a["name"] for a in artists], ["First Artist", "Second Artist"])
        self.assertEqual(artists[0]["id"], ARTIST_ID)
        self.assertEqual(artists[0]["external_urls"]["spotify"],
                         f"https://open.spotify.com/artist/{ARTIST_ID}")

    @patch("spotapi.Public")
    def test_explicit_mapping(self, mock_public):
        self.assertFalse(self._fetch(mock_public)["explicit"])
        explicit = self._fetch(mock_public, union=fakeTrackUnion(explicitLabel="EXPLICIT"))
        self.assertTrue(explicit["explicit"])

    @patch("spotapi.Public")
    def test_accepts_spotify_uri(self, mock_public):
        """The importer passes spotify:track: URIs straight from export files
        (StreamingHistoryImporter._fetchTrackMeta). The wrapper it replaces
        mangled these - isUrl() matched but urlToId() split on "/" and left the
        URI intact, so pathfinder got spotify:track:spotify:track:<id> and every
        URI lookup silently fell back to a name search. The owned client must
        normalize to the bare id."""
        mock_public.song_info.return_value = {"data": {"trackUnion": fakeTrackUnion()}}
        buildClient().track(f"spotify:track:{TRACK_ID}")
        mock_public.song_info.assert_called_once_with(TRACK_ID)

    @patch("spotapi.Public")
    def test_accepts_open_spotify_url(self, mock_public):
        mock_public.song_info.return_value = {"data": {"trackUnion": fakeTrackUnion()}}
        buildClient().track(f"https://open.spotify.com/track/{TRACK_ID}?si=abc123def")
        mock_public.song_info.assert_called_once_with(TRACK_ID)

    @patch("spotapi.Public")
    def test_accepts_bare_id(self, mock_public):
        mock_public.song_info.return_value = {"data": {"trackUnion": fakeTrackUnion()}}
        buildClient().track(TRACK_ID)
        mock_public.song_info.assert_called_once_with(TRACK_ID)


class TestTrackFallbackContract(unittest.TestCase):
    """A track Spotify wouldn't describe must yield a marked fallback record,
    not an exception - raising propagates into the poll loop's catch-all and
    loses the play (11 occurrences in 11 days of app.log; see patches.py's
    _fallbackTrackRecord, which moves here with its shape unchanged)."""

    @patch("Database.Spotify.client.time")  #< the retry ladder sleeps; tests must not
    @patch("spotapi.Public")
    def test_degraded_payload_yields_fallback_record(self, mock_public, _mock_time):
        mock_public.song_info.return_value = {"data": None}
        track = buildClient().track(TRACK_ID)

        # Facts are not invented: no duration, no artists, placeholder names.
        self.assertEqual(track["name"], UNKNOWN_TRACK_NAME)
        self.assertEqual(track["duration_ms"], 0)
        self.assertEqual(track["artists"], [])
        self.assertEqual(track["album"]["name"], UNKNOWN_ALBUM_NAME)
        # The id IS real, so the link is real - only fabricated ids carry "".
        self.assertEqual(track["id"], TRACK_ID)
        self.assertEqual(track["external_urls"]["spotify"],
                         f"https://open.spotify.com/track/{TRACK_ID}")
        # Per-track album id: one shared fabricated id would collect every
        # undescribable track into a single album page.
        self.assertEqual(track["album"]["id"], f"album_{TRACK_ID}")
        # The marker upsertTrack keys its don't-clobber-real-metadata guard off.
        self.assertEqual(track["created_reason"], RESTRICTED_FALLBACK_REASON)
        self.assertFalse(track["playability"]["playable"])


class TestSearchContract(unittest.TestCase):
    """search() feeds StreamingHistoryImporter._searchForSong, which reads
    result["tracks"]["items"] and treats an empty list as "no results"."""

    @patch("spotapi.Public")
    def test_search_shape_and_track_filtering(self, mock_public):
        mock_public.return_value.song_search.return_value = iter([fakeSearchPage()])
        result = buildClient().search("track:Fixture Song artist:First Artist")

        items = result["tracks"]["items"]
        self.assertEqual(len(items), 1)  #< the Album item must be filtered out
        self.assertEqual(items[0]["id"], TRACK_ID)
        self.assertEqual(items[0]["external_urls"]["spotify"],
                         f"https://open.spotify.com/track/{TRACK_ID}")
        self.assertEqual(items[0]["duration_ms"], 200040)

    @patch("spotapi.Public")
    def test_no_results_is_empty_list_not_error(self, mock_public):
        mock_public.return_value.song_search.return_value = iter([[]])
        self.assertEqual(buildClient().search("track:zzz artist:zzz")["tracks"]["items"], [])


def fakeAlbumUnion(albumId=ALBUM_ID):
    """A getAlbum albumUnion. Track entries sit under tracksV2 wrapped in
    {"track": ...} - the formatter must unwrap them."""
    return {
        "__typename": "Album",
        "uri": f"spotify:album:{albumId}",
        "name": "Fixture Album",
        "date": {"isoString": "2021-05-07T00:00:00Z", "precision": "DAY"},
        "coverArt": {"sources": [
            {"url": "https://i.scdn.co/image/albsmall", "width": 64, "height": 64},
            {"url": "https://i.scdn.co/image/alblarge", "width": 640, "height": 640},
        ]},
        "tracksV2": {
            "totalCount": 2,
            "items": [
                {"track": {"uri": f"spotify:track:{TRACK_ID}", "name": "Fixture Song",
                           "duration": {"totalMilliseconds": 200040}}},
                {"track": {"uri": "spotify:track:secondTrackId000000000x", "name": "Second Song",
                           "duration": {"totalMilliseconds": 180000}}},
            ],
        },
    }


class TestAlbumPlaylistContract(unittest.TestCase):
    """The listener reads .get("name"); the metadata backfiller reads much
    more from albums: release_date, total_tracks, and tracks.items[] with
    {id, name, duration_ms} per track (the album response is the only
    duration source for tracks whose own lookup came back blanked)."""

    @patch("spotapi.PublicAlbum")
    def _fetchAlbum(self, mock_album_cls):
        mock_album_cls.return_value.get_album_info.return_value = {
            "data": {"albumUnion": fakeAlbumUnion()}}
        return buildClient().album(ALBUM_ID)

    def test_album_identity(self):
        album = self._fetchAlbum()
        self.assertEqual(album["name"], "Fixture Album")
        self.assertEqual(album["id"], ALBUM_ID)

    def test_album_backfiller_fields(self):
        album = self._fetchAlbum()
        self.assertEqual(album["total_tracks"], 2)
        from Database.utils import convertToDatetime
        self.assertEqual(convertToDatetime(album["release_date"]).year, 2021)

        tracks = album["tracks"]["items"]
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0]["id"], TRACK_ID)
        self.assertEqual(tracks[0]["name"], "Fixture Song")
        self.assertEqual(tracks[0]["duration_ms"], 200040)

    @patch("spotapi.PublicAlbum")
    def test_album_without_release_date_omits_key(self, mock_album_cls):
        """The backfiller maps a missing/empty release_date to 0.0 itself;
        inventing a date here would claim a fact Spotify didn't state."""
        union = fakeAlbumUnion()
        del union["date"]
        mock_album_cls.return_value.get_album_info.return_value = {
            "data": {"albumUnion": union}}
        album = buildClient().album(ALBUM_ID)
        self.assertFalse(album.get("release_date"))

    @patch("spotapi.PublicPlaylist")
    def test_playlist_has_name_and_id(self, mock_playlist_cls):
        mock_playlist_cls.return_value.get_playlist_info.return_value = {
            "data": {"playlistV2": {"uri": f"spotify:playlist:{PLAYLIST_ID}", "name": "Fixture Playlist"}}}
        playlist = buildClient().playlist(PLAYLIST_ID)
        self.assertEqual(playlist["name"], "Fixture Playlist")
        self.assertEqual(playlist["id"], PLAYLIST_ID)


class TestPublicLookupClientPooling(unittest.TestCase):
    """album() and artist() must BORROW their TLSClient from spotapi's locked
    pool, never mint one per call and never fall back to the shared default.

    Two failure modes, and every test here pins one of them:

    - Sharing: spotapi.PublicAlbum/Artist default their `client` argument to
      ONE import-time TLSClient, and BaseClient.__init__ re-points that shared
      client's `authenticate`/`on_auth_failure` callbacks at itself
      (spotapi/client.py:94-95). These callers really do run concurrently -
      the metadata backfiller loops per user on its own thread while
      media_fetch resolves artist images inside a thread pool - so the shared
      default races.
    - Minting: a FRESH TLSClient per call is pinned forever, because both
      TLSClient.__init__ and BaseClient.__init__ atexit.register a close that
      is never unregistered (measured: 30 lookups left 30 live curl sessions
      after gc). The pool bounds live clients at peak concurrency instead.

    spotapi.Public's own song path already works this way (client_pool.get/
    put); track() rides it via Public.song_info."""

    def setUp(self):
        from spotapi.public import client_pool
        self.pool = client_pool
        self.pool.clear()   #< start empty; tests seed exactly what they assert on
        self.addCleanup(self.pool.clear)

    @patch("spotapi.PublicAlbum")
    def test_album_returns_its_borrowed_client_to_the_pool(self, mock_album_cls):
        mock_album_cls.return_value.get_album_info.return_value = {
            "data": {"albumUnion": fakeAlbumUnion()}}

        buildClient().album(ALBUM_ID)

        used = mock_album_cls.call_args.kwargs["client"]
        self.assertIn(used, self.pool.queue,
                      "the borrowed client must go back to the pool, not leak")

    @patch("spotapi.Artist")
    def test_artist_returns_its_borrowed_client_to_the_pool(self, mock_artist_cls):
        mock_artist_cls.return_value.get_artist.return_value = {"data": {"artistUnion": {
            "uri": f"spotify:artist:{ARTIST_ID}", "profile": {"name": "First Artist"}}}}

        buildClient().artist(ARTIST_ID)

        used = mock_artist_cls.call_args.kwargs["client"]
        self.assertIn(used, self.pool.queue)

    @patch("spotapi.PublicAlbum")
    def test_sequential_lookups_reuse_the_pooled_client(self, mock_album_cls):
        """The anti-leak half: a second lookup must pick the returned client
        back up, not construct a new TLSClient (each construction is pinned
        for the life of the process by its atexit registration)."""
        mock_album_cls.return_value.get_album_info.return_value = {
            "data": {"albumUnion": fakeAlbumUnion()}}
        seeded = object()   #< anything pool-shaped; PublicAlbum is mocked
        self.pool.put(seeded)
        sp = buildClient()

        sp.album(ALBUM_ID)
        sp.album(ALBUM_ID)

        first = mock_album_cls.call_args_list[0].kwargs["client"]
        second = mock_album_cls.call_args_list[1].kwargs["client"]
        self.assertIs(first, seeded)
        self.assertIs(second, seeded)
        self.assertEqual(len(self.pool.queue), 1,
                         "reuse must not grow the pool either")

    @patch("spotapi.PublicAlbum")
    def test_the_client_is_returned_even_when_the_lookup_raises(self, mock_album_cls):
        """The backfiller catches per-album exceptions and moves on - if the
        raising lookup kept its client checked out, every failure would leak
        one and a flaky stretch would drain the pool into minting."""
        mock_album_cls.return_value.get_album_info.side_effect = RuntimeError("boom")
        seeded = object()
        self.pool.put(seeded)

        with self.assertRaises(RuntimeError):
            buildClient().album(ALBUM_ID)

        self.assertIn(seeded, self.pool.queue)

    def test_concurrent_borrows_get_distinct_clients(self):
        """The isolation half (what commit 11af1fc fixed, kept): while one
        lookup holds a client, an overlapping one must get a DIFFERENT one -
        BaseClient re-points the borrowed client's auth callbacks at itself,
        so two concurrent holders of one client authenticate through
        whichever BaseClient was built last."""
        import spotapi
        from Database.Spotify.client import _pooledPublicClient

        with _pooledPublicClient() as outer, _pooledPublicClient() as inner:
            self.assertIsNot(outer, inner)
            self.assertIsInstance(outer, spotapi.TLSClient)
            self.assertIsInstance(inner, spotapi.TLSClient)

    @patch("spotapi.PublicAlbum")
    def test_the_client_is_not_spotapi_s_shared_default(self, mock_album_cls):
        """Sanity check on the dependency: PublicAlbum's default really is a
        single shared instance, so passing one explicitly is what isolates the
        call. If a future spotapi switches to a default_factory this fails,
        flagging that the workaround is no longer needed."""
        import inspect
        import spotapi.album   #< the submodule: only the spotapi.PublicAlbum re-export is mocked here

        mock_album_cls.return_value.get_album_info.return_value = {
            "data": {"albumUnion": fakeAlbumUnion()}}
        realDefault = inspect.signature(
            spotapi.album.PublicAlbum.__init__).parameters["client"].default

        buildClient().album(ALBUM_ID)

        passed = mock_album_cls.call_args.kwargs["client"]
        #< a shared instance, not a factory - which is exactly why we pass our own
        self.assertIsInstance(realDefault, spotapi.TLSClient)
        self.assertIsNot(passed, realDefault)


class TestArtistContract(unittest.TestCase):
    """media_fetch's lazy artist-image fallback reads
    artist.get("images")[0]["url"] - largest first, same as albums."""

    @patch("spotapi.Artist")
    def test_artist_images_largest_first(self, mock_artist_cls):
        mock_artist_cls.return_value.get_artist.return_value = {"data": {"artistUnion": {
            "uri": f"spotify:artist:{ARTIST_ID}",
            "profile": {"name": "First Artist"},
            "visuals": {"avatarImage": {"sources": [
                {"url": "https://i.scdn.co/image/artsmall", "width": 160, "height": 160},
                {"url": "https://i.scdn.co/image/artlarge", "width": 640, "height": 640},
            ]}},
        }}}
        artist = buildClient().artist(ARTIST_ID)
        self.assertEqual(artist["name"], "First Artist")
        self.assertEqual(artist["id"], ARTIST_ID)
        self.assertEqual(artist["images"][0]["url"], "https://i.scdn.co/image/artlarge")

    @patch("spotapi.Artist")
    def test_artist_without_visuals_has_empty_images(self, mock_artist_cls):
        """media_fetch treats an empty images list as "no artwork available";
        a KeyError here would instead mark the fetch failed forever."""
        mock_artist_cls.return_value.get_artist.return_value = {"data": {"artistUnion": {
            "uri": f"spotify:artist:{ARTIST_ID}", "profile": {"name": "First Artist"}}}}
        self.assertEqual(buildClient().artist(ARTIST_ID)["images"], [])


class TestCookieUtilitiesContract(unittest.TestCase):
    """parseCookieString feeds the login form's pasted cookies (routes/auth),
    saveSession writes the sessions file the login flow verifies against
    (app._verifyCookiesMatchEmail). Formats supported are what users actually
    paste: a devtools "k=v; k2=v2" line, a Netscape tab export (with its
    header row and # comments), or bare k=v lines."""

    def test_semicolon_line(self):
        from Database.Spotify.cookies import parseCookieString
        cookies = parseCookieString("sp_dc=abc; sp_key=def; sp_t=ghi")
        self.assertEqual(cookies, {"sp_dc": "abc", "sp_key": "def", "sp_t": "ghi"})

    def test_tab_separated_export_skips_header_and_comments(self):
        from Database.Spotify.cookies import parseCookieString
        raw = ("# Netscape HTTP Cookie File\n"
               "NAME\tVALUE\tDOMAIN\n"
               "sp_dc\tabc\t.spotify.com\n"
               "sp_key\tdef\t.spotify.com\n")
        cookies = parseCookieString(raw)
        self.assertEqual(cookies, {"sp_dc": "abc", "sp_key": "def"})

    def test_bare_pairs_one_per_line(self):
        from Database.Spotify.cookies import parseCookieString
        self.assertEqual(parseCookieString("sp_dc=abc\nsp_key=def\n\n"),
                         {"sp_dc": "abc", "sp_key": "def"})

    def test_save_session_replaces_same_identifier(self):
        from Database.Spotify.cookies import saveSession
        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "sessions.json")
            saveSession({"sp_dc": "old"}, "a@example.com", path)
            saveSession({"sp_dc": "keep"}, "b@example.com", path)
            saveSession({"sp_dc": "new"}, "a@example.com", path)

            sessions = json.loads(Path(path).read_text(encoding="utf-8"))
            byId = {s["identifier"]: s["cookies"] for s in sessions}
            self.assertEqual(len(sessions), 2)  #< replaced, not appended
            self.assertEqual(byId["a@example.com"], {"sp_dc": "new"})
            self.assertEqual(byId["b@example.com"], {"sp_dc": "keep"})

    def test_save_session_tolerates_corrupt_file(self):
        from Database.Spotify.cookies import saveSession
        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "sessions.json")
            Path(path).write_text("{not json", encoding="utf-8")
            self.assertTrue(saveSession({"sp_dc": "x"}, "a@example.com", path))
            sessions = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(sessions[0]["identifier"], "a@example.com")


class TestCurrentUserContract(unittest.TestCase):
    """The listener reads current_user().get("id") and .get("email") to detect
    cookie contamination. For some account types Spotify's profile carries no
    username, and the listener's own comments record that the email then serves
    AS the id - so id falls back to email rather than to None."""

    def _client(self):
        sp = buildClient()
        sp.user_auth = MagicMock()  #< logged-in sentinel; profile call is mocked anyway
        return sp

    @patch("spotapi.user.User")
    def test_id_and_email_from_profile(self, mock_user_cls):
        mock_user_cls.return_value.get_user_info.return_value = {
            "profile": {"username": "spotify_user_1", "email": "person@example.com"}}
        user = self._client().current_user()
        self.assertEqual(user["id"], "spotify_user_1")
        self.assertEqual(user["email"], "person@example.com")

    @patch("spotapi.user.User")
    def test_id_falls_back_to_email_when_no_username(self, mock_user_cls):
        mock_user_cls.return_value.get_user_info.return_value = {
            "profile": {"email": "person@example.com"}}
        user = self._client().current_user()
        self.assertEqual(user["id"], "person@example.com")
        self.assertEqual(user["email"], "person@example.com")


class TestRecentlyPlayedBufferContract(unittest.TestCase):
    """current_user_recently_played() is a LOCAL buffer, not a Spotify call -
    the single most important fact in this file. The listener's Z1 dedup
    (getNewItems) and the worker's item processing depend on:
      - a list (not None) even before anything played,
      - items shaped {"track", "played_at", "ms_played", "context"},
      - oldest-first order, capped at 50,
      - zero network on read."""

    def test_empty_before_anything_played(self):
        self.assertEqual(buildClient().current_user_recently_played(), [])

    @patch("spotapi.Public")
    def test_item_shape_after_a_play(self, mock_public):
        mock_public.song_info.return_value = {"data": {"trackUnion": fakeTrackUnion()}}
        sp = buildClient()
        sp._addToRecentlyPlayed(f"spotify:track:{TRACK_ID}", "2026-07-30T12:00:00Z",
                                f"spotify:playlist:{PLAYLIST_ID}", 200040)

        items = sp.current_user_recently_played()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["track"]["id"], TRACK_ID)
        self.assertEqual(item["played_at"], "2026-07-30T12:00:00Z")
        self.assertEqual(item["ms_played"], 200040)
        self.assertEqual(item["context"]["uri"], f"spotify:playlist:{PLAYLIST_ID}")

    @patch("spotapi.Public")
    def test_no_context_is_tolerated(self, mock_public):
        """A play with no known source still counts (spotifyClient.formatTrack
        treats a missing/None context as "source unknown", never an error)."""
        mock_public.song_info.return_value = {"data": {"trackUnion": fakeTrackUnion()}}
        sp = buildClient()
        sp._addToRecentlyPlayed(f"spotify:track:{TRACK_ID}", "2026-07-30T12:00:00Z", None, 1000)
        item = sp.current_user_recently_played()[0]
        self.assertTrue(item["context"] is None or item["context"].get("uri") in (None, ""))

    @patch("spotapi.Public")
    def test_oldest_first_capped_at_50(self, mock_public):
        mock_public.song_info.return_value = {"data": {"trackUnion": fakeTrackUnion()}}
        sp = buildClient()
        for i in range(55):
            sp._addToRecentlyPlayed(f"spotify:track:{TRACK_ID}", f"2026-07-30T12:{i:02d}:00Z", None, 1000)

        items = sp.current_user_recently_played()
        self.assertEqual(len(items), 50)
        self.assertEqual(items[0]["played_at"], "2026-07-30T12:05:00Z")   #< oldest surviving
        self.assertEqual(items[-1]["played_at"], "2026-07-30T12:54:00Z")  #< newest

    def test_read_makes_no_network_call(self):
        with patch("spotapi.Public") as mock_public:
            buildClient().current_user_recently_played()
            mock_public.song_info.assert_not_called()
            mock_public.return_value.song_search.assert_not_called()


class TestLoginContract(unittest.TestCase):
    """Login rules the listener's failure handling is built on:
      - a failed login RETURNS False and leaves user_auth a bool - it must not
        raise, and must not leave a half-built Login (spotifyListener's
        loginFailed branch reads exactly this sentinel),
      - the session matching the constructor's email is chosen from a
        multi-session cookies file (the cross-user contamination fix),
      - every login gets a FRESH TLSClient, never a shared default (ditto)."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cookiesFile = str(Path(self._tmp.name) / "cookies.json")

    def _writeSessions(self, identifiers):
        with open(self.cookiesFile, "w") as f:
            json.dump([{"identifier": ident, "cookies": {}} for ident in identifiers], f)

    def test_missing_cookies_file_fails_closed(self):
        from Database.Spotify import Spotify
        sp = Spotify(cookiesFile=str(Path(self._tmp.name) / "nope.json"), email="a@b.c")
        self.assertIsInstance(sp.user_auth, bool)
        self.assertFalse(sp.isLoggedIn())

    def test_corrupt_cookies_file_fails_closed(self):
        Path(self.cookiesFile).write_text("{not json", encoding="utf-8")
        from Database.Spotify import Spotify
        sp = Spotify(cookiesFile=self.cookiesFile, email="a@b.c")
        self.assertFalse(sp.isLoggedIn())

    @patch("spotapi.Login")
    def test_email_selects_matching_session(self, mock_login):
        self._writeSessions(["other@example.com", "target@example.com"])
        from Database.Spotify import Spotify
        Spotify(cookiesFile=self.cookiesFile, email="target@example.com")
        identifier = mock_login.from_saver.call_args.args[2]
        self.assertEqual(identifier, "target@example.com")

    @patch("spotapi.Login")
    def test_no_match_falls_back_to_first_session(self, mock_login):
        self._writeSessions(["first@example.com", "second@example.com"])
        from Database.Spotify import Spotify
        Spotify(cookiesFile=self.cookiesFile, email="absent@example.com")
        self.assertEqual(mock_login.from_saver.call_args.args[2], "first@example.com")

    @patch("spotapi.Login")
    def test_each_login_gets_a_fresh_tls_client(self, mock_login):
        """Two logins must not share a client: spotapi's Config dataclass
        defaults `client` to ONE import-time TLSClient, so every user's cookies
        landed in one process-wide jar - the cross-user contamination bug."""
        self._writeSessions(["a@example.com"])
        from Database.Spotify import Spotify
        Spotify(cookiesFile=self.cookiesFile, email="a@example.com")
        Spotify(cookiesFile=self.cookiesFile, email="a@example.com")

        cfg1 = mock_login.from_saver.call_args_list[0].args[1]
        cfg2 = mock_login.from_saver.call_args_list[1].args[1]
        self.assertIsNot(cfg1.client, cfg2.client)

    @patch("spotapi.Login")
    def test_successful_login_reports_logged_in(self, mock_login):
        self._writeSessions(["a@example.com"])
        from Database.Spotify import Spotify
        sp = Spotify(cookiesFile=self.cookiesFile, email="a@example.com")
        self.assertTrue(sp.isLoggedIn())
        self.assertEqual(sp.email, "a@example.com")


class TestListenerInternalsContract(unittest.TestCase):
    """spotifyListener reaches into the client via getattr chains during
    connect-state reads and shutdown. The attribute NAMES are the contract:
      sp.lastPlayedManager            None until the listener starts
      lastPlayedManager.manager       the PlayerStatus (has ._state, .ws)
      lastPlayedManager.run           writable stop flag
      lastPlayedManager.thread        joinable Thread"""

    def test_fresh_client_has_no_last_played_manager(self):
        sp = buildClient()
        self.assertIsNone(getattr(sp, "lastPlayedManager", "MISSING"),
                          "lastPlayedManager must exist and be None before start")

    def test_start_wires_manager_run_and_thread(self):
        sp = buildClient()
        sp.user_auth = MagicMock()  #< logged-in sentinel

        fakeManagerCls = MagicMock()
        with patch("Database.Spotify.client.RecentlyPlayedManager", fakeManagerCls):
            sp.startRecentlyPlayedListener(refreshInterval=6)

        fakeManagerCls.assert_called_once_with(sp.user_auth)
        manager = fakeManagerCls.return_value
        self.assertIs(sp.lastPlayedManager, manager)
        # started with the client's buffer-append callback and the interval
        callbackArg, intervalArg = manager.start.call_args.args
        self.assertEqual(callbackArg, sp._addToRecentlyPlayed)
        self.assertEqual(intervalArg, 6)


class TestSmokeTestStaysOutOfCI(unittest.TestCase):
    """Database/Spotify/smoketest.py talks to the REAL Spotify. It must never
    be collected by the suite: CI has no valid cookies, and a green build would
    then depend on Spotify's availability rather than on this code.

    Four things keep it out today - the CI command (`pytest tests`), testpaths
    in pyproject.toml, its filename, and the absence of test-shaped names
    inside it. The first two are configuration someone could widen; these
    assertions cover the two that live in the file itself, so a rename to
    test_smoke.py fails here with a reason instead of turning CI flaky."""

    #< pytest's defaults: python_files = test_*.py *_test.py
    def test_its_filename_does_not_match_pytest_discovery(self):
        from pathlib import Path

        name = Path("Database/Spotify/smoketest.py").name

        self.assertFalse(name.startswith("test_"), f"{name} would be auto-collected")
        self.assertFalse(name.endswith("_test.py"), f"{name} would be auto-collected")

    def test_it_defines_nothing_pytest_would_collect(self):
        """Belt and braces: even if a path config someday pulls the file in,
        pytest still finds nothing to run inside it."""
        import ast
        from pathlib import Path

        source = (REPO_ROOT / "Database" / "Spotify" / "smoketest.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        collectable = [
            node.name for node in ast.walk(tree)
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"))
            or (isinstance(node, ast.ClassDef) and node.name.startswith("Test"))
        ]

        self.assertEqual(collectable, [], "these would run against real Spotify under pytest")


if __name__ == "__main__":
    unittest.main()
