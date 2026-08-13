"""The manual merge review queue: the title-similarity tier that ASKS.

The ISRC matcher merges on fact - one ISRC, one recording - and was
deliberately told not to touch remasters or mono mixes, each a new master with
its own ISRC. This tier surfaces what that leaves behind: same title (after
version-marker normalization), same primary artist, duration agreeing within a
measured tolerance. It proposes; a person decides. Both verdicts write the
same track_merge_decisions rows the automatic tier already honours, so a
human's yes survives every later matcher pass and a human's no is never
re-proposed - the exact semantics migrate1_48_0's docstring promised for them.

Measured basis (2026-08-07, live copy): exact name+artist finds 427 duplicate
groups; normalizing the trailing version markers finds 648; of the
name-matched groups agreeing on duration, most agree within 1s and the ones
differing by >10s are genuinely different cuts - so the 3s gate keeps
different recordings out while letting scan-time remasters through.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
from Database.queries.tracks import isPlainTitle, normalizeTrackTitle

ISRC_A = "USRC12345678"
ISRC_B = "GBAYE0000001"
DURATION_MS = 200000


class MergeReviewTestCase(DatabaseTestCase):
    def _db(self):
        return self._makeDb({}, [])

    def _track(self, db, trackId, name, artist="Talking Heads", duration=DURATION_MS,
               isrc=None, plays=0, createdAt=1000.0, album="Album"):
        conn = db.repo._conn()
        artistId = "art_" + artist.replace(" ", "_").lower()
        albumId = "alb_" + album.replace(" ", "_").lower()
        with conn:
            conn.execute("INSERT OR IGNORE INTO users (username, created_at) VALUES ('alice', 0)")
            conn.execute("INSERT OR IGNORE INTO albums (id, name, url) VALUES (?, ?, '')",
                         (albumId, album))
            conn.execute("INSERT OR IGNORE INTO artists (id, name, url) VALUES (?, ?, '')",
                         (artistId, artist))
            conn.execute(
                "INSERT INTO tracks (id, name, url, album_id, duration_ms, isrc, created_at) "
                "VALUES (?, ?, '', ?, ?, ?, ?)",
                (trackId, name, albumId, duration, isrc, createdAt))
            conn.execute(
                "INSERT INTO track_artists (track_id, artist_id, position) VALUES (?, ?, 0)",
                (trackId, artistId))
            for i in range(plays):
                conn.execute("INSERT INTO plays (username, track_id, played_at, time_played) "
                             "VALUES ('alice', ?, ?, 200000)", (trackId, 1e9 + i))
        return trackId

    def _canonical(self, db, trackId):
        return db.repo._conn().execute(
            "SELECT canonical_id FROM tracks WHERE id=?", (trackId,)).fetchone()[0]

    def _decision(self, db, trackId):
        row = db.repo._conn().execute(
            "SELECT canonical_id, reason, decided_by FROM track_merge_decisions "
            "WHERE track_id=?", (trackId,)).fetchone()
        return dict(row) if row else None

    def _againstId(self, db, trackId):
        row = db.repo._conn().execute(
            "SELECT against_id FROM track_merge_decisions WHERE track_id=?",
            (trackId,)).fetchone()
        return row["against_id"] if row else None


class TestTitleNormalization(unittest.TestCase):
    """The recall half of the queue: a remaster differs by NAME, so exact
    matching finds 427 groups where normalized matching finds 648."""

    def test_version_marker_suffixes_are_stripped(self):
        for raw in ("Psycho Killer - 2005 Remaster",
                    "Psycho Killer (Remastered 2019)",
                    "Psycho Killer - Mono",
                    "Psycho Killer (Stereo Mix)",
                    "PSYCHO KILLER"):
            self.assertEqual(normalizeTrackTitle(raw), "psycho killer", raw)

    def test_stacked_suffixes_all_strip(self):
        self.assertEqual(normalizeTrackTitle("Song - Mono - 2009 Remaster"), "song")

    def test_a_live_version_is_not_the_same_recording(self):
        """Deliberately NOT a marker: a live cut is a different performance,
        which is the line this whole feature must never blur."""
        self.assertEqual(normalizeTrackTitle("Song (Live)"), "song (live)")

    def test_a_remix_is_not_the_same_recording_either(self):
        """Same rule as live: a remix or club edit is a different recording,
        so bare "mix" and "edit" are not markers - only the explicit
        same-recording forms strip ("radio edit", "stereo mix" via "stereo",
        and so on). "Remix" contains "mix", so a bare "mix" marker was
        quietly proposing every remix against its original."""
        for raw in ("Song (Club Mix)", "Song - Remix", "Song (VIP Edit)"):
            self.assertEqual(normalizeTrackTitle(raw), raw.lower(), raw)
        #< the explicit forms still strip
        self.assertEqual(normalizeTrackTitle("Song - Radio Edit"), "song")
        self.assertEqual(normalizeTrackTitle("Song (Stereo Mix)"), "song")

    def test_a_plain_title_survives_unchanged(self):
        self.assertEqual(normalizeTrackTitle("Don't Stop Believin'"), "don't stop believin'")

    def test_a_title_that_is_only_a_marker_keeps_its_name(self):
        """"Remastered" by some artist must not normalize to the empty string
        and collide with every other empty key."""
        self.assertEqual(normalizeTrackTitle("Remastered"), "remastered")


class TestPlainTitle(unittest.TestCase):
    """Which release READS as the song itself: the one whose title is nothing
    but the song's name. It is the same marker list normalizeTrackTitle groups
    by, asked the other way round, so "plainest" and "same song" can never
    drift apart."""

    def test_a_bare_title_is_plain(self):
        self.assertTrue(isPlainTitle("Psycho Killer"))

    def test_a_version_marker_makes_it_packaging(self):
        for raw in ("Psycho Killer - 2005 Remaster",
                    "Psycho Killer (Remastered 2019)",
                    "Psycho Killer - Mono",
                    "Psycho Killer (Deluxe Edition)",
                    "Psycho Killer (feat. Someone)"):
            self.assertFalse(isPlainTitle(raw), raw)

    def test_it_reads_the_same_markers_as_the_grouping(self):
        """A live cut or a remix is NOT a version marker (a different
        performance is a different recording), so those titles are plain -
        which is right: they are the song's name as that release ships it."""
        for raw in ("Song (Live)", "Song - Remix"):
            self.assertTrue(isPlainTitle(raw), raw)

    def test_a_title_that_is_only_a_marker_is_plain(self):
        """Nothing was stripped, because stripping it would leave nothing -
        so "Remastered" is that song's own name."""
        self.assertTrue(isPlainTitle("Remastered"))

    def test_case_and_padding_are_not_packaging(self):
        self.assertTrue(isPlainTitle("  PSYCHO KILLER  "))


