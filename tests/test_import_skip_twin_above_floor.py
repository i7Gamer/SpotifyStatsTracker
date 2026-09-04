"""An import entry ABOVE the importer's fixed 5s floor that the classifier
still calls a skip (a raised admin threshold) takes the real-play path, so it
can correct an existing longer play - the documented asymmetry. But the
real-play matcher only sees is_skip=0 rows, so when the listener had already
recorded the same event - classified is_skip=1 under the same threshold - the
twin was invisible and a second skip row landed a few seconds away. Every
5-30s abandon the listener caught was then counted twice once the yearly
export was dropped into the watch folder.

The fix keeps the asymmetry (a correction of a longer real play still wins)
and, only when there is nothing to correct, claims the nearest listener skip
the way _applySkipEntry does for sub-floor events."""
from unittest.mock import patch, MagicMock

from conftest import DatabaseTestCase, normalizeTrackForTest
from Database.import_service import SKIP_NEAR_TIME_TOLERANCE_SECONDS
from Database.repository import SKIP_MODE_SECONDS

RAISED_SECONDS_THRESHOLD = 30
TRACK_DURATION_MS = 200_000
#< between the importer's 5s floor and the raised threshold: entry["isSkip"]
#  is False, computeIsSkip says 1
ABANDONED_MS = 10_000
LONGER_REAL_PLAY_MS = 60_000
#< the listener's played_at and the export's `ts - ms_played` differ by
#  seconds for one event (Spotify's start-vs-end ambiguity)
SAME_EVENT_OFFSET_SECONDS = 3
#< safely outside the skip tolerance, and inside the real-play matcher's
#  window (duration + 60s) so a longer play there would still be corrected
SEPARATE_EVENT_OFFSET_SECONDS = SKIP_NEAR_TIME_TOLERANCE_SECONDS * 6
EVENT_TS = 1_000_000
TRACK_ID = "track_x"


def _meta(playedAt, timePlayed):
    """An importer-yielded item for TRACK_ID above the 5s floor - the importer
    tags only sub-floor events, so `isSkip` is False here."""
    track = normalizeTrackForTest({"id": TRACK_ID, "name": "Song", "artists": [],
                                   "duration": TRACK_DURATION_MS})
    track["playedAt"] = playedAt
    track["timePlayed"] = timePlayed
    track["playedFrom"] = None
    track["isSkip"] = False
    return track


class ImportAboveFloorSkipTwinTestCase(DatabaseTestCase):
    def _dbWithListenerRow(self, timePlayed, threshold=None):
        """A db holding one listener-style row of TRACK_ID at EVENT_TS,
        classified under `threshold` (the default when None) exactly as the
        live listener classifies: is_skip=computeIsSkip(...)."""
        db = self._makeDb({}, [])
        if threshold is not None:
            db.repo.setSkipThreshold(*threshold)
        db.repo.upsertTrack(normalizeTrackForTest(
            {"id": TRACK_ID, "name": "Song", "artists": [], "duration": TRACK_DURATION_MS}))
        isSkip = db.repo.computeIsSkip(timePlayed, TRACK_DURATION_MS)
        db.repo.insertPlay(db.user, TRACK_ID, EVENT_TS, timePlayed,
                           created_reason="listener", is_skip=isSkip)
        db.repo.commit()
        return db

    def _import(self, db, metas):
        importer = MagicMock()
        importer._convertToList.return_value = ([{}] * len(metas), "spotifyAcountExport")
        importer.expectedEntryCount.side_effect = lambda parsed, exportType: len(parsed)
        importer.importHistory.return_value = iter(metas)
        with patch("Database.database.Importer", return_value=importer):
            db.importHistory("raw export")

    def _rows(self, db):
        return [(r["played_at"], r["time_played"], r["is_skip"]) for r in db.repo._conn().execute(
            "SELECT played_at, time_played, is_skip FROM plays WHERE username=? ORDER BY played_at",
            (db.user,)).fetchall()]

    def test_the_listeners_skip_twin_is_claimed_not_duplicated(self):
        db = self._dbWithListenerRow(ABANDONED_MS, (SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD))
        #< the premise: under this threshold both sources call the event a skip
        self.assertEqual(self._rows(db), [(EVENT_TS, ABANDONED_MS, 1)])

        self._import(db, [_meta(EVENT_TS + SAME_EVENT_OFFSET_SECONDS, ABANDONED_MS)])

        self.assertEqual(self._rows(db), [(EVENT_TS, ABANDONED_MS, 1)])

    def test_control_at_the_default_threshold_the_same_event_is_corrected(self):
        """Already worked: at 5s both sides call it a real play, so the
        real-play matcher sees the listener's row and corrects it in place."""
        db = self._dbWithListenerRow(ABANDONED_MS)
        self.assertEqual(self._rows(db), [(EVENT_TS, ABANDONED_MS, 0)])

        self._import(db, [_meta(EVENT_TS + SAME_EVENT_OFFSET_SECONDS, ABANDONED_MS)])

        self.assertEqual(self._rows(db), [(EVENT_TS + SAME_EVENT_OFFSET_SECONDS, ABANDONED_MS, 0)])

    def test_control_a_longer_real_play_is_still_corrected_not_treated_as_a_twin(self):
        """The documented asymmetry, pinned: the export's more accurate row
        wins over the listener's longer real play - one row, corrected to the
        export's time and reclassified - rather than a skip landing beside
        it."""
        db = self._dbWithListenerRow(LONGER_REAL_PLAY_MS, (SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD))
        self.assertEqual(self._rows(db), [(EVENT_TS, LONGER_REAL_PLAY_MS, 0)])

        self._import(db, [_meta(EVENT_TS + SAME_EVENT_OFFSET_SECONDS, ABANDONED_MS)])

        self.assertEqual(self._rows(db), [(EVENT_TS + SAME_EVENT_OFFSET_SECONDS, ABANDONED_MS, 1)])

    def test_a_skip_outside_the_tolerance_is_a_separate_event(self):
        """The claim uses the skip path's tight window, not the real-play
        matcher's duration-wide one, so a genuine second abandon of the same
        track later in the session is still recorded."""
        db = self._dbWithListenerRow(ABANDONED_MS, (SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD))

        self._import(db, [_meta(EVENT_TS + SEPARATE_EVENT_OFFSET_SECONDS, ABANDONED_MS)])

        self.assertEqual(self._rows(db), [(EVENT_TS, ABANDONED_MS, 1),
                                          (EVENT_TS + SEPARATE_EVENT_OFFSET_SECONDS, ABANDONED_MS, 1)])

    def test_two_abandons_in_one_export_do_not_collapse(self):
        """Own-run writes are never claim candidates (the rule every other
        matcher here follows): two distinct abandons inside the tolerance
        stay two rows."""
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold(SKIP_MODE_SECONDS, RAISED_SECONDS_THRESHOLD)

        self._import(db, [_meta(EVENT_TS, ABANDONED_MS),
                          _meta(EVENT_TS + SAME_EVENT_OFFSET_SECONDS, ABANDONED_MS)])

        self.assertEqual(self._rows(db), [(EVENT_TS, ABANDONED_MS, 1),
                                          (EVENT_TS + SAME_EVENT_OFFSET_SECONDS, ABANDONED_MS, 1)])


if __name__ == "__main__":
    import unittest
    unittest.main()
