"""The friends half of /api/now-playing.

This is a disclosure surface: it reports, live, what other people are doing.
The tests that matter most are the negative ones - who must NOT appear - so
they come first. The two toggles guarding it are covered in
test_friends_now_playing_settings.py.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from config import FRIENDS_NOW_PLAYING_LIMIT
from _app_factory import AppTestCase


def _nowPlaying(name="Nightcall", trackId="t1", isPaused=False, artists=None):
    """A getNowPlaying() return value, in the shape listener.py produces.

    The `played`/`trackPlayed` flags are deliberately True here: they are the
    FRIEND's own history, and no chip may be built from them (see
    TestTheChipLinksToWhatItNames)."""
    return {
        "trackId": trackId, "name": name, "artistsText": "Kavinsky",
        "artists": [{"id": "art1", "name": "Kavinsky", "played": True}]
                   if artists is None else artists,
        "trackPlayed": True, "imageId": "img1", "isPaused": isPaused,
        "positionMs": 42000, "durationMs": 250000,
    }


def _catalogTrack(trackId, artistIds):
    """The upsertTrack payload for a track the VIEWER can then have plays of -
    enough of one for getPlayedTrackIds/getPlayedArtistIds to see it."""
    return {
        "id": trackId, "name": f"Track {trackId}", "url": "",
        "artists": [{"id": artistId, "name": f"Artist {artistId}", "url": "",
                     "imageUrl": "", "imageId": artistId} for artistId in artistIds],
        "album": {"id": f"al-{trackId}", "name": "Album", "url": "", "imageId": "",
                  "imageUrl": "", "totalTracks": 1, "releaseDate": 0.0},
        "imageUrl": "", "imageId": "", "duration": 200000, "explicit": False,
        "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0.0,
    }


class FriendsNowPlayingTestCase(AppTestCase):
    VIEWER = "alice"
    VIEWER_EMAIL = "alice@example.com"

    def setUp(self):
        self.dash = self._makeApp()
        self.dash.repo.upsertUser(self.VIEWER, self.VIEWER_EMAIL)

    def _addUser(self, username, playing=None, hideNowPlaying=False, live=True):
        """A second account, optionally sharing with the viewer and optionally
        with a live Database in this process."""
        self.dash.repo.upsertUser(username, f"{username}@example.com")
        self.dash.repo.setUserCookies(username, {"sp_dc": "cookie"})
        if hideNowPlaying:
            self.dash.repo.updateUserSettings(username, "day", None, hide_now_playing=True)
        if live:
            db = MagicMock()
            db.getNowPlaying.return_value = playing
            self.dash.user_databases[username] = db
            return db
        return None

    def _share(self, username):
        """An accepted (mutual) share between the viewer and `username`: the
        counterpart requests, the viewer accepts."""
        self.dash.repo.createShareRequest(username, self.VIEWER)
        pending = self.dash.repo.getPendingIncomingShares(self.VIEWER)
        shareId = next(row["id"] for row in pending if row["requester_username"] == username)
        self.dash.repo.respondToShareRequest(shareId, self.VIEWER, accept=True)

    def _viewerPlayed(self, trackId="t1", artistIds=("art1",)):
        """Give the VIEWER their own play of this track. That - not the
        friend's history - is what decides whether a chip links to our detail
        pages or out to Spotify."""
        self.dash.repo.upsertTrack(_catalogTrack(trackId, artistIds))
        self.dash.repo.insertPlay(self.VIEWER, trackId, 100, 60000)
        self.dash.repo.commit()

    def _payload(self):
        return self.dash.getFriendsNowPlaying(self.VIEWER)

    def _friendsFromTheEndpoint(self):
        """The chips as the browser gets them - the only place the URLs exist,
        since they are built with url_for in the route."""
        client = self.dash.app.test_client()
        own = MagicMock()
        own.getNowPlaying.return_value = None   #< the viewer's own half; a Mock is not serializable
        with patch.object(self.dash, "is_user_logged_in", return_value=True), \
             patch.object(self.dash, "get_username_for_email", return_value=self.VIEWER), \
             patch.object(self.dash, "get_user_db", return_value=own):
            with client.session_transaction() as sess:
                sess["email"] = self.VIEWER_EMAIL
            return client.get("/api/now-playing").get_json()["friends"]


class TestWhoIsExcluded(FriendsNowPlayingTestCase):
    def test_a_user_with_no_share_never_appears(self):
        """The whole authorization boundary in one test: bob is actively
        playing and has a live session, but has never shared with alice."""
        self._addUser("bob", playing=_nowPlaying())

        self.assertEqual(self._payload()["friends"], [])

    def test_a_pending_share_is_not_enough(self):
        self._addUser("bob", playing=_nowPlaying())
        self.dash.repo.createShareRequest(self.VIEWER, "bob")   #< never accepted

        self.assertEqual(self._payload()["friends"], [])

    def test_a_friend_who_opted_out_is_excluded(self):
        self._addUser("bob", playing=_nowPlaying(), hideNowPlaying=True)
        self._share("bob")

        self.assertEqual(self._payload()["friends"], [])

    def test_a_paused_friend_is_excluded(self):
        self._addUser("bob", playing=_nowPlaying(isPaused=True))
        self._share("bob")

        self.assertEqual(self._payload()["friends"], [])

    def test_a_friend_playing_nothing_is_excluded(self):
        self._addUser("bob", playing=None)
        self._share("bob")

        self.assertEqual(self._payload()["friends"], [])

    def test_a_friend_with_no_live_session_is_skipped_not_started(self):
        """Constructing a Database here would start another user's listener
        from this request thread."""
        self._addUser("bob", live=False)
        self._share("bob")

        with patch.object(self.dash, "get_user_db") as getUserDb:
            payload = self._payload()

        self.assertEqual(payload["friends"], [])
        getUserDb.assert_not_called()

    def test_a_failing_lookup_drops_only_that_friend(self):
        broken = self._addUser("bob", playing=None)
        broken.getNowPlaying.side_effect = RuntimeError("listener exploded")
        self._share("bob")
        self._addUser("carol", playing=_nowPlaying(name="Teardrop"))
        self._share("carol")

        friends = self._payload()["friends"]

        self.assertEqual([f["username"] for f in friends], ["carol"])


class TestWhatIsReported(FriendsNowPlayingTestCase):
    def test_a_sharing_friend_who_is_playing_appears(self):
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")

        friends = self._payload()["friends"]

        self.assertEqual(len(friends), 1)
        self.assertEqual(friends[0]["username"], "bob")
        self.assertEqual(friends[0]["name"], "Nightcall")
        self.assertEqual(friends[0]["artistsText"], "Kavinsky")
        self.assertEqual(friends[0]["imageId"], "img1")

    def test_a_chip_carries_the_display_name_alongside_the_username(self):
        """The chips are built client-side, so they can't go through the Jinja
        filter - the name people go by has to ride in the payload. `username`
        stays too: dashboard-page.js falls back to it."""
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")
        self.dash.repo.setDisplayName("bob", "Bob Builder")

        entry = self._payload()["friends"][0]

        self.assertEqual(entry["displayName"], "Bob Builder")
        self.assertEqual(entry["username"], "bob")

    def test_a_friend_without_one_reports_their_username_as_the_display_name(self):
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")

        self.assertEqual(self._payload()["friends"][0]["displayName"], "bob")

    def test_playback_position_is_not_disclosed(self):
        """Narrower than the viewer's own payload on purpose: a progress bar is
        noise at chip size, and pause state is already the exclusion rule."""
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")

        entry = self._payload()["friends"][0]

        for leaked in ("positionMs", "durationMs", "isPaused"):
            with self.subTest(field=leaked):
                self.assertNotIn(leaked, entry)

    def test_the_friend_s_own_listening_history_is_not_disclosed(self):
        """The chip carries artist ids and names - which artistsText already
        said out loud - but never the friend's `played` flags. What someone
        else has listened to BEFORE is theirs, and the chip has no use for it:
        its links are decided by the viewer's history (see
        TestTheChipLinksToWhatItNames)."""
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")

        entry = self._payload()["friends"][0]

        self.assertNotIn("trackPlayed", entry)
        for artist in entry["artists"]:
            self.assertNotIn("played", artist)

    def test_friends_are_ordered_stably(self):
        """Chips must not reshuffle between 15s polls."""
        for name in ("carol", "bob", "dave"):
            self._addUser(name, playing=_nowPlaying())
            self._share(name)

        self.assertEqual([f["username"] for f in self._payload()["friends"]],
                         ["bob", "carol", "dave"])

    def test_the_strip_is_capped_and_reports_the_overflow(self):
        for index in range(FRIENDS_NOW_PLAYING_LIMIT + 3):
            name = f"friend{index:02d}"
            self._addUser(name, playing=_nowPlaying())
            self._share(name)

        payload = self._payload()

        self.assertEqual(len(payload["friends"]), FRIENDS_NOW_PLAYING_LIMIT)
        self.assertEqual(payload["moreCount"], 3)

    def test_no_overflow_count_when_under_the_cap(self):
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")

        self.assertEqual(self._payload()["moreCount"], 0)


class TestTheChipLinksToTheFriend(FriendsNowPlayingTestCase):
    """A chip names someone you already share with, and /compare is the page
    that puts the two of you side by side - so the friend's NAME links to it.

    The URL is built in the route rather than spelled out in dashboard-page.js:
    the chips are rendered client-side, so a hand-built path there would be
    both unroutable by url_for and untestable from here."""

    def test_each_chip_carries_a_compare_link_for_that_friend(self):
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")

        self.assertEqual(self._friendsFromTheEndpoint()[0]["compareUrl"], "/compare?with=bob")

    def test_the_link_selects_by_username_even_behind_a_display_name(self):
        """?with= picks an account, and comparePage silently falls back to
        another share when it matches none - so a display name there would
        compare the viewer with the wrong person rather than fail."""
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")
        self.dash.repo.setDisplayName("bob", "Bob Builder")

        self.assertEqual(self._friendsFromTheEndpoint()[0]["compareUrl"], "/compare?with=bob")


class TestTheChipLinksToWhatItNames(FriendsNowPlayingTestCase):
    """A chip names three things and links each one where it belongs: the
    friend to /compare (above), the track to its own page, and every artist to
    theirs.

    Internal or out to Spotify is decided by the VIEWER's own play history, for
    the same reason the Compare page's counterpart lists are (see
    services/taste_match._markLinkExternally): /song/<id> and /artist/<id>
    render the VIEWER's data, so for something they have never played
    songDetailPage answers _missingEntityResponse and the link goes nowhere
    useful. The friend is playing it right now, so their own flags would be
    near-always true - and are none of the viewer's business besides."""

    def _bobPlaying(self, **kwargs):
        self._addUser("bob", playing=_nowPlaying(**kwargs))
        self._share("bob")
        return self._friendsFromTheEndpoint()[0]

    def test_a_track_the_viewer_has_played_links_to_our_own_song_page(self):
        self._viewerPlayed(trackId="t1")

        self.assertEqual(self._bobPlaying()["trackUrl"], "/song/t1")

    def test_a_track_the_viewer_has_never_played_links_out_to_spotify(self):
        """Nothing of the viewer's to show, so /song/<id> would bounce."""
        self.assertEqual(self._bobPlaying()["trackUrl"],
                         "https://open.spotify.com/track/t1")

    def test_an_artist_the_viewer_has_played_links_to_our_own_artist_page(self):
        self._viewerPlayed(trackId="t1", artistIds=("art1",))

        artists = self._bobPlaying()["artists"]

        self.assertEqual(artists, [{"name": "Kavinsky", "url": "/artist/art1"}])

    def test_an_artist_the_viewer_has_never_played_links_out_to_spotify(self):
        self.assertEqual(self._bobPlaying()["artists"],
                         [{"name": "Kavinsky", "url": "https://open.spotify.com/artist/art1"}])

    def test_the_viewer_s_history_decides_the_links_not_the_friend_s(self):
        """The whole point: the fixture says the FRIEND has played both the
        track and the artist. Alice has played neither, so both go to Spotify -
        an internal link here would be reading someone else's history."""
        entry = self._bobPlaying()

        self.assertTrue(entry["trackUrl"].startswith("https://open.spotify.com/"))
        self.assertTrue(entry["artists"][0]["url"].startswith("https://open.spotify.com/"))

    def test_one_played_artist_does_not_carry_an_unplayed_co_credit(self):
        """Each artist is judged on its own - a featured artist the viewer has
        never played still links out even beside one they have."""
        self._viewerPlayed(trackId="t1", artistIds=("art1",))
        entry = self._bobPlaying(artists=[{"id": "art1", "name": "Kavinsky"},
                                          {"id": "art2", "name": "Lovefoxxx"}])

        self.assertEqual([a["url"] for a in entry["artists"]],
                         ["/artist/art1", "https://open.spotify.com/artist/art2"])

    def test_a_first_listen_with_no_catalog_entry_carries_no_artist_links(self):
        """getNowPlaying's connect-state fallback has artist NAMES but no ids
        (see Database/workers/listener.py), so there is nothing to link to -
        the chip falls back to the plain artistsText it already carried."""
        entry = self._bobPlaying(artists=[])

        self.assertEqual(entry["artists"], [])
        self.assertEqual(entry["artistsText"], "Kavinsky")

    def test_a_nameless_track_id_still_yields_no_broken_link(self):
        """Local files and podcasts reach getNowPlaying with no track id at
        all; an empty href would be a link to the current page."""
        entry = self._bobPlaying(trackId=None)

        self.assertEqual(entry["trackUrl"], "")

    def test_the_lookups_are_batched_over_the_whole_strip(self):
        """One query for the strip's tracks and one for its artists, not two
        per chip - the poll runs every 15 seconds. Ids are deduped: friends
        listening to the same thing is exactly what a shared strip surfaces."""
        for index, name in enumerate(("bob", "carol", "dave")):
            self._addUser(name, playing=_nowPlaying(trackId="t1" if index else "t9"))
            self._share(name)

        with patch.object(self.dash.repo, "getPlayedTrackIds", return_value=set()) as tracks, \
             patch.object(self.dash.repo, "getPlayedArtistIds", return_value=set()) as artists:
            self._payload()

        tracks.assert_called_once_with(self.VIEWER, ["t9", "t1"])
        artists.assert_called_once_with(self.VIEWER, ["art1"])


