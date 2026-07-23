"""The genre-detail resolvers thread the selected date range through to their
db methods (the /genres page is now time-filterable), and keep the existing
degrade-to-empty contract on failure/stubbed dbs."""
import unittest
from unittest.mock import MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.genre_gate import (
    resolveTopArtistsForGenre, resolveTopTracksForGenre, resolveGenreHeatmap,
    emptyHeatmapGrid,
)


class TopListResolverDateScopingTestCase(unittest.TestCase):
    def _cases(self):
        return (
            (resolveTopArtistsForGenre, "getTopArtistsForGenre"),
            (resolveTopTracksForGenre, "getTopTracksForGenre"),
        )

    def test_passes_dates_through_and_returns_list(self):
        for resolver, dbMethod in self._cases():
            with self.subTest(resolver=resolver.__name__):
                db = MagicMock()
                getattr(db, dbMethod).return_value = [{"id": "a1"}]
                out = resolver(db, "rock", 5, "START", "END")
                self.assertEqual(out, [{"id": "a1"}])
                getattr(db, dbMethod).assert_called_once_with("rock", 5, startDate="START", endDate="END")

    def test_defaults_to_all_time_when_dates_omitted(self):
        for resolver, dbMethod in self._cases():
            with self.subTest(resolver=resolver.__name__):
                db = MagicMock()
                getattr(db, dbMethod).return_value = []
                resolver(db, "rock", 5)
                getattr(db, dbMethod).assert_called_once_with("rock", 5, startDate=None, endDate=None)

    def test_error_degrades_to_empty_list(self):
        for resolver, dbMethod in self._cases():
            with self.subTest(resolver=resolver.__name__):
                db = MagicMock()
                getattr(db, dbMethod).side_effect = RuntimeError("boom")
                self.assertEqual(resolver(db, "rock", 5, "START", "END"), [])

    def test_non_list_return_degrades_to_empty_list(self):
        for resolver, _ in self._cases():
            with self.subTest(resolver=resolver.__name__):
                db = MagicMock()   #< unstubbed method returns a MagicMock, not a list
                self.assertEqual(resolver(db, "rock", 5, "START", "END"), [])


class GenreHeatmapResolverDateScopingTestCase(unittest.TestCase):
    def test_passes_dates_through_and_returns_grid(self):
        db = MagicMock()
        grid = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenreHourOfDayHeatmap.return_value = grid
        out = resolveGenreHeatmap(db, "rock", "START", "END")
        self.assertIs(out, grid)
        db.getGenreHourOfDayHeatmap.assert_called_once_with("rock", startDate="START", endDate="END")

    def test_defaults_to_all_time_when_dates_omitted(self):
        db = MagicMock()
        db.getGenreHourOfDayHeatmap.return_value = []
        resolveGenreHeatmap(db, "rock")
        db.getGenreHourOfDayHeatmap.assert_called_once_with("rock", startDate=None, endDate=None)

    def test_error_degrades_to_empty_grid(self):
        db = MagicMock()
        db.getGenreHourOfDayHeatmap.side_effect = RuntimeError("boom")
        self.assertEqual(resolveGenreHeatmap(db, "rock", "START", "END"), emptyHeatmapGrid())

    def test_non_list_return_degrades_to_empty_grid(self):
        db = MagicMock()   #< unstubbed method returns a MagicMock, not a list
        self.assertEqual(resolveGenreHeatmap(db, "rock"), emptyHeatmapGrid())


if __name__ == "__main__":
    unittest.main()
