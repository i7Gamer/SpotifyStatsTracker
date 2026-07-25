"""Instance-wide skip threshold + numeric tunables, stored in app_settings.

The skip threshold is the single admin-tunable boundary between a skip and a
real listen (it replaced the old play_skips split and the 30s completion line).
computeIsSkip is the one classifier; recomputeSkipFlags re-materializes
plays.is_skip when the threshold changes. getIntSetting backs the migrated
numeric constants (Discover count, worker pool sizes).
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from unittest.mock import patch

from conftest import DatabaseTestCase
from Database.db import SKIP_THRESHOLD_MS
from Database.repository import (
    SKIP_THRESHOLD_MODE_KEY, SKIP_THRESHOLD_VALUE_KEY,
    SKIP_MODE_SECONDS, SKIP_MODE_PERCENT,
    SKIP_SECONDS_MIN, SKIP_SECONDS_MAX, SKIP_PERCENT_MIN, SKIP_PERCENT_MAX,
    SKIP_THRESHOLD_DEFAULT_MODE, SKIP_THRESHOLD_DEFAULT_VALUE,
    DISCOVER_ARTIST_LIMIT_KEY, DISCOVER_ARTIST_LIMIT_MIN, DISCOVER_ARTIST_LIMIT_MAX,
    COMPLETION_COMPLETE_PERCENT_KEY, COMPLETION_COMPLETE_PERCENT_MIN,
    COMPLETION_COMPLETE_PERCENT_MAX, COMPLETION_COMPLETE_PERCENT_DEFAULT,
    MS_PER_SECOND, PERCENT_DIVISOR,
)

# Fixtures for the "a completed play is never a skip" invariant. SHORT_TRACK_MS
# is shorter than RAISED_SECONDS_THRESHOLD (so the seconds threshold has to cap
# against it) and divides evenly by every completion percent used here.
SHORT_TRACK_MS = 20_000
LONG_TRACK_MS = 200_000
RAISED_SECONDS_THRESHOLD = 30
LOW_COMPLETION_PERCENT = COMPLETION_COMPLETE_PERCENT_MIN     #< 50%
FULL_COMPLETION_PERCENT = COMPLETION_COMPLETE_PERCENT_MAX    #< 100%
# Above every completion percent, and deliberately outside the admin form's
# SKIP_PERCENT_MAX range: the classifier still has to hold when a caller hands
# it an unclamped threshold (computeIsSkip's `threshold` argument does exactly
# that) or when the bounds are widened later.
CONTRADICTORY_SKIP_PERCENT = 90
MODERATE_SKIP_PERCENT = 20                                   #< well under every completion percent


def _completeAt(durationMs: int, percent: int = COMPLETION_COMPLETE_PERCENT_DEFAULT) -> int:
    """The smallest time_played that getCompletionCounts calls "complete"."""
    return int(durationMs * percent / PERCENT_DIVISOR)


class SkipThresholdSettingsTestCase(DatabaseTestCase):
    def test_defaults_to_seconds_5_when_unset(self):
        db = self._makeDb({}, [])
        mode, value = db.repo.getSkipThreshold()
        self.assertEqual(mode, SKIP_MODE_SECONDS)
        self.assertEqual(value, 5)
        self.assertEqual((SKIP_THRESHOLD_DEFAULT_MODE, SKIP_THRESHOLD_DEFAULT_VALUE), (SKIP_MODE_SECONDS, 5))
        self.assertIsNone(db.repo.getAppSetting(SKIP_THRESHOLD_MODE_KEY))

    def test_round_trips_seconds_and_percent(self):
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, 30)
        self.assertEqual(db.repo.getSkipThreshold(), (SKIP_MODE_SECONDS, 30))
        db.repo.setSkipThreshold(SKIP_MODE_PERCENT, 20)
        self.assertEqual(db.repo.getSkipThreshold(), (SKIP_MODE_PERCENT, 20))

    def test_clamps_seconds_to_bounds(self):
        db = self._makeDb({}, [])
        self.assertEqual(db.repo.setSkipThreshold(SKIP_MODE_SECONDS, 1), (SKIP_MODE_SECONDS, SKIP_SECONDS_MIN))
        self.assertEqual(db.repo.setSkipThreshold(SKIP_MODE_SECONDS, 999), (SKIP_MODE_SECONDS, SKIP_SECONDS_MAX))

    def test_clamps_percent_to_bounds(self):
        db = self._makeDb({}, [])
        self.assertEqual(db.repo.setSkipThreshold(SKIP_MODE_PERCENT, 0), (SKIP_MODE_PERCENT, SKIP_PERCENT_MIN))
        self.assertEqual(db.repo.setSkipThreshold(SKIP_MODE_PERCENT, 100), (SKIP_MODE_PERCENT, SKIP_PERCENT_MAX))

    def test_unknown_mode_rejected(self):
        db = self._makeDb({}, [])
        with self.assertRaises(ValueError):
            db.repo.setSkipThreshold("half", 10)

    def test_corrupt_stored_value_falls_back_to_default(self):
        db = self._makeDb({}, [])
        db.repo.setAppSetting(SKIP_THRESHOLD_MODE_KEY, SKIP_MODE_SECONDS)
        db.repo.setAppSetting(SKIP_THRESHOLD_VALUE_KEY, "not-a-number")
        self.assertEqual(db.repo.getSkipThreshold(), (SKIP_MODE_SECONDS, SKIP_THRESHOLD_DEFAULT_VALUE))


class ComputeIsSkipTestCase(DatabaseTestCase):
    def test_seconds_mode_boundary(self):
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, 30)
        self.assertEqual(db.repo.computeIsSkip(29_999), 1)   #< just under 30s
        self.assertEqual(db.repo.computeIsSkip(30_000), 0)   #< exactly 30s is a real play
        self.assertEqual(db.repo.computeIsSkip(60_000), 0)

    def test_percent_mode_uses_duration(self):
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_PERCENT, 20)
        # 20% of a 200s track = 40s
        self.assertEqual(db.repo.computeIsSkip(39_000, durationMs=200_000), 1)
        self.assertEqual(db.repo.computeIsSkip(40_000, durationMs=200_000), 0)

    def test_percent_mode_unknown_duration_uses_floor(self):
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_PERCENT, 25)
        # No duration -> fall back to the fixed sub-5s floor.
        self.assertEqual(db.repo.computeIsSkip(SKIP_THRESHOLD_MS - 1, durationMs=0), 1)
        self.assertEqual(db.repo.computeIsSkip(SKIP_THRESHOLD_MS, durationMs=None), 0)
        self.assertEqual(db.repo.computeIsSkip(10_000, durationMs=0), 0)   #< 10s, no duration -> not a skip

    def test_threshold_arg_avoids_settings_read(self):
        db = self._makeDb({}, [])
        # Stored threshold is the default (5s), but an explicit override wins.
        self.assertEqual(db.repo.computeIsSkip(10_000, threshold=(SKIP_MODE_SECONDS, 30)), 1)

    def test_seconds_mode_never_calls_a_completed_track_a_skip(self):
        """A track SHORTER than the threshold can never reach it, so a duration-
        blind comparison marks every play of it - including one that ran to the
        last millisecond - as a skip, permanently and by construction. Real
        case: "From Zero (Intro)" is 22.174s, and under a 30s threshold all 17
        of its complete plays (Spotify's own export: reason_end=trackdone,
        skipped=false) were reported as a 100% skip rate."""
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, 30)
        INTRO_MS = 22_174

        self.assertEqual(db.repo.computeIsSkip(INTRO_MS, durationMs=INTRO_MS), 0)
        # Partial listens of that same short track are still skips - but only
        # the ones that stopped short of the completion percent, see
        # CompletionInvariantTestCase (a play at 99% of it is "complete", so
        # calling it a skip would be the same contradiction one step further in).
        self.assertEqual(db.repo.computeIsSkip(_completeAt(INTRO_MS) - 1, durationMs=INTRO_MS), 1)
        self.assertEqual(db.repo.computeIsSkip(1_000, durationMs=INTRO_MS), 1)

    def test_seconds_mode_unaffected_when_track_is_longer_than_threshold(self):
        """The duration cap must only ever loosen the rule for short tracks -
        a normal-length track keeps the plain threshold semantics."""
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, 30)

        self.assertEqual(db.repo.computeIsSkip(29_999, durationMs=200_000), 1)
        self.assertEqual(db.repo.computeIsSkip(30_000, durationMs=200_000), 0)

    def test_seconds_mode_without_duration_keeps_the_plain_threshold(self):
        """Duration is optional at most call sites, so an unknown duration must
        not change the existing answer."""
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, 30)

        self.assertEqual(db.repo.computeIsSkip(29_999), 1)
        self.assertEqual(db.repo.computeIsSkip(29_999, durationMs=0), 1)
        self.assertEqual(db.repo.computeIsSkip(29_999, durationMs=None), 1)


class CompletionInvariantTestCase(DatabaseTestCase):
    """A play that counts as COMPLETE can never also be a SKIP.

    The instance has two independent tunables: the skip threshold and
    getCompletionCompletePercent (the complete-vs-partial boundary the Charts
    completion pie and the "Full plays only" filter use). Nothing tied them
    together, so they could contradict each other - the same play reported as
    "listened to the end" by one and "abandoned" by the other, on the same page.

    Seconds mode reached that state through the duration cap: a track shorter
    than the threshold is compared against its own length, so a play at 95% of
    a 20s intro was a skip while the completion pie called it complete. Percent
    mode reaches it whenever the skip percent is set above the completion
    percent. The classifier now caps at the completion boundary in both modes,
    which is the widest cap that can never turn a real skip into a play."""

    def _setCompletionPercent(self, db, percent):
        db.repo.setIntSetting(COMPLETION_COMPLETE_PERCENT_KEY, percent,
                              COMPLETION_COMPLETE_PERCENT_MIN, COMPLETION_COMPLETE_PERCENT_MAX)

    # ---- seconds mode --------------------------------------------------------

    def test_seconds_mode_completed_play_of_a_short_track_is_not_a_skip(self):
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD)
        completeMs = _completeAt(SHORT_TRACK_MS)

        self.assertEqual(db.repo.computeIsSkip(completeMs, durationMs=SHORT_TRACK_MS), 0)
        self.assertEqual(db.repo.computeIsSkip(completeMs + 1, durationMs=SHORT_TRACK_MS), 0)
        # One millisecond below the completion boundary is still a skip: the cap
        # must land exactly on the boundary, not near it.
        self.assertEqual(db.repo.computeIsSkip(completeMs - 1, durationMs=SHORT_TRACK_MS), 1)

    def test_seconds_mode_follows_a_lowered_completion_percent(self):
        """Half a track counts as complete at 50%, so it stops being a skip -
        the cap tracks the setting rather than a second hardcoded boundary."""
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD)
        self._setCompletionPercent(db, LOW_COMPLETION_PERCENT)
        completeMs = _completeAt(SHORT_TRACK_MS, LOW_COMPLETION_PERCENT)

        self.assertEqual(db.repo.computeIsSkip(completeMs, durationMs=SHORT_TRACK_MS), 0)
        self.assertEqual(db.repo.computeIsSkip(completeMs - 1, durationMs=SHORT_TRACK_MS), 1)

    def test_seconds_mode_at_full_completion_percent_keeps_the_duration_cap(self):
        """At 100% only a play that reaches the last millisecond is complete,
        which is exactly the pre-existing duration cap - the invariant has to
        subsume it, not replace it with something looser."""
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD)
        self._setCompletionPercent(db, FULL_COMPLETION_PERCENT)

        self.assertEqual(db.repo.computeIsSkip(SHORT_TRACK_MS, durationMs=SHORT_TRACK_MS), 0)
        self.assertEqual(db.repo.computeIsSkip(SHORT_TRACK_MS - 1, durationMs=SHORT_TRACK_MS), 1)

    def test_seconds_mode_normal_track_is_untouched_by_the_completion_cap(self):
        """The cap only ever loosens, and only where the two settings disagree:
        on a track long enough for the threshold to be reachable, the plain
        threshold still decides."""
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD)
        thresholdMs = RAISED_SECONDS_THRESHOLD * MS_PER_SECOND

        self.assertEqual(db.repo.computeIsSkip(thresholdMs - 1, durationMs=LONG_TRACK_MS), 1)
        self.assertEqual(db.repo.computeIsSkip(thresholdMs, durationMs=LONG_TRACK_MS), 0)

    # ---- percent mode --------------------------------------------------------

    def test_percent_mode_completed_play_is_not_a_skip(self):
        """Skip 90% vs complete 80%: a play at 85% is complete AND a skip under
        a duration-only comparison. It must come out a real play."""
        db = self._makeDb({}, [])
        threshold = (SKIP_MODE_PERCENT, CONTRADICTORY_SKIP_PERCENT)
        completeMs = _completeAt(LONG_TRACK_MS)

        self.assertEqual(db.repo.computeIsSkip(completeMs, durationMs=LONG_TRACK_MS,
                                               threshold=threshold), 0)
        self.assertEqual(db.repo.computeIsSkip(completeMs + 10_000, durationMs=LONG_TRACK_MS,
                                               threshold=threshold), 0)
        self.assertEqual(db.repo.computeIsSkip(completeMs - 1, durationMs=LONG_TRACK_MS,
                                               threshold=threshold), 1)

    def test_percent_mode_below_the_completion_percent_keeps_the_skip_percent(self):
        """The cap must not drag the skip boundary UP to the completion line:
        with skip 20% and complete 80%, a play at 25% is a partial listen, not
        a skip."""
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_PERCENT, MODERATE_SKIP_PERCENT)
        thresholdMs = int(LONG_TRACK_MS * MODERATE_SKIP_PERCENT / PERCENT_DIVISOR)

        self.assertEqual(db.repo.computeIsSkip(thresholdMs - 1, durationMs=LONG_TRACK_MS), 1)
        self.assertEqual(db.repo.computeIsSkip(thresholdMs, durationMs=LONG_TRACK_MS), 0)
        # Well past the skip line but nowhere near complete: still a real play.
        self.assertEqual(db.repo.computeIsSkip(thresholdMs * 2, durationMs=LONG_TRACK_MS), 0)

    def test_percent_mode_follows_a_lowered_completion_percent(self):
        db = self._makeDb({}, [])
        self._setCompletionPercent(db, LOW_COMPLETION_PERCENT)
        threshold = (SKIP_MODE_PERCENT, CONTRADICTORY_SKIP_PERCENT)
        completeMs = _completeAt(LONG_TRACK_MS, LOW_COMPLETION_PERCENT)

        self.assertEqual(db.repo.computeIsSkip(completeMs, durationMs=LONG_TRACK_MS,
                                               threshold=threshold), 0)
        self.assertEqual(db.repo.computeIsSkip(completeMs - 1, durationMs=LONG_TRACK_MS,
                                               threshold=threshold), 1)

    # ---- unchanged fallbacks + genuine skips ---------------------------------

    def test_unknown_duration_fallbacks_are_unchanged(self):
        """With no duration there is no completion boundary to respect, so both
        modes keep their documented fallback regardless of the setting."""
        db = self._makeDb({}, [])
        self._setCompletionPercent(db, LOW_COMPLETION_PERCENT)

        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD)
        thresholdMs = RAISED_SECONDS_THRESHOLD * MS_PER_SECOND
        self.assertEqual(db.repo.computeIsSkip(thresholdMs - 1, durationMs=None), 1)
        self.assertEqual(db.repo.computeIsSkip(thresholdMs - 1, durationMs=0), 1)
        self.assertEqual(db.repo.computeIsSkip(thresholdMs, durationMs=0), 0)

        db.repo.setSkipThreshold(SKIP_MODE_PERCENT, SKIP_PERCENT_MAX)
        self.assertEqual(db.repo.computeIsSkip(SKIP_THRESHOLD_MS - 1, durationMs=0), 1)
        self.assertEqual(db.repo.computeIsSkip(SKIP_THRESHOLD_MS, durationMs=None), 0)

    def test_genuinely_abandoned_plays_still_classify_as_skips(self):
        """The invariant is a ceiling on the threshold, not an amnesty."""
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD)
        self.assertEqual(db.repo.computeIsSkip(3_000, durationMs=LONG_TRACK_MS), 1)
        self.assertEqual(db.repo.computeIsSkip(3_000, durationMs=SHORT_TRACK_MS), 1)

        db.repo.setSkipThreshold(SKIP_MODE_PERCENT, SKIP_PERCENT_MAX)
        self.assertEqual(db.repo.computeIsSkip(3_000, durationMs=LONG_TRACK_MS), 1)
        self.assertEqual(db.repo.computeIsSkip(3_000, durationMs=SHORT_TRACK_MS), 1)

    def test_explicit_completion_percent_argument_wins_over_the_setting(self):
        """Bulk loops hoist both settings out of the per-row path; the override
        has to be honoured or the hoist would silently classify differently."""
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD)
        completeMs = _completeAt(SHORT_TRACK_MS, LOW_COMPLETION_PERCENT)

        # Stored completion percent is the default (80%), so completeMs (50%)
        # would be a skip; the explicit 50% override makes it a real play.
        self.assertEqual(db.repo.computeIsSkip(completeMs, durationMs=SHORT_TRACK_MS), 1)
        self.assertEqual(db.repo.computeIsSkip(completeMs, durationMs=SHORT_TRACK_MS,
                                               completionPercent=LOW_COMPLETION_PERCENT), 0)


class IntSettingTestCase(DatabaseTestCase):
    def test_default_when_unset(self):
        db = self._makeDb({}, [])
        self.assertEqual(db.repo.getDiscoverArtistLimit(5), 5)
        self.assertIsNone(db.repo.getAppSetting(DISCOVER_ARTIST_LIMIT_KEY))

    def test_clamps_and_round_trips(self):
        db = self._makeDb({}, [])
        self.assertEqual(db.repo.setIntSetting(DISCOVER_ARTIST_LIMIT_KEY, 999,
                                               DISCOVER_ARTIST_LIMIT_MIN, DISCOVER_ARTIST_LIMIT_MAX),
                         DISCOVER_ARTIST_LIMIT_MAX)
        self.assertEqual(db.repo.getDiscoverArtistLimit(5), DISCOVER_ARTIST_LIMIT_MAX)

    def test_bad_stored_value_falls_back_to_default(self):
        db = self._makeDb({}, [])
        db.repo.setAppSetting(DISCOVER_ARTIST_LIMIT_KEY, "lots")
        self.assertEqual(db.repo.getDiscoverArtistLimit(7), 7)


class RecomputeSkipFlagsTestCase(DatabaseTestCase):
    """Needs the plays.is_skip column (schema change) + real plays."""

    def _skipFlags(self, db, username="testuser"):
        rows = db.repo._conn().execute(
            "SELECT track_id, time_played, is_skip FROM plays WHERE username=? ORDER BY track_id", (username,)
        ).fetchall()
        return {r["track_id"]: r["is_skip"] for r in rows}

    def test_seconds_mode_reclassifies_all_rows(self):
        tracks = {"short": {"id": "short", "name": "Short", "artists": []},
                  "long": {"id": "long", "name": "Long", "artists": []}}
        entries = [
            {"id": "short", "playedAt": 1000.0, "timePlayed": 10_000},   #< 10s
            {"id": "long", "playedAt": 2000.0, "timePlayed": 200_000},   #< 200s
        ]
        db = self._makeDb(tracks, entries)
        # Default 5s: neither is a skip.
        self.assertEqual(self._skipFlags(db), {"short": 0, "long": 0})
        # Raise to 30s and recompute: the 10s play becomes a skip.
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, 30)
        processed = db.repo.recomputeSkipFlags()
        self.assertEqual(processed, 2)
        self.assertEqual(self._skipFlags(db), {"short": 1, "long": 0})

    def test_seconds_mode_keeps_completed_short_tracks_out_of_the_skips(self):
        """The bulk rewrite has its own SQL, so it needs the same duration cap
        as computeIsSkip - otherwise raising the threshold above a short track's
        length silently reclassifies its complete plays as skips. Mirrors the
        real 22.174s intro that reported a 100% skip rate under a 30s
        threshold."""
        INTRO_MS = 22_174
        tracks = {
            "intro": {"id": "intro", "name": "Intro", "artists": [], "duration": INTRO_MS},
            "long": {"id": "long", "name": "Long", "artists": [], "duration": 200_000},
        }
        entries = [
            {"id": "intro", "playedAt": 1000.0, "timePlayed": INTRO_MS},    #< played to the end
            {"id": "long", "playedAt": 2000.0, "timePlayed": 25_000},       #< 25s of 200s
        ]
        db = self._makeDb(tracks, entries)
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, 30)
        db.repo.recomputeSkipFlags()

        # The intro finished, so it is a real play; the long track was abandoned
        # before 30s, so it stays a skip.
        self.assertEqual(self._skipFlags(db), {"intro": 0, "long": 1})

    def test_seconds_mode_still_skips_a_partial_play_of_a_short_track(self):
        INTRO_MS = 22_174
        tracks = {"intro": {"id": "intro", "name": "Intro", "artists": [], "duration": INTRO_MS}}
        entries = [{"id": "intro", "playedAt": 1000.0, "timePlayed": 5_000}]
        db = self._makeDb(tracks, entries)
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, 30)
        db.repo.recomputeSkipFlags()

        self.assertEqual(self._skipFlags(db)["intro"], 1)

    def test_seconds_mode_unknown_duration_keeps_the_plain_threshold(self):
        """A track with no usable duration must fall back to the raw threshold
        rather than the COALESCE swallowing the row."""
        tracks = {"t": {"id": "t", "name": "T", "artists": [], "duration": 0}}
        entries = [{"id": "t", "playedAt": 1000.0, "timePlayed": 10_000}]
        db = self._makeDb(tracks, entries)
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, 30)
        db.repo.recomputeSkipFlags()

        self.assertEqual(self._skipFlags(db)["t"], 1)   #< 10s < 30s, duration unknown

    def test_percent_mode_reclassifies_with_duration(self):
        tracks = {"t": {"id": "t", "name": "T", "artists": [], "duration": 200_000}}
        entries = [{"id": "t", "playedAt": 1000.0, "timePlayed": 30_000}]   #< 30s of a 200s track = 15%
        db = self._makeDb(tracks, entries)
        db.repo.setSkipThreshold(SKIP_MODE_PERCENT, 20)   #< 20% = 40s threshold
        db.repo.recomputeSkipFlags()
        self.assertEqual(self._skipFlags(db)["t"], 1)     #< 30s < 40s -> skip
        db.repo.setSkipThreshold(SKIP_MODE_PERCENT, 10)   #< 10% = 20s threshold
        db.repo.recomputeSkipFlags()
        self.assertEqual(self._skipFlags(db)["t"], 0)     #< 30s >= 20s -> real play

    def test_percent_mode_unknown_duration_uses_floor(self):
        tracks = {"t": {"id": "t", "name": "T", "artists": [], "duration": 0}}
        entries = [{"id": "t", "playedAt": 1000.0, "timePlayed": 10_000}]   #< 10s, unknown duration
        db = self._makeDb(tracks, entries)
        db.repo.setSkipThreshold(SKIP_MODE_PERCENT, 25)
        db.repo.recomputeSkipFlags()
        # Unknown duration falls back to the <5s floor, so a 10s play is NOT a skip.
        self.assertEqual(self._skipFlags(db)["t"], 0)


class RecomputeCompletionInvariantTestCase(DatabaseTestCase):
    """The same "complete is never a skip" invariant, through the bulk rewrite.

    recomputeSkipFlags does the classification in its own SQL rather than
    calling computeIsSkip per row, so the rule exists twice and the two have
    drifted apart before. Every case here has a computeIsSkip twin above."""

    _skipFlags = RecomputeSkipFlagsTestCase._skipFlags

    def _setCompletionPercent(self, db, percent):
        db.repo.setIntSetting(COMPLETION_COMPLETE_PERCENT_KEY, percent,
                              COMPLETION_COMPLETE_PERCENT_MIN, COMPLETION_COMPLETE_PERCENT_MAX)

    def _shortTrackDb(self, completeMs, partialMs):
        """Two plays of equally short tracks - one at/over the completion
        boundary, one under it - plus a genuinely abandoned normal-length play."""
        tracks = {
            "complete": {"id": "complete", "name": "Complete", "artists": [], "duration": SHORT_TRACK_MS},
            "partial": {"id": "partial", "name": "Partial", "artists": [], "duration": SHORT_TRACK_MS},
            "long": {"id": "long", "name": "Long", "artists": [], "duration": LONG_TRACK_MS},
        }
        entries = [
            {"id": "complete", "playedAt": 1000.0, "timePlayed": completeMs},
            {"id": "partial", "playedAt": 2000.0, "timePlayed": partialMs},
            {"id": "long", "playedAt": 3000.0, "timePlayed": 3_000},
        ]
        return self._makeDb(tracks, entries)

    def test_seconds_mode_leaves_completed_plays_of_short_tracks_alone(self):
        completeMs = _completeAt(SHORT_TRACK_MS)
        db = self._shortTrackDb(completeMs, completeMs - 1)
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD)
        db.repo.recomputeSkipFlags()

        self.assertEqual(self._skipFlags(db), {"complete": 0, "partial": 1, "long": 1})

    def test_seconds_mode_follows_a_lowered_completion_percent(self):
        completeMs = _completeAt(SHORT_TRACK_MS, LOW_COMPLETION_PERCENT)
        db = self._shortTrackDb(completeMs, completeMs - 1)
        self._setCompletionPercent(db, LOW_COMPLETION_PERCENT)
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD)
        db.repo.recomputeSkipFlags()

        self.assertEqual(self._skipFlags(db), {"complete": 0, "partial": 1, "long": 1})

    def test_seconds_mode_at_full_completion_percent_keeps_the_duration_cap(self):
        db = self._shortTrackDb(SHORT_TRACK_MS, SHORT_TRACK_MS - 1)
        self._setCompletionPercent(db, FULL_COMPLETION_PERCENT)
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD)
        db.repo.recomputeSkipFlags()

        self.assertEqual(self._skipFlags(db), {"complete": 0, "partial": 1, "long": 1})

    def test_percent_mode_leaves_completed_plays_alone(self):
        """Skip 90% vs complete 80%. The stored setting is clamped to
        SKIP_PERCENT_MAX, so the contradiction is injected at the accessor -
        the SQL still has to defend against it, both because computeIsSkip's
        `threshold` argument accepts unclamped values and because the bounds
        are one edit away from allowing it."""
        completeMs = _completeAt(LONG_TRACK_MS)
        tracks = {
            "complete": {"id": "complete", "name": "Complete", "artists": [], "duration": LONG_TRACK_MS},
            "partial": {"id": "partial", "name": "Partial", "artists": [], "duration": LONG_TRACK_MS},
        }
        entries = [
            {"id": "complete", "playedAt": 1000.0, "timePlayed": completeMs},
            {"id": "partial", "playedAt": 2000.0, "timePlayed": completeMs - 1},
        ]
        db = self._makeDb(tracks, entries)
        with patch.object(db.repo, "getSkipThreshold",
                          return_value=(SKIP_MODE_PERCENT, CONTRADICTORY_SKIP_PERCENT)):
            db.repo.recomputeSkipFlags()

        self.assertEqual(self._skipFlags(db), {"complete": 0, "partial": 1})

    def test_percent_mode_below_the_completion_percent_keeps_the_skip_percent(self):
        thresholdMs = int(LONG_TRACK_MS * MODERATE_SKIP_PERCENT / PERCENT_DIVISOR)
        tracks = {
            "over": {"id": "over", "name": "Over", "artists": [], "duration": LONG_TRACK_MS},
            "under": {"id": "under", "name": "Under", "artists": [], "duration": LONG_TRACK_MS},
        }
        entries = [
            {"id": "over", "playedAt": 1000.0, "timePlayed": thresholdMs},
            {"id": "under", "playedAt": 2000.0, "timePlayed": thresholdMs - 1},
        ]
        db = self._makeDb(tracks, entries)
        db.repo.setSkipThreshold(SKIP_MODE_PERCENT, MODERATE_SKIP_PERCENT)
        db.repo.recomputeSkipFlags()

        self.assertEqual(self._skipFlags(db), {"over": 0, "under": 1})


class SkipClassifierParityTestCase(DatabaseTestCase):
    """computeIsSkip (Python, one row) and recomputeSkipFlags (SQL, all rows)
    must return the same answer for the same row under the same settings.

    Pinned as a matrix rather than case by case because the two have diverged
    before, and because both boundaries are floating point once a percentage is
    involved - the SQL and the Python have to round the same way."""

    # (track id, duration_ms, time_played) - boundaries on both sides of the
    # skip line and the completion line, plus an unknown-duration row.
    PARITY_ROWS = [
        ("short_full", SHORT_TRACK_MS, SHORT_TRACK_MS),
        ("short_complete", SHORT_TRACK_MS, _completeAt(SHORT_TRACK_MS)),
        ("short_nearly", SHORT_TRACK_MS, _completeAt(SHORT_TRACK_MS) - 1),
        ("short_half", SHORT_TRACK_MS, _completeAt(SHORT_TRACK_MS, LOW_COMPLETION_PERCENT)),
        ("short_stub", SHORT_TRACK_MS, 1_000),
        ("long_complete", LONG_TRACK_MS, _completeAt(LONG_TRACK_MS)),
        ("long_partial", LONG_TRACK_MS, _completeAt(LONG_TRACK_MS) - 1),
        ("long_quarter", LONG_TRACK_MS, LONG_TRACK_MS // 4),
        ("long_abandoned", LONG_TRACK_MS, 3_000),
        ("unknown", 0, 10_000),
    ]

    PARITY_SETTINGS = [
        (SKIP_MODE_SECONDS, SKIP_THRESHOLD_DEFAULT_VALUE, COMPLETION_COMPLETE_PERCENT_DEFAULT),
        (SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD, COMPLETION_COMPLETE_PERCENT_DEFAULT),
        (SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD, FULL_COMPLETION_PERCENT),
        (SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD, LOW_COMPLETION_PERCENT),
        (SKIP_MODE_SECONDS, SKIP_SECONDS_MAX, LOW_COMPLETION_PERCENT),
        (SKIP_MODE_PERCENT, SKIP_PERCENT_MIN, COMPLETION_COMPLETE_PERCENT_DEFAULT),
        (SKIP_MODE_PERCENT, SKIP_PERCENT_MAX, LOW_COMPLETION_PERCENT),
        (SKIP_MODE_PERCENT, CONTRADICTORY_SKIP_PERCENT, COMPLETION_COMPLETE_PERCENT_DEFAULT),
        (SKIP_MODE_PERCENT, CONTRADICTORY_SKIP_PERCENT, FULL_COMPLETION_PERCENT),
    ]

    def test_sql_and_python_classify_every_row_identically(self):
        tracks = {trackId: {"id": trackId, "name": trackId, "artists": [], "duration": durationMs}
                  for trackId, durationMs, _ in self.PARITY_ROWS}
        entries = [{"id": trackId, "playedAt": 1000.0 + index, "timePlayed": timePlayed}
                   for index, (trackId, _, timePlayed) in enumerate(self.PARITY_ROWS)]
        db = self._makeDb(tracks, entries)

        for mode, value, completionPercent in self.PARITY_SETTINGS:
            with self.subTest(mode=mode, value=value, completionPercent=completionPercent):
                db.repo.setIntSetting(COMPLETION_COMPLETE_PERCENT_KEY, completionPercent,
                                      COMPLETION_COMPLETE_PERCENT_MIN, COMPLETION_COMPLETE_PERCENT_MAX)
                # Patched rather than stored: setSkipThreshold clamps, and the
                # contradictory percents are deliberately outside the bounds.
                with patch.object(db.repo, "getSkipThreshold", return_value=(mode, value)):
                    db.repo.recomputeSkipFlags()
                    rows = db.repo._conn().execute(
                        "SELECT p.track_id, p.time_played, p.is_skip, t.duration_ms "
                        "FROM plays p JOIN tracks t ON t.id = p.track_id"
                    ).fetchall()
                    self.assertEqual(len(rows), len(self.PARITY_ROWS))
                    for row in rows:
                        expected = db.repo.computeIsSkip(row["time_played"], row["duration_ms"])
                        self.assertEqual(row["is_skip"], expected,
                                         f"{row['track_id']}: SQL said {row['is_skip']}, Python said {expected}")

    def test_no_completed_play_is_ever_a_skip(self):
        """The invariant itself, stated once over the whole matrix: if a row
        counts as complete for getCompletionCounts, it is not a skip."""
        tracks = {trackId: {"id": trackId, "name": trackId, "artists": [], "duration": durationMs}
                  for trackId, durationMs, _ in self.PARITY_ROWS}
        entries = [{"id": trackId, "playedAt": 1000.0 + index, "timePlayed": timePlayed}
                   for index, (trackId, _, timePlayed) in enumerate(self.PARITY_ROWS)]
        db = self._makeDb(tracks, entries)

        for mode, value, completionPercent in self.PARITY_SETTINGS:
            with self.subTest(mode=mode, value=value, completionPercent=completionPercent):
                db.repo.setIntSetting(COMPLETION_COMPLETE_PERCENT_KEY, completionPercent,
                                      COMPLETION_COMPLETE_PERCENT_MIN, COMPLETION_COMPLETE_PERCENT_MAX)
                with patch.object(db.repo, "getSkipThreshold", return_value=(mode, value)):
                    db.repo.recomputeSkipFlags()
                    storedFlags = {row["track_id"]: row["is_skip"] for row in db.repo._conn().execute(
                        "SELECT track_id, is_skip FROM plays").fetchall()}
                    for trackId, durationMs, timePlayed in self.PARITY_ROWS:
                        if durationMs > 0 and timePlayed >= durationMs * completionPercent / PERCENT_DIVISOR:
                            self.assertEqual(db.repo.computeIsSkip(timePlayed, durationMs), 0,
                                             f"{trackId} is complete but computeIsSkip called it a skip")
                            self.assertEqual(storedFlags[trackId], 0,
                                             f"{trackId} is complete but recomputeSkipFlags stored a skip")
                # The pie's three buckets still account for every play, and a
                # complete play stored as a skip would land in `skips`.
                counts = db.repo.getCompletionCounts("testuser")
                self.assertEqual(counts["skips"] + counts["completes"] + counts["partials"],
                                 len(self.PARITY_ROWS))


class ConfigureWorkerPoolsTestCase(DatabaseTestCase):
    """Worker pool sizes are read from settings once at startup (applies after
    restart). Restores the shared class-level executors after the test."""

    def test_reads_worker_counts_from_settings(self):
        from Database.database import Database, ARTIST_BIO_FETCH_WORKERS
        db = self._makeDb({}, [])

        originals = (Database._imageDownloadExecutor,
                     Database._artistBioFetchExecutor,
                     Database._albumBioFetchExecutor)

        def _restore():
            Database._imageDownloadExecutor = originals[0]
            Database._artistBioFetchExecutor = originals[1]
            Database._albumBioFetchExecutor = originals[2]
        self.addCleanup(_restore)

        db.repo.setIntSetting("image_download_workers", 9, 1, 32)
        Database.configureWorkerPools(db.repo)

        self.assertEqual(Database._imageDownloadExecutor._max_workers, 9)
        # Unset pools fall back to the code default.
        self.assertEqual(Database._artistBioFetchExecutor._max_workers, ARTIST_BIO_FETCH_WORKERS)


if __name__ == "__main__":
    import unittest
    unittest.main()
