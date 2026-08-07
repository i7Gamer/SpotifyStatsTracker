"""The "Full plays only" filter on /history (routes/charts.py's historyPage,
templates/_full_plays_toggle.html).

Same control, same ?fullOnly=1|0 param and same completion-percent meaning as
the Top Songs/Artists/Albums pages (tests/test_top_lists_htmx.py's
TestFullPlaysOnlyToggle covers the markup contract there, and this file's
sibling in tests/test_history_htmx.py covers it here). What is asserted below
is the SQL end to end, against a real seeded repository rather than a MagicMock
db, because the filter has to reach four different queries - count and list,
each in a search and a non-search flavour - and the interesting failure is one
of them missing it.

Two things make that failure invisible to a narrower test:

- /history already excluded skips unconditionally before this filter existed
  (Repository.getPlaysCount and friends default includeSkips=False, and
  searchPlays hardcoded `AND p.is_skip=0`), so the checkbox's DEFAULT state is
  indistinguishable from the old behaviour. Everything new is in the OFF state.
- the count and the list are separate statements. A filter applied to the list
  but not the count leaves the rows right and the pager over-reporting, which
  no assertion about visible track names would catch - hence
  test_the_count_agrees_with_the_list_in_both_states.

The four seeded plays are one per case the predicate distinguishes, on four
DIFFERENT tracks so a rendered row names which one survived:

    Complete Song  200000ms of 200000ms  is_skip=0  - a finished play
    Partial Song    10000ms of 200000ms  is_skip=0  - listened, then moved on
    Skipped Song     1000ms of 200000ms  is_skip=1  - classified a skip
    Unknown Length   5000ms of      0ms  is_skip=0  - duration never arrived
"""
import unittest
from unittest.mock import patch

from tests._app_factory import AppTestCase

#< what htmx puts on every request it makes; asking for the list, not the shell
HX_HEADERS = {"HX-Request": "true"}

#< comfortably either side of any completion-complete percent the admin can set
#  (the default is 80), so these tests do not encode the current default
FULL_MS = 200000
PARTIAL_MS = 10000
SKIP_MS = 1000
UNKNOWN_DURATION_PLAY_MS = 5000
#< a track whose metadata never arrived: duration_ms <= 0 is the "cannot judge
#  this one" arm of FULL_PLAY_PREDICATE
UNKNOWN_DURATION = 0


def makeTrack(trackId, name, durationMs=FULL_MS):
    return {
        "id": trackId,
        "name": name,
        "url": f"http://example.com/track/{trackId}",
        "artists": [{"id": "art1", "name": "An Artist", "url": "http://example.com/artist/art1",
                     "imageUrl": "", "imageId": "art1"}],
        "album": {
            "id": "alb1", "name": "An Album", "url": "http://example.com/album/alb1",
            "imageId": "alb1", "imageUrl": "http://img.example.com/a.jpg",
            "totalTracks": 10, "releaseDate": 12345.0,
        },
        "imageUrl": "http://img.example.com/a.jpg",
        "imageId": "alb1",
        "duration": durationMs,
        "explicit": False,
        "isrc": "US1234567890",
        "discNumber": 1,
        "trackNumber": 3,
        "releaseDate": 12345.0,
    }


class HistoryFullPlaysTestCase(AppTestCase):
    def setUp(self):
        self.dash = self._makeApp()   #< registers shutdown(): get_user_db below starts real threads
        self.client = self.dash.app.test_client()
        self.username = "testuser"
        self.email = "testuser@example.com"

        self.listener_patcher = patch("Database.database.Database.startListener")
        self.listener_patcher.start()
        self.addCleanup(self.listener_patcher.stop)

        self.dash.repo.upsertUser(self.username, self.email)
        self.dash.repo.setUserCookies(self.username, {"sp_dc": "fake_cookie"})
        self.user_db = self.dash.get_user_db(self.username, self.email)

        self.dash.repo.upsertTrack(makeTrack("t1", "Complete Song"))
        self.dash.repo.upsertTrack(makeTrack("t2", "Partial Song"))
        self.dash.repo.upsertTrack(makeTrack("t3", "Skipped Song"))
        self.dash.repo.upsertTrack(makeTrack("t4", "Unknown Length Song", durationMs=UNKNOWN_DURATION))
        #< ascending played_at, so ?sort=oldest reverses a known order
        self.dash.repo.insertPlay(self.username, "t1", 1000.0, FULL_MS)
        self.dash.repo.insertPlay(self.username, "t2", 2000.0, PARTIAL_MS)
        self.dash.repo.insertPlay(self.username, "t3", 3000.0, SKIP_MS, is_skip=1)
        self.dash.repo.insertPlay(self.username, "t4", 4000.0, UNKNOWN_DURATION_PLAY_MS)
        self.dash.repo.commit()

        self.logged_in_patcher = patch.object(self.dash, "is_user_logged_in", return_value=True)
        self.logged_in_patcher.start()
        self.addCleanup(self.logged_in_patcher.stop)

        with self.client.session_transaction() as sess:
            sess["email"] = self.email
            sess["username"] = self.username

    def _list(self, query=""):
        """The fragment - the list itself, not the shell around it."""
        return self.client.get(f"/history{query}", headers=HX_HEADERS).get_data(as_text=True)

    def assertShows(self, body, *names):
        """Exactly `names` are listed, and nothing else seeded is."""
        everySong = {"Complete Song", "Partial Song", "Skipped Song", "Unknown Length Song"}
        for name in names:
            self.assertIn(name, body)
        for name in everySong - set(names):
            self.assertNotIn(name, body)


