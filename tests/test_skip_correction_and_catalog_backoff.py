"""Skip-only corrections preserve import transactions; nested 429 stops batches."""
import unittest
from unittest.mock import MagicMock, patch

from conftest import DatabaseTestCase
from Database.database import _ImportRunState
from Database.workers.metadata_backfiller import MetadataBackfillMixin
from test_repository import makeTrack

PLAYED_AT = 1000
PLAYED_MS = 10000
PLAY_YEAR = 1970
CATALOG_NOW = 1000
RETRY_AFTER_SECONDS = 60


class TestSkipOnlyCorrection(DatabaseTestCase):
    def _seed(self):
        db = self._makeDb({}, [])
        db.repo.setSkipThreshold("percent", 20)
        track = makeTrack()
        track["duration"] = 0
        db.repo.upsertTrack(track)
        db.repo.insertPlay(db.user, "t1", PLAYED_AT, PLAYED_MS, created_reason="listener_play", is_skip=0)
        db.repo.commit()
        return db

    def test_exact_duplicate_updates_classification_without_committing(self):
        db = self._seed()
        before = dict(db.repo._conn().execute("SELECT * FROM plays").fetchone())
        assert not db.repo.insertPlay(db.user, "t1", PLAYED_AT, PLAYED_MS, is_skip=1)
        after = dict(db.repo._conn().execute("SELECT * FROM plays").fetchone())
        assert after == {**before, "is_skip": 1}
        assert db.repo._conn().in_transaction
        db.repo.rollback()
        assert dict(db.repo._conn().execute("SELECT * FROM plays").fetchone()) == before

    def test_skip_only_import_correction_propagates_both_transaction_modes(self):
        for deferred in (False, True):
            with self.subTest(deferred=deferred):
                db = self._seed()
                before = dict(db.repo._conn().execute("SELECT * FROM plays").fetchone())
                track = makeTrack()
                run_state = _ImportRunState()
                progress = MagicMock()
                with patch.object(db, "_invalidateWrappedFromEarliestOf") as invalidate, \
                     patch.object(db, "saveImagesFromTrack"):
                    db._applyImportData(
                        {"t1": track}, [{"id": "t1", "playedAt": PLAYED_AT, "timePlayed": PLAYED_MS}],
                        {}, 1, "review export", "", True, False, {}, run_state, deferred, progress)
                after = dict(db.repo._conn().execute("SELECT * FROM plays").fetchone())
                assert after == {**before, "is_skip": 1}
                assert "1 corrected" in repr(progress.call_args_list)
                if deferred:
                    assert db.repo._conn().in_transaction
                    assert run_state.correctedYears == {PLAY_YEAR}
                    invalidate.assert_not_called()
                    db.repo.rollback()
                    assert dict(db.repo._conn().execute("SELECT * FROM plays").fetchone()) == before
                else:
                    assert not db.repo._conn().in_transaction
                    invalidate.assert_called_once_with({PLAY_YEAR}, "Import")


class TestNestedCatalogCooldown(unittest.TestCase):
    def test_nested_rate_limit_preserves_partial_album_and_stops_later_ids(self):
        worker = MetadataBackfillMixin()
        worker.user = "fixture"
        stop = MagicMock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        album = {"tracks": {"items": [{"id": "page1"}], "next": "https://api.spotify.com/v1/albums/A/tracks?offset=50"}}
        ok = MagicMock(status_code=200)
        ok.json.return_value = album
        limited = MagicMock(status_code=429, headers={"Retry-After": str(RETRY_AFTER_SECONDS)})
        complete = []

        def accept(entity_id, response):
            complete.append(worker._walkRemainingAlbumTracks(response.json(), {}, stop))

        with patch("requests.get", side_effect=[ok, limited, ok, ok]) as get, \
             patch("Database.database.time.time", return_value=CATALOG_NOW):
            batch = worker._spendCatalogBatch("albums", ["A", "B", "C"], {}, stop, accept)
        assert get.call_count == 2
        assert batch.attempted == ["A"]
        assert batch.failures == 0
        assert complete == [False]
        assert album["tracks"]["items"] == [{"id": "page1"}]
        with patch("requests.get", return_value=ok) as get, \
             patch("Database.database.time.time", return_value=CATALOG_NOW + RETRY_AFTER_SECONDS):
            resumed = worker._spendCatalogBatch("albums", ["B"], {}, stop, MagicMock())
        assert resumed.attempted == ["B"]
        get.assert_called_once()
