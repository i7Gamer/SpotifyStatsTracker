import unittest
from unittest.mock import patch, MagicMock
import sys
import os
from pathlib import Path
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# See tests/test_database_images.py for why this guard is needed.
if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from conftest import DatabaseTestCase
from Database.database import Database
from Database.repository import IMAGE_KIND_TRACK, IMAGE_STATUS_FAILED


class TestDatabaseImageDiscard(DatabaseTestCase):
    def test_download_image_task_marks_failed_and_allows_reclaim(self):
        """A failed download must not permanently block the image - it's marked
        failed in the shared catalog (not left 'pending' forever), so a later
        saveImg for the same id can reclaim and retry it."""
        db = self._makeDb({}, [])

        with tempfile.TemporaryDirectory() as tmpdir:
            imgDir = Path(tmpdir)

            with patch.object(Database, "_mediaGet", side_effect=Exception("network error")), \
                 patch("builtins.print"):
                db._downloadImageTask(imgDir, "https://example.com/bad", "failed-id", IMAGE_KIND_TRACK)

            self.assertEqual(db.repo.imageStatus("failed-id", IMAGE_KIND_TRACK), IMAGE_STATUS_FAILED)
            self.assertTrue(db.repo.tryClaimImageDownload("failed-id", IMAGE_KIND_TRACK))


if __name__ == "__main__":
    unittest.main()
