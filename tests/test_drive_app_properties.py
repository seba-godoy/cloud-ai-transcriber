import unittest
from unittest.mock import patch, MagicMock
from googleapiclient.errors import HttpError
from utils import make_source_key, DriveIntegrityError
from main import verify_drive_file_exists

class TestDriveAppPropertiesSearch(unittest.TestCase):
    @patch("main.get_drive_service")
    def test_search_query_exact_appproperties_has_syntax(self, mock_get_service):
        mock_service = MagicMock()
        mock_files = MagicMock()
        
        # 1. get() returns 404 HttpError
        mock_get_err = HttpError(resp=MagicMock(status=404), content=b'Not Found')
        mock_files.get.return_value.execute.side_effect = mock_get_err
        
        # 2. list() returns 1 match
        mock_list_cmd = MagicMock()
        source_key = make_source_key("drive", "id123")
        mock_list_cmd.execute.return_value = {
            "files": [{"id": "repaired_drive_id"}]
        }
        mock_files.list.return_value = mock_list_cmd
        mock_service.files.return_value = mock_files
        mock_get_service.return_value = mock_service

        mock_store = MagicMock()

        exists, drive_id = verify_drive_file_exists("stale_id", "id123", "drive", "docx", mock_store)

        self.assertTrue(exists)
        self.assertEqual(drive_id, "repaired_drive_id")
        
        # Verify exact search query
        mock_files.list.assert_called_once()
        kwargs = mock_files.list.call_args[1]
        query = kwargs.get("q", "")
        self.assertIn("appProperties has { key='source_key' and value='", query)
        self.assertIn("appProperties has { key='document_type' and value='docx' }", query)
        self.assertNotIn("appProperties.source_key", query)

    @patch("main.get_drive_service")
    def test_multiple_search_matches_raises_drive_integrity_error(self, mock_get_service):
        mock_service = MagicMock()
        mock_files = MagicMock()
        mock_get_err = HttpError(resp=MagicMock(status=404), content=b'Not Found')
        mock_files.get.return_value.execute.side_effect = mock_get_err
        
        mock_list_cmd = MagicMock()
        mock_list_cmd.execute.return_value = {
            "files": [{"id": "id1"}, {"id": "id2"}]
        }
        mock_files.list.return_value = mock_list_cmd
        mock_service.files.return_value = mock_files
        mock_get_service.return_value = mock_service

        mock_store = MagicMock()
        with self.assertRaises(DriveIntegrityError):
            verify_drive_file_exists("stale_id", "id123", "drive", "docx", mock_store)

    @patch("main.get_drive_service")
    def test_get_503_raises_exception_without_fallback_search(self, mock_get_service):
        mock_service = MagicMock()
        mock_files = MagicMock()
        mock_files.get.return_value.execute.side_effect = HttpError(resp=MagicMock(status=503), content=b'Service Unavailable')
        mock_service.files.return_value = mock_files
        mock_get_service.return_value = mock_service

        mock_store = MagicMock()
        with self.assertRaises(HttpError) as ctx:
            verify_drive_file_exists("stale_id", "id123", "drive", "docx", mock_store)
        self.assertEqual(ctx.exception.resp.status, 503)
        mock_files.list.assert_not_called()

    @patch("main.get_drive_service")
    def test_get_429_raises_exception_without_fallback_search(self, mock_get_service):
        mock_service = MagicMock()
        mock_files = MagicMock()
        mock_files.get.return_value.execute.side_effect = HttpError(resp=MagicMock(status=429), content=b'Too Many Requests')
        mock_service.files.return_value = mock_files
        mock_get_service.return_value = mock_service

        mock_store = MagicMock()
        with self.assertRaises(HttpError) as ctx:
            verify_drive_file_exists("stale_id", "id123", "drive", "docx", mock_store)
        self.assertEqual(ctx.exception.resp.status, 429)
        mock_files.list.assert_not_called()

    @patch("main.get_drive_service")
    def test_get_403_raises_exception_without_fallback_search(self, mock_get_service):
        mock_service = MagicMock()
        mock_files = MagicMock()
        mock_files.get.return_value.execute.side_effect = HttpError(resp=MagicMock(status=403), content=b'Forbidden')
        mock_service.files.return_value = mock_files
        mock_get_service.return_value = mock_service

        mock_store = MagicMock()
        with self.assertRaises(HttpError) as ctx:
            verify_drive_file_exists("stale_id", "id123", "drive", "docx", mock_store)
        self.assertEqual(ctx.exception.resp.status, 403)
        mock_files.list.assert_not_called()

    @patch("main.get_drive_service")
    def test_get_timeout_error_raises_exception_without_fallback_search(self, mock_get_service):
        mock_service = MagicMock()
        mock_files = MagicMock()
        mock_files.get.return_value.execute.side_effect = TimeoutError("Network timeout")
        mock_service.files.return_value = mock_files
        mock_get_service.return_value = mock_service

        mock_store = MagicMock()
        with self.assertRaises(TimeoutError):
            verify_drive_file_exists("stale_id", "id123", "drive", "docx", mock_store)
        mock_files.list.assert_not_called()

    @patch("main.get_drive_service")
    def test_get_generic_exception_raises_without_fallback_search(self, mock_get_service):
        mock_service = MagicMock()
        mock_files = MagicMock()
        mock_files.get.return_value.execute.side_effect = RuntimeError("Unexpected internal error")
        mock_service.files.return_value = mock_files
        mock_get_service.return_value = mock_service

        mock_store = MagicMock()
        with self.assertRaises(RuntimeError):
            verify_drive_file_exists("stale_id", "id123", "drive", "docx", mock_store)
        mock_files.list.assert_not_called()

if __name__ == "__main__":
    unittest.main()