class TestReviewCandidates(MergeReviewTestCase):
    def test_a_remaster_pair_is_proposed_with_the_most_played_as_canonical(self):
        db = self._db()
        self._track(db, "A" * 22, "Psycho Killer", isrc=ISRC_A, plays=70)
        self._track(db, "B" * 22, "Psycho Killer - 2005 Remaster", isrc=ISRC_B, plays=13)

        review = db.repo.getMergeReviewCandidates()

        self.assertEqual(review["totalGroups"], 1)
        self.assertEqual(review["totalMembers"], 1)
        group = review["groups"][0]
        self.assertEqual(group["canonical"]["trackId"], "A" * 22)
        self.assertEqual(group["members"][0]["trackId"], "B" * 22)

    def test_the_plain_release_is_suggested_even_when_it_is_played_less(self):
        """The point of the suggestion: whichever release you keep becomes the
        song's page, and a page titled "- 2005 Remaster" reads as a different
        song from the one you played. Plays used to decide alone, so a
        well-seeded remaster took the page from the original."""
        db = self._db()
        self._track(db, "A" * 22, "Psycho Killer - 2005 Remaster", plays=70)
        self._track(db, "B" * 22, "Psycho Killer", plays=13)

        group = db.repo.getMergeReviewCandidates()["groups"][0]

        self.assertEqual(group["canonical"]["trackId"], "B" * 22)
        self.assertEqual([m["trackId"] for m in group["members"]], ["A" * 22])

    def test_the_least_packaged_title_wins_when_no_release_is_plain(self):
        """Nothing in the group is the bare song, so "normal" falls back to
        what the user described it as: the shortest of the names."""
        db = self._db()
        self._track(db, "A" * 22, "Song - Mono - 2009 Remaster", plays=70)
        self._track(db, "B" * 22, "Song - 2009 Remaster", plays=3)

        self.assertEqual(
            db.repo.getMergeReviewCandidates()["groups"][0]["canonical"]["trackId"],
            "B" * 22)

    def test_plays_still_decide_between_two_equally_plain_releases(self):
        """The old rule is not gone, it moved down: title plainness only
        separates a release from its own packaging. Two copies of the bare
        title are the case it says nothing about."""
        db = self._db()
        self._track(db, "A" * 22, "Song", album="Single", plays=3)
        self._track(db, "B" * 22, "Song", album="The Best Of", plays=70)

        self.assertEqual(
            db.repo.getMergeReviewCandidates()["groups"][0]["canonical"]["trackId"],
            "B" * 22)

    def test_a_release_nobody_has_played_does_not_take_the_songs_page(self):
        """The one case the plain-title rule answers badly: a release NOBODY
        here has ever played. On the live queue (2026-08-12) three of the
        fifty visible rows proposed handing the song's page to one - "We Are
        the Warriors" on an untouched compilation, against the feat. release
        carrying all 22 plays - so the page, its cover and its link would all
        have become the pressing nobody listened to.

        "Played less" is the question the title rule is for. "No history here
        at all" is a different one, and it is answered above it."""
        db = self._db()
        self._track(db, "A" * 22, "We Are the Warriors",
                    album="Last of the Warriors, Vol. 1", plays=0)
        self._track(db, "B" * 22, "We Are the Warriors (feat. Johnny Santoro)", plays=22)

        group = db.repo.getMergeReviewCandidates()["groups"][0]

        self.assertEqual(group["canonical"]["trackId"], "B" * 22)
        self.assertEqual([m["trackId"] for m in group["members"]], ["A" * 22])

    def test_the_title_rule_still_decides_between_two_unplayed_releases(self):
        """The new tier separates "has a history here" from "has none", so a
        group where nothing has been played ties on it - and the title rule
        underneath decides exactly as it did before."""
        db = self._db()
        self._track(db, "A" * 22, "Song - 2005 Remaster", plays=0)
        self._track(db, "B" * 22, "Song", plays=0)

        self.assertEqual(
            db.repo.getMergeReviewCandidates()["groups"][0]["canonical"]["trackId"],
            "B" * 22)

    def test_matching_is_case_insensitive(self):
        db = self._db()
        self._track(db, "A" * 22, "YOU SO DONE", plays=80)
        self._track(db, "B" * 22, "You So Done", plays=61)

        self.assertEqual(db.repo.getMergeReviewCandidates()["totalGroups"], 1)

    def test_durations_beyond_the_tolerance_do_not_pair(self):
        """The >10s disagreements in the measured data were genuinely
        different cuts - the gate is what keeps them out."""
        db = self._db()
        self._track(db, "A" * 22, "Song", duration=DURATION_MS, plays=5)
        self._track(db, "B" * 22, "Song", duration=DURATION_MS + 4000, plays=2)

        self.assertEqual(db.repo.getMergeReviewCandidates()["totalGroups"], 0)

    def test_an_unknown_duration_cannot_disagree(self):
        """A fabricated import row carries duration 0 - "unknown", not "zero
        seconds long". Those are exactly the ghosts worth surfacing."""
        db = self._db()
        self._track(db, "A" * 22, "Song", duration=DURATION_MS, plays=5)
        self._track(db, "B" * 22, "Song", duration=0, plays=2)

        self.assertEqual(db.repo.getMergeReviewCandidates()["totalGroups"], 1)

    def test_the_same_title_by_another_artist_is_another_song(self):
        db = self._db()
        self._track(db, "A" * 22, "Intro", artist="Artist One", plays=5)
        self._track(db, "B" * 22, "Intro", artist="Artist Two", plays=3)

        self.assertEqual(db.repo.getMergeReviewCandidates()["totalGroups"], 0)

    def test_both_tiers_elect_the_same_release_to_survive(self):
        """The two tiers meet whenever a group's ISRCs arrive after a person
        has already been asked about it, and a person who answered the queue
        must not have placed the song somewhere the matcher would then move
        it. They share _electCanonical rather than a description of it; this
        is the assertion that would fail if one of them grew its own rule.

        The tie-breakers are what a restatement gets wrong, so the group is
        built to need all three: equal plays, then equal created_at."""
        db = self._db()
        self._track(db, "B" * 22, "Song", plays=4, createdAt=500.0)
        self._track(db, "A" * 22, "Song", plays=4, createdAt=500.0)
        self._track(db, "C" * 22, "Song", plays=1, createdAt=100.0)

        anchor = db.repo.getMergeReviewCandidates()["groups"][0]["canonical"]["trackId"]
        #< the same three tracks, now carrying the ISRC that hands them to the
        #  automatic tier instead
        db.repo.updateTrackIsrcs({t * 22: ISRC_A for t in "ABC"})
        canonical = db.repo.previewMergeTracksByIsrc()["groups"][0]["canonical"]["trackId"]

        self.assertEqual(anchor, canonical)
        #< the last tie-break is the id under max(), so the HIGHEST wins. Which
        #  direction is arbitrary - only that both tiers pick the same one
        #  matters - but it is asserted so a rewrite has to say it moved
        self.assertEqual(anchor, "B" * 22)

    def test_a_nameless_release_can_never_take_the_songs_page(self):
        """The trap in measuring "plainest" as "nothing was stripped": a
        blanked row normalizes to "" untouched, which reads as the plainest
        title there is AND the shortest. The review queue filters nameless
        rows out in SQL, but the ISRC tier does not - so this is asserted
        where the election is shared, through the tier that can reach it."""
        db = self._db()
        self._track(db, "A" * 22, "", isrc=ISRC_A, plays=70)
        self._track(db, "B" * 22, "Song", isrc=ISRC_A, plays=2)

        canonical = db.repo.previewMergeTracksByIsrc()["groups"][0]["canonical"]

        self.assertEqual(canonical["trackId"], "B" * 22)

    def test_both_tiers_agree_about_the_plain_title_too(self):
        """The title rule is the newest key in the shared election, so it is
        the one most likely to be added to only the tier that asked for it.
        Same-ISRC releases can differ by name (a single and a deluxe reissue
        of one master), so the ISRC tier has the same question to answer."""
        db = self._db()
        self._track(db, "A" * 22, "Song (Deluxe Edition)", plays=70)
        self._track(db, "B" * 22, "Song", plays=3)

        anchor = db.repo.getMergeReviewCandidates()["groups"][0]["canonical"]["trackId"]
        db.repo.updateTrackIsrcs({"A" * 22: ISRC_A, "B" * 22: ISRC_A})
        canonical = db.repo.previewMergeTracksByIsrc()["groups"][0]["canonical"]["trackId"]

        self.assertEqual(anchor, canonical)
        self.assertEqual(anchor, "B" * 22)

    def test_both_tiers_agree_that_an_unplayed_release_steps_aside(self):
        """Same reason as the title rule above: the newest key in the shared
        election is the one most likely to be added to only the tier that
        asked for it. The matcher reaches this state on its own - a pressing
        with no plays is exactly what a freshly backfilled ISRC pairs up."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=0)
        self._track(db, "B" * 22, "Song (Deluxe Edition)", plays=9)

        anchor = db.repo.getMergeReviewCandidates()["groups"][0]["canonical"]["trackId"]
        db.repo.updateTrackIsrcs({"A" * 22: ISRC_A, "B" * 22: ISRC_A})
        canonical = db.repo.previewMergeTracksByIsrc()["groups"][0]["canonical"]["trackId"]

        self.assertEqual(anchor, canonical)
        self.assertEqual(anchor, "B" * 22)

    def test_a_pair_sharing_an_isrc_belongs_to_the_automatic_tier(self):
        """Same ISRC = same master = the checkbox's job, and its preview
        already lists it. The queue exists for what ISRC can NOT decide."""
        db = self._db()
        self._track(db, "A" * 22, "Song", isrc=ISRC_A, plays=5)
        self._track(db, "B" * 22, "Song", isrc=ISRC_A, plays=2)

        self.assertEqual(db.repo.getMergeReviewCandidates()["totalGroups"], 0)

    def test_a_pinned_track_is_never_proposed(self):
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")

        self.assertEqual(db.repo.getMergeReviewCandidates()["totalGroups"], 0)

    def test_an_already_merged_pair_is_done_not_pending(self):
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="timorzipa")

        self.assertEqual(db.repo.getMergeReviewCandidates()["totalGroups"], 0)

    def test_a_member_of_some_other_merge_group_is_not_poached(self):
        """A track already merged elsewhere has been decided - by the matcher
        or a person - and the sticky rule says decisions don't drift."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=50)
        self._track(db, "B" * 22, "Song", plays=2)
        self._track(db, "C" * 22, "Song (Remastered)", plays=9)
        #< B already belongs to C's group
        db.repo.mergeTrackManually("B" * 22, "C" * 22, decidedBy="timorzipa")

        review = db.repo.getMergeReviewCandidates()

        #< C (with B inside) is still offered against A - as a member, since
        #  A out-plays it - but B itself is not listed separately
        memberIds = {m["trackId"] for g in review["groups"] for m in g["members"]}
        self.assertNotIn("B" * 22, memberIds)

    def test_new_arrivals_are_offered_into_an_existing_groups_canonical(self):
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=50)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="timorzipa")
        self._track(db, "C" * 22, "Song (2011 Remaster)", plays=9)

        review = db.repo.getMergeReviewCandidates()

        self.assertEqual(review["totalGroups"], 1)
        group = review["groups"][0]
        self.assertEqual(group["canonical"]["trackId"], "A" * 22)
        self.assertEqual([m["trackId"] for m in group["members"]], ["C" * 22])

    def test_groups_lead_with_the_most_plays_and_totals_count_everything(self):
        db = self._db()
        self._track(db, "A" * 22, "Small Song", plays=5)
        self._track(db, "B" * 22, "Small Song", plays=1)
        self._track(db, "C" * 22, "Big Song", plays=100)
        self._track(db, "D" * 22, "Big Song", plays=90)

        review = db.repo.getMergeReviewCandidates()

        self.assertEqual([g["canonical"]["trackId"] for g in review["groups"]],
                         ["C" * 22, "A" * 22])
        self.assertEqual(review["totalGroups"], 2)
        self.assertEqual(review["totalMembers"], 2)

    def test_the_page_cap_reports_what_it_cut(self):
        db = self._db()
        self._track(db, "A" * 22, "Song One", plays=5)
        self._track(db, "B" * 22, "Song One", plays=1)
        self._track(db, "C" * 22, "Song Two", plays=9)
        self._track(db, "D" * 22, "Song Two", plays=2)

        with patch("Database.queries.tracks.MERGE_REVIEW_PAGE_LIMIT", 1):
            review = db.repo.getMergeReviewCandidates()

        self.assertEqual(len(review["groups"]), 1)
        self.assertEqual(review["totalGroups"], 2)   #< the cap cuts display, not the count