class TestTheFilterItself(HistoryFullPlaysTestCase):
    def test_the_default_lists_only_plays_that_finished(self):
        """No ?fullOnly at all: an absent param means the default, which is ON."""
        self.assertShows(self._list(), "Complete Song", "Unknown Length Song")

    def test_unchecking_lists_every_play_including_skips(self):
        """The whole point of the OFF state - and the only genuinely new
        behaviour here, since the ON state matches what /history always did."""
        self.assertShows(self._list("?fullOnly=0"),
                         "Complete Song", "Partial Song", "Skipped Song", "Unknown Length Song")

    def test_a_track_whose_duration_never_arrived_survives_the_filter(self):
        """FULL_PLAY_PREDICATE keeps duration_ms <= 0 rather than dropping it:
        the filter cannot judge that play, and dropping it would silently hide
        every play of a track whose metadata never came back from Spotify."""
        self.assertIn("Unknown Length Song", self._list())

    def test_an_explicit_one_is_the_same_as_no_param(self):
        self.assertShows(self._list("?fullOnly=1"), "Complete Song", "Unknown Length Song")

    def test_only_an_explicit_zero_opts_out(self):
        """Junk reads as the default rather than as the opt-out - the same
        tri-state the Top pages use, where absence already means ON."""
        self.assertShows(self._list("?fullOnly=bogus"), "Complete Song", "Unknown Length Song")


class TestEveryQueryPathAgrees(HistoryFullPlaysTestCase):
    """Four queries carry this filter - count and list, each in a search and a
    non-search flavour. A miss in any one of them is a bug the others hide."""

    def test_the_search_branch_filters_the_same_way(self):
        """searchPlays/searchPlaysCount are the path that had no includeSkips
        parameter at all before this change - they hardcoded the skip filter -
        so they are the likeliest to be left behind."""
        self.assertShows(self._list("?q=Song"), "Complete Song", "Unknown Length Song")
        self.assertShows(self._list("?q=Song&fullOnly=0"),
                         "Complete Song", "Partial Song", "Skipped Song", "Unknown Length Song")

    def test_oldest_first_filters_the_same_way(self):
        """A different query function (getPlaysOldestFirst), which also has to
        build its clauses in the right order - it has an extra afterTs bind the
        newest-first twin does not."""
        self.assertShows(self._list("?sort=oldest"), "Complete Song", "Unknown Length Song")
        self.assertShows(self._list("?sort=oldest&fullOnly=0"),
                         "Complete Song", "Partial Song", "Skipped Song", "Unknown Length Song")

    def test_the_filter_survives_a_date_range(self):
        """The range clause binds parameters too, so this is the case that
        catches a _fullPlaysClause built in the wrong position: with no range
        set, a scrambled bind order compares numbers to the wrong things and
        can still return the right rows."""
        wholeSeededSpan = "?interval=custom&startDate=1970-01-01&endDate=2100-01-01"

        self.assertShows(self._list(wholeSeededSpan), "Complete Song", "Unknown Length Song")
        self.assertShows(self._list(wholeSeededSpan + "&fullOnly=0"),
                         "Complete Song", "Partial Song", "Skipped Song", "Unknown Length Song")

    def test_the_count_agrees_with_the_list_in_both_states(self):
        """The count and the list are separate statements. A filter applied to
        one and not the other leaves the rows looking right while the pager
        reports a total nothing can page to."""
        self.assertIn("Showing 1-2 of 2", self._list())
        self.assertIn("Showing 1-4 of 4", self._list("?fullOnly=0"))

    def test_the_count_agrees_with_the_list_when_searching(self):
        self.assertIn("Showing 1-2 of 2", self._list("?q=Song"))
        self.assertIn("Showing 1-4 of 4", self._list("?q=Song&fullOnly=0"))


