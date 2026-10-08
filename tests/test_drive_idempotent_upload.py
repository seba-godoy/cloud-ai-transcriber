import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from googleapiclient.errors import HttpError
from drive_uploader import upload_file_to_drive, generate_drive_file_id
from utils import make_source_key, DriveIntegrityError, DriveUploadUnconfirmedError, is_transient_error

class TestDriveIdempotentUpload(unittest.TestCase):
    @patch("drive_uploader.get_drive_service")
    def test_generate_drive_file_id_valid_response(self, mock_get_service):
        mock_service = MagicMock()
        mock_files = MagicMock()
        mock_files.generateIds.return_value.execute.return_value = {
            "ids": ["gen_id_100"],
            "space": "drive",
            "kind": "drive#generatedIds"
        }
        mock_service.files.return_value = mock_files
        mock_get_service.return_value = mock_service

        res = generate_drive_file_id()
        self.assertEqual(res, "gen_id_100")

    @patch("drive_uploader.get_drive_service")
    def test_generate_drive_file_id_empty_ids_raises_value_error(self, mock_get_service):
        mock_service = MagicMock()
        mock_files = MagicMock()
        mock_files.generateIds.return_value.execute.return_value = {"ids": []}
        mock_service.files.return_value = mock_files
        mock_get_service.return_value = mock_service

        with self.assertRaises(ValueError) as ctx:
            generate_drive_file_id()
        self.assertIn("failed to return a single valid ID", str(ctx.exception))

    def test_missing_preallocated_id_raises_value_error(self):
        source_key = make_source_key("drive", "id123")
        with self.assertRaises(ValueError) as ctx:
            upload_file_to_drive("dummy.docx", "application/docx", source_key, "docx", preallocated_id="")
        self.assertIn("preallocated_id is required", str(ctx.exception))

    @patch("time.sleep")
    @patch("drive_uploader.MediaFileUpload")
    @patch("drive_uploader.get_drive_service")
    def test_create_empty_response_get_404_retry_same_id_success(self, mock_get_service, mock_media, mock_sleep):
        mock_service = MagicMock()
        mock_files = MagicMock()
        
        pre_id = "pre_allocated_999"
        source_key = make_source_key("drive", "id999")
        err404 = HttpError(resp=MagicMock(status=404), content=b'Not Found')

        # 1st attempt: get before create -> 404
        # create returns {}
        # get after create -> 404
        # 2nd attempt: get before create -> 404
        # create returns {"id": pre_id}
        mock_files.get.return_value.execute.side_effect = [
            err404,
            err404,
            err404,
            err404
        ]
        mock_files.create.return_value.execute.side_effect = [
            {}, # 1st create response missing ID
            {"id": pre_id} # 2nd create response successful
        ]
        
        mock_service.files.return_value = mock_files
        mock_get_service.return_value = mock_service

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_docx = os.path.join(tmpdir, "test.docx")
            with open(tmp_docx, "w", encoding="utf-8") as f:
                f.write("content")
                
            returned_id = upload_file_to_drive(tmp_docx, "application/docx", source_key, "docx", preallocated_id=pre_id)

        self.assertEqual(returned_id, pre_id)
        # Assert files().create was called twice with the EXACT same preallocated ID in body
        self.assertEqual(mock_files.create.call_count, 2)
        for call in mock_files.create.call_args_list:
            body = call[1].get("body", {})
            self.assertEqual(body.get("id"), pre_id)

    @patch("time.sleep")
    @patch("drive_uploader.MediaFileUpload")
    @patch("drive_uploader.get_drive_service")
    def test_unconfirmed_creation_exhaustion_raises_transient_drive_upload_unconfirmed_error(self, mock_get_service, mock_media, mock_sleep):
        mock_service = MagicMock()
        mock_files = MagicMock()
        
        pre_id = "pre_allocated_888"
        source_key = make_source_key("drive", "id888")
        err404 = HttpError(resp=MagicMock(status=404), content=b'Not Found')

        mock_files.get.return_value.execute.side_effect = err404
        mock_files.create.return_value.execute.return_value = {} # Always missing ID
        
        mock_service.files.return_value = mock_files
        mock_get_service.return_value = mock_service

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_docx = os.path.join(tmpdir, "test.docx")
            with open(tmp_docx, "w", encoding="utf-8") as f:
                f.write("content")
                
            with self.assertRaises(DriveUploadUnconfirmedError) as ctx:
                upload_file_to_drive(tmp_docx, "application/docx", source_key, "docx", preallocated_id=pre_id)

            self.assertTrue(is_transient_error(ctx.exception))

    @patch("drive_uploader.get_drive_service")
    def test_existing_id_with_incompatible_app_properties_raises_drive_integrity_error_without_create(self, mock_get_service):
        mock_service = MagicMock()
        mock_files = MagicMock()
        
        pre_id = "pre_allocated_777"
        source_key = make_source_key("drive", "id777")

        # Existing file has wrong source_key
        mock_files.get.return_value.execute.return_value = {
            "id": pre_id,
            "trashed": False,
            "appProperties": {"source_key": "WRONG_KEY", "document_type": "docx"}
        }
        mock_service.files.return_value = mock_files
        mock_get_service.return_value = mock_service

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_docx = os.path.join(tmpdir, "test.docx")
            with open(tmp_docx, "w", encoding="utf-8") as f:
                f.write("content")
                
            with self.assertRaises(DriveIntegrityError):
                upload_file_to_drive(tmp_docx, "application/docx", source_key, "docx", preallocated_id=pre_id)

        # Assert create was NOT called
        mock_files.create.assert_not_called()

if __name__ == "__main__":
    unittest.main()
