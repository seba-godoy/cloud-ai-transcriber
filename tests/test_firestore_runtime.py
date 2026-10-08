import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from main import execute_pipeline, handle_processing_failure, process_file, process_telegram_audio, process_youtube_url
from state_backend import LeaseLostError


class FirestoreRuntimeLeaseTests(unittest.TestCase):
    def backend(self):
        backend = MagicMock()
        backend.backend_name = "firestore"
        backend.owner_id = "worker-a"
        backend.lease_ttl_seconds = 900
        backend.bind_source.side_effect = lambda source, source_id, name="": f"{source}:{source_id}"
        return backend

    @patch("main.download_file")
    @patch("main.send_telegram_message")
    def test_contended_drive_job_is_not_downloaded(self, notify, download):
        backend = self.backend(); backend.acquire_lease.return_value = False
        process_file({"id": "d1", "name": "audio.mp3"}, backend)
        download.assert_not_called()

    @patch("main.execute_pipeline")
    @patch("main.download_telegram_file")
    def test_completed_telegram_uses_source_before_duplicate_check(self, download, pipeline):
        backend = self.backend(); backend.is_processed.return_value = True
        process_telegram_audio({"audio": {"file_id": "t1", "file_name": "audio.mp3"}}, 1, backend)
        backend.bind_source.assert_called_once_with("telegram", "t1", "audio.mp3")
        download.assert_not_called(); pipeline.assert_not_called()

    @patch("main.execute_pipeline")
    @patch("main.download_youtube_audio")
    @patch("main.get_youtube_metadata", return_value=("title", "v1"))
    @patch("main.send_telegram_message")
    def test_completed_youtube_uses_source_before_duplicate_check(self, notify, metadata, download, pipeline):
        backend = self.backend(); backend.get_youtube_state.return_value = {"summary_status": "completed"}
        backend.is_processed.return_value = True
        process_youtube_url("https://youtu.be/v1", backend)
        backend.bind_source.assert_called_once_with("youtube", "v1", "title")
        download.assert_not_called(); pipeline.assert_not_called()

    @patch("main.get_youtube_metadata", side_effect=ConnectionError("metadata unavailable"))
    @patch("main.send_telegram_message")
    def test_youtube_prelease_failure_does_not_create_durable_failure(self, notify, metadata):
        backend = self.backend()
        process_youtube_url("https://youtu.be/v1", backend)
        backend.mark_retryable.assert_not_called()
        backend.mark_permanent_failed.assert_not_called()
        backend.bind_source.assert_not_called()

    @patch("main.execute_pipeline", return_value=(True, "text"))
    @patch("main.download_file", return_value="audio.mp3")
    @patch("main.send_telegram_message")
    def test_drive_lease_wraps_download_and_pipeline(self, notify, download, pipeline):
        backend = self.backend(); backend.acquire_lease.return_value = True
        process_file({"id": "d1", "name": "audio.mp3"}, backend)
        backend.acquire_lease.assert_called_once_with("drive:d1", "worker-a", 900)
        pipeline.assert_called_once(); backend.release_lease.assert_called_once_with("drive:d1", "worker-a")

    def test_renew_failure_before_docx_upload_prevents_all_uploads(self):
        backend = self.backend(); backend.renew_lease.side_effect = [True, True, True, False]
        with ExitStack() as stack:
            audio = stack.enter_context(patch("main.AudioSegment.from_file")); audio.return_value.__len__.return_value = 600000
            stack.enter_context(patch("main.transcribe_audio", return_value=("text", "model")))
            stack.enter_context(patch("main.create_docx", return_value="out.docx"))
            stack.enter_context(patch("main.create_pdf", return_value="out.pdf"))
            upload = stack.enter_context(patch("main.upload_file_to_drive"))
            stack.enter_context(patch("main.verify_drive_file_exists", return_value=(False, None)))
            stack.enter_context(patch("main.send_telegram_message"))
            stack.enter_context(patch("main.os.path.exists", return_value=False))
            backend.get_partial_uploads.return_value = {}
            with self.assertRaises(LeaseLostError):
                execute_pipeline("audio.mp3", "audio.mp3", "d1", backend, source="drive")
            upload.assert_not_called()

    def test_renew_failure_after_docx_upload_prevents_pdf(self):
        backend = self.backend(); backend.renew_lease.side_effect = [True, True, True, True, False]
        with ExitStack() as stack:
            audio = stack.enter_context(patch("main.AudioSegment.from_file")); audio.return_value.__len__.return_value = 600000
            stack.enter_context(patch("main.transcribe_audio", return_value=("text", "model")))
            stack.enter_context(patch("main.create_docx", return_value="out.docx"))
            stack.enter_context(patch("main.create_pdf", return_value="out.pdf"))
            upload = stack.enter_context(patch("main.upload_file_to_drive"))
            stack.enter_context(patch("main.verify_drive_file_exists", return_value=(False, None)))
            stack.enter_context(patch("main.generate_drive_file_id", return_value="docx-id"))
            stack.enter_context(patch("main.send_telegram_message"))
            stack.enter_context(patch("main.os.path.exists", return_value=False))
            backend.get_partial_uploads.return_value = {}
            with self.assertRaises(LeaseLostError):
                execute_pipeline("audio.mp3", "audio.mp3", "d1", backend, source="drive")
            self.assertEqual(upload.call_count, 1)

    def test_failure_handler_propagates_lost_lease_without_retry_mutation(self):
        backend = self.backend()
        backend.mark_retryable.side_effect = LeaseLostError("taken over")
        with patch("main.send_telegram_message"), self.assertRaises(LeaseLostError):
            handle_processing_failure("d1", "audio.mp3", "drive", ConnectionError("upload failed"), backend)
        backend.mark_retryable.assert_called_once()
        backend.mark_permanent_failed.assert_not_called()


if __name__ == "__main__": unittest.main()
