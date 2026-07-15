import unittest
from unittest.mock import patch, MagicMock, mock_open
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.Importers.AutoImporter import AutoImporter, Watchdog

class TestAutoImporterLogging(unittest.TestCase):
    def setUp(self):
        # Set up a caplog-like context or use logging assertLogs
        self.logger = logging.getLogger("Database.Importers.AutoImporter")
        self.original_level = self.logger.level
        self.logger.setLevel(logging.INFO)

    def tearDown(self):
        self.logger.setLevel(self.original_level)

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.os.listdir")
    def test_watchdog_monitoring_log(self, mock_listdir, mock_makedirs, mock_exists):
        mock_exists.return_value = True
        mock_listdir.return_value = []
        
        wd = Watchdog()
        wd.run = False  # Stop the loop immediately in the test
        
        with self.assertLogs("Database.Importers.AutoImporter", level="INFO") as log_capture:
            wd.watchFolder_blocking("/dummy/path", lambda x: None, callbackInitialFiles=False)
            
        self.assertTrue(any("Monitoring /dummy/path for new files (Polling)..." in record for record in log_capture.output))

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.os.listdir")
    def test_watchdog_file_found_log(self, mock_listdir, mock_makedirs, mock_exists):
        mock_exists.return_value = True
        mock_listdir.side_effect = [["file1.txt"]]
        
        wd = Watchdog()
        wd.run = False
        
        with patch("Database.Importers.AutoImporter.os.path.isfile", return_value=True):
            with self.assertLogs("Database.Importers.AutoImporter", level="INFO") as log_capture:
                wd.watchFolder_blocking("/dummy/path", lambda x: None, callbackInitialFiles=True)
                
        self.assertTrue(any("File found:" in record for record in log_capture.output))

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_auto_importer_import_success_log(self, mock_move, mock_exists):
        import os
        mock_exists.side_effect = lambda p: os.path.normpath(p) == os.path.normpath("/dummy/path/DONE")
        
        import_callback = MagicMock()
        importer = AutoImporter("/dummy/path", import_callback)
        
        m_open = mock_open(read_data="dummy data")
        with patch("Database.Importers.AutoImporter.open", m_open):
            with self.assertLogs("Database.Importers.AutoImporter", level="INFO") as log_capture:
                importer._handleImport("/dummy/path/file.txt")
                
        self.assertTrue(any("Successfully imported file.txt" in record for record in log_capture.output))
        self.assertTrue(any("Successfully moved file.txt to DONE/" in record for record in log_capture.output))

if __name__ == "__main__":
    unittest.main()