class TestTheFilterRidesAlong(HistoryFullPlaysTestCase):
    #< that it reaches the PAGINATION links needs more than one page of results,
    #  so it is pinned in tests/test_dashboard_pagination.py's
    #  TestHistoryFullPlaysWiring, where the stubbed db already reports 120

    def test_the_off_state_survives_into_the_first_load_url(self):
        """The shell's placeholder fetches listUrl. Leaving fullOnly out of it
        would make the first load disagree with the checkbox rendered above it."""
        shell = self.client.get("/history?fullOnly=0").get_data(as_text=True)

        self.assertIn("fullOnly=0", shell)

    def test_junk_never_reaches_the_first_load_url(self):
        """This page's URLs are built from validated values, not echoed back -
        the same rule test_the_first_load_url_holds_no_unvalidated_input pins
        for ?interval=."""
        shell = self.client.get("/history?fullOnly=bogus").get_data(as_text=True)

        self.assertNotIn("bogus", shell)

    def test_it_combines_with_the_tag_filter(self):
        """Both narrow the same queries, and both bind parameters."""
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t1")
        self.dash.repo.addTag(self.username, "roadtrip", "track", "t2")
        self.dash.repo.commit()

        self.assertShows(self._list("?tag=roadtrip"), "Complete Song")
        self.assertShows(self._list("?tag=roadtrip&fullOnly=0"), "Complete Song", "Partial Song")


class TestThePlayTypeBadges(HistoryFullPlaysTestCase):
    """Unticking the filter turns /history into the raw log, and a raw log is
    only useful if a 1-second skip is distinguishable from a real listen - the
    rows are otherwise identical, since the card shows the track, not the play.

    Same vocabulary as the song detail timeline (playType/playTypeLabel, see
    _enrichSongTimelineEntries), because they classify the same thing against
    the same admin-tunable boundary."""

    #< PARTIAL_MS of FULL_MS is 5%; the label rounds it. The separator is a
    #  literal U+2022, not &bull; - Jinja escapes only &<>"', so it reaches the
    #  page as itself
    PARTIAL_BADGE = "Partial • 5%"
    SKIP_BADGE = "Skipped"

    def test_a_partial_listen_says_how_much_of_it_was_played(self):
        body = self._list("?fullOnly=0")

        self.assertIn(self.PARTIAL_BADGE, body)
        self.assertIn('class="track-label play-type-partial"', body)

    def test_a_skip_is_labelled_as_one(self):
        body = self._list("?fullOnly=0")

        self.assertIn(self.SKIP_BADGE, body)
        self.assertIn('class="track-label play-type-skip"', body)

    def test_a_play_that_finished_carries_no_badge(self):
        """Only the exceptions are worth a chip. Badging every row would put a
        label on all of them in the DEFAULT view, where by definition they are
        all full plays - noise that says nothing."""
        body = self._list("?fullOnly=0")

        self.assertNotIn("play-type-full", body)
        self.assertNotIn("Full Play", body)

    def test_the_default_view_carries_no_badges_at_all(self):
        """With the filter on, every row IS a full play."""
        body = self._list()

        self.assertNotIn("play-type-", body)
        self.assertNotIn("Partial", body)
        self.assertNotIn(self.SKIP_BADGE, body)

    def test_a_track_whose_duration_is_unknown_is_not_called_partial(self):
        """The percentage would be meaningless and the row is NOT a skip, so it
        reads as a normal play - matching the filter, which keeps it for exactly
        the same reason (it cannot judge what it cannot measure)."""
        body = self._list("?fullOnly=0")
        card = body[body.index("Unknown Length Song"):]
        card = card[:card.find("</article>")]

        self.assertNotIn("play-type-", card)

    def test_the_badge_survives_a_search(self):
        """The search branch hydrates its rows through the same path; it used
        not to select is_skip at all, which would have made every skip in a
        search result render as a partial listen."""
        body = self._list("?q=Skipped&fullOnly=0")

        self.assertIn("Skipped Song", body)
        self.assertIn('class="track-label play-type-skip"', body)


if __name__ == "__main__":
    unittest.main()
