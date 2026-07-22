"""The four settings this release moved into app_settings: the completion
complete-percent, the cookie<->email verification toggle, the Last.fm genre/bio
backfill retry intervals, and the backup interval/retention. Settings-level
behavior + the two wirings worth an integration check (getCompletionStats and
the genre backfill queue)."""
import sys
import os
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest
from Database.repository import (
    COMPLETION_COMPLETE_PERCENT_KEY, COMPLETION_COMPLETE_PERCENT_MIN, COMPLETION_COMPLETE_PERCENT_MAX,
    GENRE_BACKFILL_RETRY_DAYS_KEY, BIO_BACKFILL_RETRY_DAYS_KEY, BACKFILL_RETRY_DAYS_MIN, BACKFILL_RETRY_DAYS_MAX,
    BACKUP_INTERVAL_HOURS_KEY, BACKUP_INTERVAL_HOURS_MIN, BACKUP_INTERVAL_HOURS_MAX,
)

_DAY = 24 * 3600


class CompletionPercentTestCase(DatabaseTestCase):
    def test_default_and_clamp(self):
        db = self._makeDb({}, [])
        self.assertEqual(db.repo.getCompletionCompletePercent(), 80)
        db.repo.setIntSetting(COMPLETION_COMPLETE_PERCENT_KEY, 999,
                              COMPLETION_COMPLETE_PERCENT_MIN, COMPLETION_COMPLETE_PERCENT_MAX)
        self.assertEqual(db.repo.getCompletionCompletePercent(), 100)

    def test_getCompletionStats_uses_the_setting(self):
        tracks = {"t": {"id": "t", "name": "T", "artists": [], "duration": 100000}}
        entries = [{"id": "t", "playedAt": 1000, "timePlayed": 70000}]   #< 70% of the track
        db = self._makeDb(tracks, entries)
        # Default 80%: 70% is a partial.
        self.assertEqual(db.getCompletionStats(), {"skips": 0, "completes": 0, "partials": 1})
        # Lower the bar to 60%: the same play is now a complete.
        db.repo.setIntSetting(COMPLETION_COMPLETE_PERCENT_KEY, 60,
                              COMPLETION_COMPLETE_PERCENT_MIN, COMPLETION_COMPLETE_PERCENT_MAX)
        self.assertEqual(db.getCompletionStats(), {"skips": 0, "completes": 1, "partials": 0})


class EmailVerificationSettingTestCase(DatabaseTestCase):
    def test_defaults_enabled_and_toggles(self):
        db = self._makeDb({}, [])
        self.assertTrue(db.repo.isEmailVerificationEnabled())
        db.repo.setEmailVerificationEnabled(False)
        self.assertFalse(db.repo.isEmailVerificationEnabled())
        db.repo.setEmailVerificationEnabled(True)
        self.assertTrue(db.repo.isEmailVerificationEnabled())


class BackfillRetryDaysTestCase(DatabaseTestCase):
    def test_defaults_and_seconds_conversion(self):
        db = self._makeDb({}, [])
        self.assertEqual(db.repo.getGenreBackfillRetryDays(), 30)
        self.assertEqual(db.repo.getBioBackfillRetryDays(), 30)
        self.assertEqual(db.repo.getGenreBackfillRetrySeconds(), 30 * _DAY)
        self.assertEqual(db.repo.getBioBackfillRetrySeconds(), 30 * _DAY)
        db.repo.setIntSetting(GENRE_BACKFILL_RETRY_DAYS_KEY, 7, BACKFILL_RETRY_DAYS_MIN, BACKFILL_RETRY_DAYS_MAX)
        self.assertEqual(db.repo.getGenreBackfillRetrySeconds(), 7 * _DAY)

    def test_genre_retry_setting_affects_the_queue(self):
        db = self._makeDb({}, [])
        db.repo.upsertUser("u", "u@e.com")
        db.repo.upsertTrack(normalizeTrackForTest({"id": "t", "name": "T", "artists": [{"id": "a", "name": "A"}]}))
        db.repo.insertPlay("u", "t", 1000, 60000)
        conn = db.repo._conn()
        with conn:   #< attempted 10 days ago, still no genres
            conn.execute("UPDATE artists SET lastfm_attempted_at = ? WHERE id='a'", (time.time() - 10 * _DAY,))
        db.repo.commit()

        # Default 30-day retry: a 10-day-old attempt is too recent to re-queue.
        self.assertNotIn("a", {r["id"] for r in db.repo.getArtistsMissingGenres(limit=50)})
        # Shorten to 7 days: the 10-day-old attempt is now past the cutoff.
        db.repo.setIntSetting(GENRE_BACKFILL_RETRY_DAYS_KEY, 7, BACKFILL_RETRY_DAYS_MIN, BACKFILL_RETRY_DAYS_MAX)
        self.assertIn("a", {r["id"] for r in db.repo.getArtistsMissingGenres(limit=50)})


class BackupSettingsTestCase(DatabaseTestCase):
    def test_falls_back_to_default_then_clamps_and_allows_zero(self):
        db = self._makeDb({}, [])
        self.assertEqual(db.repo.getBackupIntervalHours(24), 24)   #< env/code fallback when unset
        self.assertEqual(db.repo.getBackupRetentionCount(7), 7)
        db.repo.setIntSetting(BACKUP_INTERVAL_HOURS_KEY, 9999, BACKUP_INTERVAL_HOURS_MIN, BACKUP_INTERVAL_HOURS_MAX)
        self.assertEqual(db.repo.getBackupIntervalHours(24), BACKUP_INTERVAL_HOURS_MAX)
        db.repo.setIntSetting(BACKUP_INTERVAL_HOURS_KEY, 0, BACKUP_INTERVAL_HOURS_MIN, BACKUP_INTERVAL_HOURS_MAX)
        self.assertEqual(db.repo.getBackupIntervalHours(24), 0)    #< 0 (disable) is a valid stored value


if __name__ == "__main__":
    import unittest
    unittest.main()