class TestManualMerge(MergeReviewTestCase):
    def test_it_merges_and_records_who_decided(self):
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)

        merged = db.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="timorzipa")

        self.assertEqual(merged, 1)
        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)
        decision = self._decision(db, "B" * 22)
        self.assertEqual(decision["reason"], "manual-merge")
        self.assertEqual(decision["decided_by"], "timorzipa")

    def test_it_drops_the_cached_wrapped_years_the_pair_was_played_in(self):
        """Same shape as the automatic tier, scope included: the merge moves
        numbers frozen inside the cached years the resulting group was played
        in, and leaves the rest alone. _track times its plays in 2001; 2025 is
        a year neither track was ever played in, so it is not this merge's to
        drop. Full contract in test_wrapped_invalidation_scope."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        conn = db.repo._conn()
        with conn:
            for year in (2001, 2025):
                conn.execute(
                    "INSERT INTO user_wrapped (username, year, calculated_at, max_played_at,"
                    " total_plays, total_ms, longest_streak, unique_songs, unique_artists,"
                    " discovered_songs, discovered_artists, time_series_day, time_series_week,"
                    " time_series_month, top_songs, top_artists, top_albums,"
                    " discovered_songs_list, discovered_artists_list, discovered_albums_list)"
                    " VALUES ('alice', ?, 1, 1, 1, 1, 1, 1, 1, 1, 1, '[]', '[]', '[]', '[]',"
                    " '[]', '[]', '[]', '[]', '[]')", (year,))

        db.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="timorzipa")

        self.assertEqual(
            [row["year"] for row in
             conn.execute("SELECT year FROM user_wrapped ORDER BY year")], [2025])

    def test_merging_a_canonical_carries_its_members_along(self):
        """Never a chain: C already points at B, so B joining A must bring C
        to A too - or every reader walks a linked list of unknown depth."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=50)
        self._track(db, "B" * 22, "Song", plays=2)
        self._track(db, "C" * 22, "Song", plays=1)
        db.repo.mergeTrackManually("C" * 22, "B" * 22, decidedBy="timorzipa")

        merged = db.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="timorzipa")

        self.assertEqual(merged, 2)
        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)
        self.assertEqual(self._canonical(db, "C" * 22), "A" * 22)
        #< the audit row follows the move, so it still says where C points
        self.assertEqual(self._decision(db, "C" * 22)["canonical_id"], "A" * 22)

    def test_merging_into_a_member_lands_on_its_canonical(self):
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=50)
        self._track(db, "B" * 22, "Song", plays=2)
        self._track(db, "C" * 22, "Song", plays=1)
        db.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="timorzipa")

        db.repo.mergeTrackManually("C" * 22, "B" * 22, decidedBy="timorzipa")

        self.assertEqual(self._canonical(db, "C" * 22), "A" * 22)

    def test_merging_a_track_into_its_own_group_changes_nothing(self):
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="timorzipa")

        merged = db.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="timorzipa")

        self.assertEqual(merged, 0)
        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)

    def test_an_unknown_track_is_refused(self):
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)

        with self.assertRaises(ValueError):
            db.repo.mergeTrackManually("Z" * 22, "A" * 22, decidedBy="timorzipa")
        with self.assertRaises(ValueError):
            db.repo.mergeTrackManually("A" * 22, "Z" * 22, decidedBy="timorzipa")

    def test_the_matcher_never_overrules_a_manual_merge(self):
        """The human said same recording; a later ISRC pass discovering they
        carry different ISRCs must not undo that."""
        db = self._db()
        self._track(db, "A" * 22, "Song", isrc=ISRC_A, plays=5)
        self._track(db, "B" * 22, "Song", isrc=ISRC_B, plays=2)
        db.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="timorzipa")

        db.repo.mergeTracksByIsrc()
        db.repo.unmergeAllIsrcMerges()   #< the toggle's OFF edge

        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)


