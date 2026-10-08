import os
import unittest
from unittest.mock import MagicMock, patch, call
from main import execute_pipeline
from utils import TranscriptionUnavailableError

class TestPartialUploadOrder(unittest.TestCase):
    @patch("main.create_pdf")
    @patch("main.create_docx")
    @patch("main.transcribe_audio", side_effect=TranscriptionUnavailableError("incomplete"))
    @patch("main.AudioSegment.from_file")
    def test_incomplete_transcript_never_generates_or_marks_completed(
            self, audio, transcribe, create_docx, create_pdf):
        audio.return_value.__len__.return_value = 15023
        store = MagicMock()
        with patch("main.os.path.exists", return_value=False), \
             patch("main.send_telegram_message"):
            success, transcript = execute_pipeline(
                "short.mp3", "short.mp3", "source-id", store, source="drive")
        self.assertEqual((success, transcript), (False, ""))
        create_docx.assert_not_called()
        create_pdf.assert_not_called()
        store.mark_processed.assert_not_called()

    @patch("time.sleep")
    @patch("main.generate_drive_file_id")
    @patch("main.verify_drive_file_exists")
    @patch("main.send_long_telegram_message")
    @patch("main.send_telegram_message")
    @patch("main.upload_file_to_drive")
    @patch("main.create_pdf")
    @patch("main.create_docx")
    @patch("main.transcribe_audio")
    @patch("main.AudioSegment.from_file")
    def test_partial_upload_persistence_sequence(
        self, mock_audio_segment, mock_transcribe, mock_create_docx, 
        mock_create_pdf, mock_upload_drive, mock_send_tg, mock_send_long_tg, 
        mock_verify_drive, mock_gen_id, mock_sleep
    ):
        mock_seg = MagicMock()
        mock_seg.__len__.return_value = 600000 # 10 min
        mock_audio_segment.return_value = mock_seg
        mock_transcribe.return_value = ("Texto transcrito de prueba", "gemini-3.5-flash-lite")
        mock_create_docx.return_value = "dummy.docx"
        mock_create_pdf.return_value = "dummy.pdf"
        mock_verify_drive.return_value = (False, None)
        mock_gen_id.side_effect = ["pre_docx_id_100", "pre_pdf_id_200"]

        call_order = []
        
        def mock_upload_impl(path, mime_type, source_key, doc_type, preallocated_id):
            if doc_type == "docx":
                call_order.append("upload_docx")
                return preallocated_id
            else:
                call_order.append("upload_pdf")
                return preallocated_id
                
        mock_upload_drive.side_effect = mock_upload_impl

        mock_store = MagicMock()
        mock_store.get_partial_uploads.return_value = {}
        mock_store.save_partial_upload.side_effect = lambda file_id, doc_type, drive_id: call_order.append(f"save_partial_{doc_type}")
        mock_store.mark_processed.side_effect = lambda file_id, name, model, final_ids=None: call_order.append("mark_processed")

        with patch("main.os.path.exists", return_value=False):
            success, transcript = execute_pipeline("dummy.mp3", "dummy.mp3", "id123", mock_store, source="drive")

        self.assertTrue(success)
        expected = ["save_partial_docx", "upload_docx", "save_partial_pdf", "upload_pdf", "mark_processed"]
        self.assertEqual(call_order, expected)

    @patch("time.sleep")
    @patch("main.generate_drive_file_id")
    @patch("main.verify_drive_file_exists")
    @patch("main.send_long_telegram_message")
    @patch("main.send_telegram_message")
    @patch("main.upload_file_to_drive")
    @patch("main.create_pdf")
    @patch("main.create_docx")
    @patch("main.transcribe_audio")
    @patch("main.AudioSegment.from_file")
    def test_stale_partial_id_removal_and_reupload(
        self, mock_audio_segment, mock_transcribe, mock_create_docx, 
        mock_create_pdf, mock_upload_drive, mock_send_tg, mock_send_long_tg, 
        mock_verify_drive, mock_gen_id, mock_sleep
    ):
        mock_seg = MagicMock()
        mock_seg.__len__.return_value = 600000
        mock_audio_segment.return_value = mock_seg
        mock_transcribe.return_value = ("Texto", "gemini-3.5-flash-lite")
        mock_create_docx.return_value = "dummy.docx"
        mock_create_pdf.return_value = "dummy.pdf"
        mock_verify_drive.return_value = (False, None)
        mock_gen_id.side_effect = ["new_docx_id", "new_pdf_id"]
        mock_upload_drive.side_effect = ["new_docx_id", "new_pdf_id"]
        
        mock_store = MagicMock()
        mock_store.get_partial_uploads.return_value = {"docx": "stale_docx_id", "pdf": "stale_pdf_id"}

        with patch("main.os.path.exists", return_value=False):
            success, transcript = execute_pipeline("dummy.mp3", "dummy.mp3", "id123", mock_store, source="drive")

        self.assertTrue(success)
        self.assertEqual(mock_upload_drive.call_count, 2)

if __name__ == "__main__":
    unittest.main()
