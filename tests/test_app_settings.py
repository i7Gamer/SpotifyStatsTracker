"""Instance-wide admin feature toggles: Spotify API backfill, Last.fm genre
backfill, data sharing and new user registration. Each mirrors the existing
genres_include_inherited toggle - default enabled, stored in app_settings,
generic getAppSetting/setAppSetting underneath."""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
from Database.repository import (
    SPOTIFY_BACKFILL_SETTING_KEY, LASTFM_BACKFILL_SETTING_KEY,
    DATA_SHARING_SETTING_KEY, REGISTRATION_SETTING_KEY,
)


class FeatureToggleTestCase(DatabaseTestCase):
    """Table-driven over the four (getter, setter, key) triples - identical
    contract for all four, so one parametrized case covers them rather than
    four near-duplicate test classes."""

    def _cases(self):
        db = self._makeDb({}, [])
        return db, (
            (db.repo.isSpotifyApiBackfillEnabled, db.repo.setSpotifyApiBackfillEnabled,
             SPOTIFY_BACKFILL_SETTING_KEY),
            (db.repo.isLastfmGenreBackfillEnabled, db.repo.setLastfmGenreBackfillEnabled,
             LASTFM_BACKFILL_SETTING_KEY),
            (db.repo.isDataSharingEnabled, db.repo.setDataSharingEnabled,
             DATA_SHARING_SETTING_KEY),
            (db.repo.isRegistrationEnabled, db.repo.setRegistrationEnabled,
             REGISTRATION_SETTING_KEY),
        )

    def test_defaults_to_enabled_when_never_set(self):
        db, cases = self._cases()
        for isEnabled, _setEnabled, key in cases:
            self.assertTrue(isEnabled(), key)
            self.assertIsNone(db.repo.getAppSetting(key))

    def test_round_trips_through_disable_and_re_enable(self):
        db, cases = self._cases()
        for isEnabled, setEnabled, key in cases:
            setEnabled(False)
            self.assertFalse(isEnabled(), key)
            self.assertIsNotNone(db.repo.getAppSetting(key))
            setEnabled(True)
            self.assertTrue(isEnabled(), key)

    def test_setting_is_idempotent_on_repeated_writes(self):
        db, cases = self._cases()
        for isEnabled, setEnabled, key in cases:
            setEnabled(False)
            setEnabled(False)
            self.assertFalse(isEnabled(), key)

    def test_toggles_are_independent_of_each_other(self):
        db, cases = self._cases()
        _, setSpotify, _ = cases[0]
        isLastfm, _, _ = cases[1]
        isSharing, _, _ = cases[2]
        isRegistration, _, _ = cases[3]

        setSpotify(False)

        self.assertTrue(isLastfm())
        self.assertTrue(isSharing())
        self.assertTrue(isRegistration())


if __name__ == "__main__":
    import unittest
    unittest.main()