class TestDismissal(MergeReviewTestCase):
    def test_a_dismissal_is_a_recorded_not_the_same_verdict(self):
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)

        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")

        decision = self._decision(db, "B" * 22)
        self.assertIsNone(decision["canonical_id"])
        self.assertEqual(decision["reason"], "manual-reject")
        self.assertEqual(decision["decided_by"], "timorzipa")
        self.assertIsNone(self._canonical(db, "B" * 22))

    def test_a_dismissal_moves_no_numbers_so_it_drops_no_caches(self):
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        conn = db.repo._conn()
        with conn:
            conn.execute(
                "INSERT INTO user_wrapped (username, year, calculated_at, max_played_at,"
                " total_plays, total_ms, longest_streak, unique_songs, unique_artists,"
                " discovered_songs, discovered_artists, time_series_day, time_series_week,"
                " time_series_month, top_songs, top_artists, top_albums,"
                " discovered_songs_list, discovered_artists_list, discovered_albums_list)"
                " VALUES ('alice', 2025, 1, 1, 1, 1, 1, 1, 1, 1, 1, '[]', '[]', '[]', '[]',"
                " '[]', '[]', '[]', '[]', '[]')")

        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")

        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM user_wrapped").fetchone()[0], 1)

    def test_a_later_shared_isrc_outranks_the_dismissal(self):
        """The dismissal answered the QUEUE's question - same title, no shared
        ISRC, since the queue never proposes a pair that has one. When the
        backfill later delivers a shared ISRC, the fact that guess was
        standing in for has arrived, and fact outranks guess: the pair merges,
        and the row becomes an ordinary matcher decision (decided_by NULL) so
        the toggle's off edge undoes it like any other ISRC merge. A manual
        SPLIT is the opposite verdict - a person overruling the ISRC itself -
        and stays pinned (test_a_manual_decision_is_never_overwritten)."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")
        #< the fact arrives afterwards, via the backfill's own write path
        db.repo.updateTrackIsrcs({"A" * 22: ISRC_A, "B" * 22: ISRC_A})

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)
        decision = self._decision(db, "B" * 22)
        self.assertEqual(decision["reason"], "isrc")
        self.assertIsNone(decision["decided_by"])

    def test_the_overriding_merge_is_undone_by_the_toggle_like_any_other(self):
        """What decided_by NULL buys: the person's no was outranked, not
        turned into a merge nothing can take back - the toggle's off edge
        clears it along with every other matcher merge."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")
        db.repo.updateTrackIsrcs({"A" * 22: ISRC_A, "B" * 22: ISRC_A})
        db.repo.mergeTracksByIsrc()

        db.repo.unmergeAllIsrcMerges()

        self.assertIsNone(self._canonical(db, "B" * 22))
        self.assertIsNone(self._decision(db, "B" * 22))

    def test_an_unknown_track_is_refused(self):
        db = self._db()
        with self.assertRaises(ValueError):
            db.repo.dismissMergeCandidate("Z" * 22, decidedBy="timorzipa")

    def test_a_merged_track_cannot_be_dismissed(self):
        """The queue only proposes UNMERGED members, so a dismissal arriving
        for a merged track was aimed at a state that no longer exists - the
        queue open in two tabs, or the matcher landing between render and
        click. Recording it anyway would leave the pointer standing under a
        row claiming "not the same recording": an audit that lies, one the
        toggle's off edge can never clear (decided_by is set), and one the
        reject-yields rule could flip straight back regardless. The "no" for
        a merged track is the split (unmergeTrack), which pins."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="timorzipa")

        with self.assertRaises(ValueError):
            db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")

        #< nothing moved and the audit still tells the truth
        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)
        self.assertEqual(self._decision(db, "B" * 22)["reason"], "manual-merge")

    def test_a_person_can_change_their_mind_and_merge_after_all(self):
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")

        merged = db.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="timorzipa")

        self.assertEqual(merged, 1)
        self.assertEqual(self._decision(db, "B" * 22)["reason"], "manual-merge")


class TestSeeingAndUndoingDismissals(MergeReviewTestCase):
    """The other half of "both answers are remembered": a remembered answer
    you cannot find is one you cannot change your mind about.

    A dismissal is the only verdict with no surface anywhere - a merge shows on
    the song's page and can be split there, but "not the same" just removes the
    pair from the queue forever, and nothing lists what was removed. Undoing it
    deletes the row outright rather than writing a third verdict: the pair goes
    back to never-having-been-asked, which is exactly the state that lets the
    queue propose it again."""

    def _dismissedIds(self, db):
        return [entry["trackId"]
                for entry in db.repo.getDismissedMergeCandidates()["entries"]]

    def _onlyEntry(self, db):
        return db.repo.getDismissedMergeCandidates()["entries"][0]

    def test_a_dismissal_records_what_it_was_ruled_against(self):
        """The half the row was missing. "Not the same" is only re-checkable
        months later if it says not the same as WHAT - by then the pair that
        made it obvious is gone from the queue and out of memory."""
        db = self._db()
        self._track(db, "A" * 22, "Psycho Killer", album="Talking Heads: 77", plays=5)
        self._track(db, "B" * 22, "Psycho Killer - Live", album="The Name Of This Band", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa", againstId="A" * 22)

        against = self._onlyEntry(db)["against"]

        self.assertEqual(against["trackId"], "A" * 22)
        self.assertEqual(against["name"], "Psycho Killer")
        self.assertEqual(against["album"], "Talking Heads: 77")

    def test_a_dismissal_without_a_counterpart_still_records_the_verdict(self):
        """Every row written before the column existed, and any caller that
        does not know the pair. The log names what was ruled on and admits it
        does not know the rest, rather than the whole entry vanishing."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")

        entry = self._onlyEntry(db)

        self.assertEqual(entry["trackId"], "B" * 22)
        self.assertIsNone(entry["against"])

    def test_a_counterpart_that_names_no_track_is_refused(self):
        """Without the check the FOREIGN KEY raises instead, which the route
        cannot turn into a 400 - the same reason its siblings check first."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)

        with self.assertRaises(ValueError):
            db.repo.dismissMergeCandidate("A" * 22, decidedBy="timorzipa", againstId="Z" * 22)
        self.assertIsNone(self._decision(db, "A" * 22))

    def test_a_release_cannot_be_ruled_not_the_same_as_itself(self):
        """Nothing on the page can post it - the row keeping the song's page
        carries no verdict buttons - but a log entry reading "X, not the same
        as X" is a decision that cannot be re-checked because it means
        nothing."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)

        with self.assertRaises(ValueError):
            db.repo.dismissMergeCandidate("A" * 22, decidedBy="timorzipa", againstId="A" * 22)

    def test_changing_your_mind_and_merging_clears_the_counterpart(self):
        """The row stops being a rejection, so the release it was rejected
        against stops being true of it - left behind it would describe a
        verdict that no longer exists."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa", againstId="A" * 22)

        db.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="timorzipa")

        self.assertIsNone(self._againstId(db, "B" * 22))

    def test_a_shared_isrc_overruling_a_dismissal_clears_the_counterpart(self):
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa", againstId="A" * 22)
        db.repo.updateTrackIsrcs({"A" * 22: ISRC_A, "B" * 22: ISRC_A})

        db.repo.mergeTracksByIsrc()

        self.assertIsNone(self._againstId(db, "B" * 22))

    def test_a_counterpart_already_merged_into_the_track_is_refused(self):
        """The contradiction, refused on the way in rather than cleaned up
        after: a "no" against a release that is already in this track's group
        would be false the moment it was written. The queue cannot post it -
        it proposes only unmerged pairs - so this is the crafted-post 400."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="timorzipa")

        with self.assertRaises(ValueError):
            db.repo.dismissMergeCandidate("A" * 22, decidedBy="timorzipa",
                                          againstId="B" * 22)
        self.assertEqual(self._dismissedIds(db), [])

    def test_a_release_nobody_has_played_is_listed_at_zero(self):
        """The tally is a second query keyed by id, so a release missing from
        it must read as zero rather than dropping out of the log."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=0)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")

        self.assertEqual(self._onlyEntry(db)["plays"], 0)

    def test_merging_the_counterpart_INTO_the_rejected_release_clears_it_too(self):
        """The same contradiction from the other end. Only the release being
        MERGED has its row rewritten, so a rejection sitting on the release
        that keeps the song's page survived the pair being merged - and the
        log went on saying "not the same as X" about a group X is now in."""
        db = self._db()
        self._track(db, "P" * 22, "Song", plays=1)
        self._track(db, "R" * 22, "Song - 2005 Remaster", plays=50)
        db.repo.dismissMergeCandidate("P" * 22, decidedBy="timorzipa", againstId="R" * 22)

        db.repo.mergeTrackManually("R" * 22, "P" * 22, decidedBy="timorzipa")

        self.assertIsNone(self._decision(db, "P" * 22))
        self.assertEqual(self._dismissedIds(db), [])

    def test_a_shared_isrc_landing_on_the_rejected_release_clears_it_too(self):
        """The automatic tier reaches the same state, and more easily since
        the title rule elects exactly the plain release a person is likely to
        have ruled the remaster against."""
        db = self._db()
        self._track(db, "P" * 22, "Song", isrc=ISRC_A, plays=1)
        self._track(db, "R" * 22, "Song - 2005 Remaster", isrc=ISRC_A, plays=50)
        db.repo.dismissMergeCandidate("P" * 22, decidedBy="timorzipa", againstId="R" * 22)

        db.repo.mergeTracksByIsrc()

        self.assertEqual(self._canonical(db, "R" * 22), "P" * 22)   #< the title rule's head
        self.assertIsNone(self._decision(db, "P" * 22))
        self.assertEqual(self._dismissedIds(db), [])

    def test_a_rejection_about_some_other_release_is_left_standing(self):
        """Only the contradicted verdict goes. "B is not the same as C" says
        nothing about A, so A merging into B must not delete it - that would
        turn any unrelated merge into a silent erasure of someone's answer."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        self._track(db, "C" * 22, "Other", plays=1)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa", againstId="C" * 22)

        db.repo.mergeTrackManually("A" * 22, "B" * 22, decidedBy="timorzipa")

        self.assertEqual(self._decision(db, "B" * 22)["reason"], "manual-reject")
        self.assertEqual(self._againstId(db, "B" * 22), "C" * 22)

    def test_a_rejection_naming_nothing_is_left_standing(self):
        """Recorded before against_id existed: nothing knows what it was ruled
        against, so nothing can prove this merge contradicts it."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")

        db.repo.mergeTrackManually("A" * 22, "B" * 22, decidedBy="timorzipa")

        self.assertEqual(self._decision(db, "B" * 22)["reason"], "manual-reject")

    def test_the_counterpart_arriving_by_carry_along_clears_it_too(self):
        """The counterpart need not be the track named in the merge: C rides
        into B's group as A's dependent, and the pair is just as merged."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        self._track(db, "C" * 22, "Song", plays=1)
        db.repo.mergeTrackManually("C" * 22, "A" * 22, decidedBy="timorzipa")
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa", againstId="C" * 22)

        db.repo.mergeTrackManually("A" * 22, "B" * 22, decidedBy="timorzipa")

        self.assertEqual(self._canonical(db, "C" * 22), "B" * 22)
        self.assertIsNone(self._decision(db, "B" * 22))

    def test_splitting_a_track_back_out_clears_the_counterpart(self):
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa", againstId="A" * 22)
        db.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="timorzipa")

        db.repo.unmergeTrack("B" * 22, decidedBy="timorzipa")

        self.assertEqual(self._decision(db, "B" * 22)["reason"], "manual-split")
        self.assertIsNone(self._againstId(db, "B" * 22))

    def test_a_dismissal_is_listed_with_what_it_was_and_who_said_so(self):
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", album="The Best Of", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")

        listing = db.repo.getDismissedMergeCandidates()

        self.assertEqual(listing["total"], 1)
        entry = listing["entries"][0]
        self.assertEqual(entry["trackId"], "B" * 22)
        self.assertEqual(entry["name"], "Song")
        self.assertEqual(entry["album"], "The Best Of")
        self.assertEqual(entry["plays"], 2)
        self.assertEqual(entry["decidedBy"], "timorzipa")
        self.assertGreater(entry["decidedAt"], 0)

    def test_only_dismissals_are_listed_not_the_other_verdicts(self):
        """A merge and a split both have their own surface already - the song's
        own page - and neither is undone by putting a pair back in the queue."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        self._track(db, "C" * 22, "Song", plays=1)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")
        db.repo.mergeTrackManually("C" * 22, "A" * 22, decidedBy="timorzipa")
        db.repo.unmergeTrack("C" * 22, decidedBy="timorzipa")

        self.assertEqual(self._dismissedIds(db), ["B" * 22])

    def test_the_newest_decision_is_listed_first(self):
        db = self._db()
        self._track(db, "A" * 22, "Song One", plays=5)
        self._track(db, "B" * 22, "Song One", plays=2)
        self._track(db, "C" * 22, "Song Two", plays=5)
        self._track(db, "D" * 22, "Song Two", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")
        with patch("Database.queries.tracks.time.time", return_value=2e9):
            db.repo.dismissMergeCandidate("D" * 22, decidedBy="timorzipa")

        self.assertEqual(self._dismissedIds(db), ["D" * 22, "B" * 22])

    def test_the_page_cap_reports_what_it_cut(self):
        db = self._db()
        self._track(db, "A" * 22, "Song One", plays=5)
        self._track(db, "B" * 22, "Song One", plays=2)
        self._track(db, "C" * 22, "Song Two", plays=5)
        self._track(db, "D" * 22, "Song Two", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")
        db.repo.dismissMergeCandidate("D" * 22, decidedBy="timorzipa")

        with patch("Database.queries.tracks.MERGE_DISMISSED_PAGE_LIMIT", 1):
            listing = db.repo.getDismissedMergeCandidates()

        self.assertEqual(len(listing["entries"]), 1)
        self.assertEqual(listing["total"], 2)

    def test_undoing_a_dismissal_puts_the_pair_back_in_the_queue(self):
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")
        self.assertEqual(db.repo.getMergeReviewCandidates()["totalGroups"], 0)

        db.repo.undismissMergeCandidate("B" * 22)

        self.assertIsNone(self._decision(db, "B" * 22))
        self.assertEqual(db.repo.getMergeReviewCandidates()["totalGroups"], 1)
        self.assertEqual(db.repo.getDismissedMergeCandidates()["total"], 0)

    def test_one_admin_can_take_back_another_admins_no(self):
        """DELIBERATE, and pinned here because nothing else says so: the
        verdict is not owned by whoever reached it.

        A merge is instance-wide - it moves every account's numbers at once -
        so the log of what was kept apart is instance-wide too, and any admin
        can revisit any call. Scoping the undo to its author would leave a
        decision nobody remaining can undo the moment that admin is gone,
        which is the same "cannot change your mind" this whole log exists to
        fix. decided_by stays the record of WHO, on the row and on the page;
        it is not a permission."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")

        db.repo.undismissMergeCandidate("B" * 22)   #< no caller identity at all

        self.assertIsNone(self._decision(db, "B" * 22))

    def test_the_log_shows_every_admins_rows_and_names_who(self):
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        self._track(db, "C" * 22, "Other", plays=1)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")
        db.repo.dismissMergeCandidate("C" * 22, decidedBy="kevin")

        listing = db.repo.getDismissedMergeCandidates()

        self.assertEqual(listing["total"], 2)
        self.assertEqual({entry["decidedBy"] for entry in listing["entries"]},
                         {"timorzipa", "kevin"})

    def test_undoing_moves_no_numbers_so_it_drops_no_caches(self):
        """Same reasoning as the dismissal it undoes: no pointer moves, so
        nothing frozen in a cached year changed."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")
        conn = db.repo._conn()
        with conn:
            conn.execute(
                "INSERT INTO user_wrapped (username, year, calculated_at, max_played_at,"
                " total_plays, total_ms, longest_streak, unique_songs, unique_artists,"
                " discovered_songs, discovered_artists, time_series_day, time_series_week,"
                " time_series_month, top_songs, top_artists, top_albums,"
                " discovered_songs_list, discovered_artists_list, discovered_albums_list)"
                " VALUES ('alice', 2025, 1, 1, 1, 1, 1, 1, 1, 1, 1, '[]', '[]', '[]', '[]',"
                " '[]', '[]', '[]', '[]', '[]')")

        db.repo.undismissMergeCandidate("B" * 22)

        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM user_wrapped").fetchone()[0], 1)

    def test_a_placement_is_not_something_this_can_delete(self):
        """The row shapes differ by one column, so an unfiltered DELETE here
        would quietly unpin a merge or a split - the two verdicts that exist
        precisely to survive every automatic pass."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.mergeTrackManually("B" * 22, "A" * 22, decidedBy="timorzipa")

        with self.assertRaises(ValueError):
            db.repo.undismissMergeCandidate("B" * 22)

        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)
        self.assertEqual(self._decision(db, "B" * 22)["reason"], "manual-merge")

    def test_a_dismissal_a_shared_isrc_already_overruled_is_not_one_to_undo(self):
        """It stopped being the person's verdict when the fact arrived: the
        row is now an ordinary matcher merge, and the way back is the toggle's
        off edge or a split, both of which say so out loud."""
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)
        self._track(db, "B" * 22, "Song", plays=2)
        db.repo.dismissMergeCandidate("B" * 22, decidedBy="timorzipa")
        db.repo.updateTrackIsrcs({"A" * 22: ISRC_A, "B" * 22: ISRC_A})
        db.repo.mergeTracksByIsrc()

        with self.assertRaises(ValueError):
            db.repo.undismissMergeCandidate("B" * 22)

        self.assertEqual(self._canonical(db, "B" * 22), "A" * 22)

    def test_undoing_something_nobody_dismissed_is_refused(self):
        db = self._db()
        self._track(db, "A" * 22, "Song", plays=5)

        for trackId in ("A" * 22, "Z" * 22):
            with self.assertRaises(ValueError):
                db.repo.undismissMergeCandidate(trackId)


if __name__ == "__main__":
    unittest.main()
