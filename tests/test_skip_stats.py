"""Skip statistics: per-entity counts for the detail pages, and the Charts
page's most-skipped songs/artists lists.

The ranking decision these pin: most-skipped is ordered by skip RATE, not by
raw skip count. Count alone just resurfaces whatever is played most. A raw rate
is meaningless at low volume (one skip and no plays is 100%), so the rate used
for ORDERING is shrunk toward the library's own average; the percentage SHOWN
stays the true one. Both raw numbers are returned so the UI can show either.
"""
import datetime
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest
import Database.utils as utilsModule


def _ts(year, month=6, day=1, hour=12):
    return int(datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc).timestamp())


class SkipStatsTestCase(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(utilsModule, "tz", datetime.timezone.utc)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = self._makeDb({}, [])

    def _track(self, trackId, artistId="art1", artistName="Artist One", albumId="alb1"):
        self.db.repo.upsertTrack(normalizeTrackForTest({
            "id": trackId, "name": f"Song {trackId}", "imageId": albumId,
            "artists": [{"id": artistId, "name": artistName}],
        }))

    def _record(self, trackId, plays=0, skips=0, year=2026, startHour=0):
        """`plays` real plays and `skips` skip rows for one track."""
        hour = startHour
        for _ in range(plays):
            self.db.repo.insertPlay(self.db.user, trackId, _ts(year, hour=hour % 24) + hour, 200000, is_skip=0)
            hour += 1
        for _ in range(skips):
            self.db.repo.insertPlay(self.db.user, trackId, _ts(year, hour=hour % 24) + hour, 2000, is_skip=1)
            hour += 1
        self.db.repo.commit()


class TestPerEntitySkipStats(SkipStatsTestCase):
    """What the song/artist/album detail pages show."""

    def test_counts_plays_and_skips_for_one_track(self):
        self._track("t1")
        self._record("t1", plays=7, skips=3)

        stats = self.db.getSkipStats(trackId="t1")

        self.assertEqual(stats["plays"], 7)
        self.assertEqual(stats["skips"], 3)

    def test_percent_is_share_of_encounters_not_of_plays(self):
        """3 skips out of 10 times it came up = 30%, not 3/7."""
        self._track("t1")
        self._record("t1", plays=7, skips=3)

        self.assertEqual(self.db.getSkipStats(trackId="t1")["skipPercent"], 30.0)

    def test_a_never_skipped_track_reports_zero(self):
        self._track("t1")
        self._record("t1", plays=5, skips=0)

        stats = self.db.getSkipStats(trackId="t1")

        self.assertEqual(stats["skips"], 0)
        self.assertEqual(stats["skipPercent"], 0.0)

    def test_an_unheard_track_does_not_divide_by_zero(self):
        self._track("t1")

        self.assertEqual(self.db.getSkipStats(trackId="t1"),
                         {"plays": 0, "skips": 0, "skipPercent": 0.0})

    def test_scopes_to_an_artist(self):
        self._track("t1", artistId="artA")
        self._track("t2", artistId="artB")
        self._record("t1", plays=1, skips=4)
        self._record("t2", plays=9, skips=1, startHour=12)

        self.assertEqual(self.db.getSkipStats(artistId="artA")["skips"], 4)
        self.assertEqual(self.db.getSkipStats(artistId="artB")["skips"], 1)

    def test_scopes_to_an_album(self):
        self._track("t1", albumId="albA")
        self._track("t2", albumId="albB")
        self._record("t1", plays=1, skips=3)
        self._record("t2", plays=5, skips=0, startHour=12)

        self.assertEqual(self.db.getSkipStats(albumId="albA")["skips"], 3)
        self.assertEqual(self.db.getSkipStats(albumId="albB")["skips"], 0)

    def test_respects_the_date_range(self):
        self._track("t1")
        self._record("t1", plays=1, skips=1, year=2024)
        self._record("t1", plays=1, skips=4, year=2026)

        stats = self.db.getSkipStats(
            startDate=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2027, 1, 1, tzinfo=datetime.timezone.utc),
            trackId="t1")

        self.assertEqual(stats["skips"], 4)