class TestToggleGating(FriendsNowPlayingTestCase):
    def test_the_admin_switch_empties_the_strip(self):
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")
        self.dash.repo.setFriendsNowPlayingEnabled(False)

        self.assertEqual(self._payload(), {"friends": [], "moreCount": 0})

    def test_turning_off_data_sharing_also_empties_it(self):
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")
        self.dash.repo.setDataSharingEnabled(False)

        self.assertEqual(self._payload(), {"friends": [], "moreCount": 0})

    def test_a_disabled_switch_skips_the_lookups_entirely(self):
        db = self._addUser("bob", playing=_nowPlaying())
        self._share("bob")
        self.dash.repo.setFriendsNowPlayingEnabled(False)

        self._payload()

        db.getNowPlaying.assert_not_called()


class TestEndpoint(FriendsNowPlayingTestCase):
    def _client(self):
        viewerDb = MagicMock()
        viewerDb.getNowPlaying.return_value = _nowPlaying(name="My Song", trackId="mine")
        for patcher in (
            patch.object(self.dash, "is_user_logged_in", return_value=True),
            patch.object(self.dash, "get_username_for_email", return_value=self.VIEWER),
            patch.object(self.dash, "get_user_db", return_value=viewerDb),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        client = self.dash.app.test_client()
        with client.session_transaction() as sess:
            sess["email"] = self.VIEWER_EMAIL
            sess["username"] = self.VIEWER
        return client

    def test_the_poll_returns_both_halves_in_one_request(self):
        self._addUser("bob", playing=_nowPlaying())
        self._share("bob")

        payload = self._client().get("/api/now-playing").get_json()

        self.assertEqual(payload["nowPlaying"]["name"], "My Song")
        self.assertEqual([f["username"] for f in payload["friends"]], ["bob"])
        self.assertEqual(payload["friendsMoreCount"], 0)

    def test_the_friends_key_is_always_present(self):
        """The client renders off this key every poll; an absent key would
        make it fall back to whatever was on screen."""
        payload = self._client().get("/api/now-playing").get_json()

        self.assertEqual(payload["friends"], [])

    def test_an_anonymous_request_is_rejected(self):
        with patch.object(self.dash, "is_user_logged_in", return_value=False):
            resp = self.dash.app.test_client().get("/api/now-playing")

        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