class TestMostSkippedSongs(SkipStatsTestCase):
    def test_ranks_by_rate_not_by_raw_count(self):
        """The whole ranking decision in one test: `often` has more skips, but
        `always` is skipped a far higher share of the time."""
        self._track("often")
        self._track("always")
        self._record("often", plays=40, skips=10)          #< 10 skips, 20%
        self._record("always", plays=1, skips=9, startHour=60)   #< 9 skips, 90%

        ranked = self.db.getMostSkippedSongs(limit=10)

        self.assertEqual([s["id"] for s in ranked], ["always", "often"])

    def test_reports_both_numbers(self):
        self._track("t1")
        self._record("t1", plays=6, skips=4)

        entry = self.db.getMostSkippedSongs(limit=10)[0]

        self.assertEqual(entry["skips"], 4)
        self.assertEqual(entry["plays"], 6)
        self.assertEqual(entry["encounters"], 10)
        self.assertEqual(entry["skipPercent"], 40.0)

    def test_a_barely_heard_track_is_listed_but_not_ranked_top(self):
        """A track skipped once and never played is a raw 100% rate. It used to
        be hidden by a minimum-encounters cutoff; now it appears (nothing is
        silently missing from a paged list) but is pulled toward the library
        average, so a genuinely-skipped track with real volume outranks it."""
        self._track("bulk")
        self._record("bulk", plays=100, skips=0)   #< the rest of the library, so the average is a normal ~22%
        self._track("noise")
        self._record("noise", plays=0, skips=1, startHour=110)      #< raw 100%, one encounter
        self._track("proven")
        self._record("proven", plays=10, skips=30, startHour=120)   #< raw 75%, 40 encounters

        ranked = [s["id"] for s in self.db.getMostSkippedSongs(limit=10)]

        self.assertEqual(ranked, ["proven", "noise"])

    def test_low_volume_rows_drift_toward_the_library_average_not_toward_zero(self):
        """Which way the shrinkage pulls, not just that it happens.

        This library is skipped 43% of the time overall, so a thin row is not
        assumed innocent - `thin` starts from that average and its own 100%
        carries it past `steady`, which is skipped LESS than this user's norm.
        Shrinking toward zero instead would order these the other way round."""
        self._track("thin")
        self._record("thin", plays=0, skips=1)                    #< 100% on 1 encounter
        self._track("steady")
        self._record("steady", plays=12, skips=8, startHour=20)   #< 40% on 20, just under the norm

        ranked = [s["id"] for s in self.db.getMostSkippedSongs(limit=10)]

        self.assertEqual(ranked, ["thin", "steady"])

    def test_the_displayed_percentage_stays_the_true_one(self):
        """Shrinkage decides the ORDER; the number shown is the real rate."""
        self._track("noise")
        self._record("noise", plays=0, skips=1)

        self.assertEqual(self.db.getMostSkippedSongs(limit=10)[0]["skipPercent"], 100.0)

    def test_volume_breaks_the_tie_between_equal_rates(self):
        """Same raw rate, different evidence - the better-evidenced one wins."""
        self._track("thin")
        self._record("thin", plays=1, skips=1)              #< 50% on 2 encounters
        self._track("thick")
        self._record("thick", plays=20, skips=20, startHour=20)   #< 50% on 40

        ranked = [s["id"] for s in self.db.getMostSkippedSongs(limit=10)]

        self.assertEqual(ranked, ["thick", "thin"])

    def test_never_skipped_tracks_are_omitted_entirely(self):
        self._track("loved")
        self._record("loved", plays=20, skips=0)

        self.assertEqual(self.db.getMostSkippedSongs(limit=10), [])

    def test_carries_track_metadata_for_the_card(self):
        self._track("t1")
        self._record("t1", plays=5, skips=5)

        entry = self.db.getMostSkippedSongs(limit=10)[0]

        self.assertEqual(entry["name"], "Song t1")
        self.assertEqual([a["name"] for a in entry["artists"]], ["Artist One"])

    def test_honours_the_limit(self):
        for index in range(5):
            self._track(f"t{index}")
            self._record(f"t{index}", plays=5, skips=5, startHour=index * 20)

        self.assertEqual(len(self.db.getMostSkippedSongs(limit=3)), 3)

    def test_respects_the_date_range(self):
        self._track("t1")
        self._record("t1", plays=5, skips=5, year=2024)

        ranked = self.db.getMostSkippedSongs(
            startDate=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
            endDate=datetime.datetime(2027, 1, 1, tzinfo=datetime.timezone.utc),
            limit=10)

        self.assertEqual(ranked, [])


class TestMostSkippedArtists(SkipStatsTestCase):
    def test_ranks_artists_by_rate(self):
        self._track("t1", artistId="skipped", artistName="Skipped Artist")
        self._track("t2", artistId="loved", artistName="Loved Artist")
        self._record("t1", plays=2, skips=8)
        self._record("t2", plays=18, skips=2, startHour=40)

        ranked = self.db.getMostSkippedArtists(limit=10)

        self.assertEqual([a["name"] for a in ranked], ["Skipped Artist", "Loved Artist"])
        self.assertEqual(ranked[0]["skipPercent"], 80.0)

    def test_aggregates_across_an_artists_tracks(self):
        self._track("t1", artistId="art1")
        self._track("t2", artistId="art1")
        self._record("t1", plays=1, skips=4)
        self._record("t2", plays=1, skips=4, startHour=20)

        entry = self.db.getMostSkippedArtists(limit=10)[0]

        self.assertEqual(entry["skips"], 8)
        self.assertEqual(entry["encounters"], 10)

    def test_a_collaboration_counts_for_every_credited_artist(self):
        """Matches how every other artist aggregate treats multi-artist tracks."""
        self.db.repo.upsertTrack(normalizeTrackForTest({
            "id": "duet", "name": "Duet", "imageId": "alb1",
            "artists": [{"id": "artA", "name": "A"}, {"id": "artB", "name": "B"}],
        }))
        self._record("duet", plays=2, skips=6)

        ranked = {a["id"]: a for a in self.db.getMostSkippedArtists(limit=10)}

        self.assertEqual(ranked["artA"]["skips"], 6)
        self.assertEqual(ranked["artB"]["skips"], 6)

    def test_a_barely_heard_artist_is_listed_but_ranked_below_a_proven_one(self):
        """Same shrinkage as the song list - see
        TestMostSkippedSongs.test_a_barely_heard_track_is_listed_but_not_ranked_top."""
        self._track("bulk", artistId="bulk")
        self._record("bulk", plays=100, skips=0)
        self._track("t1", artistId="rare")
        self._record("t1", plays=0, skips=2, startHour=110)          #< raw 100%, 2 encounters
        self._track("t2", artistId="proven")
        self._record("t2", plays=10, skips=30, startHour=120)        #< raw 75%, 40 encounters

        ranked = [a["id"] for a in self.db.getMostSkippedArtists(limit=10)]

        self.assertEqual(ranked, ["proven", "rare"])

    def test_carries_the_id_for_click_through(self):
        self._track("t1", artistId="art1", artistName="Artist One")
        self._record("t1", plays=5, skips=5)

        self.assertEqual(self.db.getMostSkippedArtists(limit=10)[0]["id"], "art1")


if __name__ == "__main__":
    unittest.main()
