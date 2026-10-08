import os
import json
import socket
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import requests
from config import Config
from state_store import StateStore
from utils import (
    is_transient_error, 
    is_network_or_dns_error, 
    StatePersistenceError, 
    TelegramDeliveryError
)
from main import handle_processing_failure, execute_pipeline, process_file
from gemini_transcriber import transcribe_audio

class TestDNSResilience(unittest.TestCase):
    def setUp(self):
        self.orig_token = Config.TELEGRAM_BOT_TOKEN
        self.orig_chat = Config.TELEGRAM_CHAT_ID
        Config.TELEGRAM_BOT_TOKEN = "test-token"
        Config.TELEGRAM_CHAT_ID = "123"

    def tearDown(self):
        Config.TELEGRAM_BOT_TOKEN = self.orig_token
        Config.TELEGRAM_CHAT_ID = self.orig_chat

    # =========================================================================
    # A. Classification Tests
    # =========================================================================

    def test_gaierror_11001_is_transient(self):
        err = socket.gaierror(11001, "getaddrinfo failed")
        self.assertTrue(is_network_or_dns_error(err))
        self.assertTrue(is_transient_error(err))

    def test_oserror_winerror_11001_is_transient(self):
        err = OSError()
        err.winerror = 11001
        err.strerror = "getaddrinfo failed"
        self.assertTrue(is_network_or_dns_error(err))
        self.assertTrue(is_transient_error(err))

    def test_oserror_errno_11001_is_transient(self):
        err = OSError(11001, "getaddrinfo failed")
        self.assertTrue(is_network_or_dns_error(err))
        self.assertTrue(is_transient_error(err))

    def test_requests_connection_error_caused_by_gaierror_is_transient(self):
        gai = socket.gaierror(11001, "getaddrinfo failed")
        req_err = requests.ConnectionError("Failed to establish connection")
        req_err.__cause__ = gai
        self.assertTrue(is_network_or_dns_error(req_err))
        self.assertTrue(is_transient_error(req_err))

    def test_generic_exception_caused_by_gaierror_is_transient(self):
        gai = socket.gaierror(11001, "getaddrinfo failed")
        gen_err = Exception("Wrapper exception")
        gen_err.__cause__ = gai
        self.assertTrue(is_network_or_dns_error(gen_err))
        self.assertTrue(is_transient_error(gen_err))

    def test_state_persistence_error_not_transient(self):
        err = StatePersistenceError("Local state write failed")
        self.assertFalse(is_network_or_dns_error(err))
        self.assertFalse(is_transient_error(err))

    def test_local_permission_error_not_transient(self):
        err = PermissionError(13, "Permission denied on local file")
        self.assertFalse(is_network_or_dns_error(err))
        self.assertFalse(is_transient_error(err))

    @patch("requests.post")
    def test_permanent_http_400_remains_permanent(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"ok": False, "error_code": 400, "description": "Bad Request"}
        mock_post.return_value = mock_resp
        
        with patch("time.sleep"):
            from telegram_notifier import send_telegram_message_with_retry
            with self.assertRaises(TelegramDeliveryError) as ctx:
                send_telegram_message_with_retry("msg", required=True)
            
            self.assertFalse(is_transient_error(ctx.exception))

    # =========================================================================
    # Requirement 1: Network Retry Limit & Property Preservation Tests
    # =========================================================================

    def test_six_consecutive_dns_failures_remain_retryable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            store = StateStore(file_path=state_file)

            for i in range(1, 7):
                store.mark_retryable("dns_net_file", "net.mp3", "drive", f"DNS error attempt {i}", is_network_error=True)
                entry = store._load()["dns_net_file"]
                self.assertEqual(entry["status"], "retryable")
                self.assertEqual(entry["network_retry_count"], i)

            # Confirm non-network permanent error still becomes failed_permanent
            store.mark_permanent_failed("perm_file", "bad.mp3", "Invalid format", source="drive")
            self.assertEqual(store._load()["perm_file"]["status"], "failed_permanent")

    def test_network_retry_delay_never_exceeds_max_seconds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            store = StateStore(file_path=state_file)

            for i in range(1, 15):
                store.mark_retryable("delay_file", "audio.mp3", "drive", f"DNS error {i}", is_network_error=True)
                entry = store._load()["delay_file"]
                next_dt = datetime.fromisoformat(entry["next_retry_at"])
                last_dt = datetime.fromisoformat(entry["last_attempt_at"])
                delay = (next_dt - last_dt).total_seconds()
                self.assertLessEqual(delay, Config.NETWORK_FILE_RETRY_MAX_SECONDS + 1)

    def test_restarting_statestore_does_not_reset_network_retry_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            store1 = StateStore(file_path=state_file)
            store1.mark_retryable("net_restart_file", "test.mp3", "drive", "DNS fail 1", is_network_error=True)
            store1.mark_retryable("net_restart_file", "test.mp3", "drive", "DNS fail 2", is_network_error=True)

            # Re-initialize store
            store2 = StateStore(file_path=state_file)
            entry = store2._load()["net_restart_file"]
            self.assertEqual(entry["network_retry_count"], 2)

            store2.mark_retryable("net_restart_file", "test.mp3", "drive", "DNS fail 3", is_network_error=True)
            entry_after = store2._load()["net_restart_file"]
            self.assertEqual(entry_after["network_retry_count"], 3)

    def test_historical_migration_does_not_loop_or_lose_marker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            store = StateStore(file_path=state_file)
            initial_data = {
                "mig_file": {
                    "file_id": "mig_file",
                    "name": "class.mp3",
                    "status": "failed_permanent",
                    "error_message": "[Errno 11001] getaddrinfo failed"
                }
            }
            store._save(initial_data)

            # First migration
            store1 = StateStore(file_path=state_file)
            entry1 = store1._load()["mig_file"]
            self.assertEqual(entry1["status"], "retryable")
            self.assertEqual(entry1["migrated_from"], "failed_permanent_network_error")

            # Subsequent mark_retryable call preserves migrated_from
            store1.mark_retryable("mig_file", "class.mp3", "drive", "DNS error retry", is_network_error=True)
            entry2 = store1._load()["mig_file"]
            self.assertEqual(entry2["status"], "retryable")
            self.assertEqual(entry2["migrated_from"], "failed_permanent_network_error")

            # Re-initializing does not double migrate or loop
            store2 = StateStore(file_path=state_file)
            entry3 = store2._load()["mig_file"]
            self.assertEqual(entry3["status"], "retryable")
            self.assertEqual(entry3["migrated_from"], "failed_permanent_network_error")

    # =========================================================================
    # B. Pipeline Failure & Checkpoint Preservation Tests
    # =========================================================================

    @patch("main.send_telegram_message")
    def test_pipeline_network_failure_preserves_checkpoint_and_marks_retryable(self, mock_send_tg):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            store = StateStore(file_path=state_file)

            audio_path = os.path.join(tmpdir, "long_audio.mp3")
            with open(audio_path, "wb") as f:
                f.write(b"fake audio data")

            progress_file = os.path.join(tmpdir, "long_audio_progress.json")
            progress_data = {
                "source_file_id": "file_dns_123",
                "original_name": "long_audio.mp3",
                "total_chunks": 40,
                "chunk_duration_ms": 60000,
                "audio_size": len(b"fake audio data"),
                "last_completed_chunk": 30,
                "last_idx": 29,
                "final_output": ["chunk 1..30 output"],
                "used_models": ["gemini-3.5-flash-lite"],
                "total_words": 500
            }
            with open(progress_file, "w", encoding="utf-8") as f:
                json.dump(progress_data, f)

            gai_err = socket.gaierror(11001, "getaddrinfo failed")

            with patch("config.Config.TEMP_DIR", tmpdir), \
                 patch("main.transcribe_audio", side_effect=gai_err):
                
                success, transcript = execute_pipeline(audio_path, "long_audio.mp3", "file_dns_123", store, source="drive")

                self.assertFalse(success)
                self.assertEqual(transcript, "")

                # Confirm mark_retryable was called and status is retryable
                entry = store._load().get("file_dns_123", {})
                self.assertEqual(entry.get("status"), "retryable")
                self.assertNotEqual(entry.get("status"), "failed_permanent")

                # Confirm progress file remains on disk with last_completed_chunk = 30
                self.assertTrue(os.path.exists(progress_file))
                with open(progress_file, "r", encoding="utf-8") as pf:
                    saved_prog = json.load(pf)
                self.assertEqual(saved_prog.get("last_completed_chunk"), 30)

    # =========================================================================
    # C. Resumption & Restart Tests
    # =========================================================================

    @patch("main.send_telegram_message")
    @patch("main.upload_file_to_drive")
    @patch("main.generate_drive_file_id", side_effect=["docx_111", "pdf_222"])
    @patch("main.create_pdf", return_value="fake.pdf")
    @patch("main.create_docx", return_value="fake.docx")
    @patch("gemini_transcriber.transcribe_chunk", return_value=("Chunk 31..40 done", "gemini-3.5-flash-lite"))
    @patch("gemini_transcriber.genai.Client")
    @patch("gemini_transcriber.AudioSegment.from_file")
    def test_resumption_from_checkpoint_skips_completed_chunks(
        self, mock_audio_from_file, mock_genai, mock_transcribe_chunk,
        mock_docx, mock_pdf, mock_gen_id, mock_upload, mock_send_tg
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            store = StateStore(file_path=state_file)

            # Store expired retryable state entry
            store.mark_retryable("file_res_999", "res_audio.mp3", "drive", "DNS failed", is_network_error=True)
            entry = store._load()["file_res_999"]
            entry["next_retry_at"] = "2000-01-01T00:00:00+00:00" # Expired, due for retry
            store._save(store._load())

            audio_path = os.path.join(tmpdir, "res_audio.mp3")
            with open(audio_path, "wb") as f:
                f.write(b"fake audio data")

            # 40 chunks total (40 mins = 2400000ms)
            mock_audio = MagicMock()
            mock_audio.__len__.return_value = 2400000
            mock_audio.__getitem__.return_value = mock_audio
            mock_audio_from_file.return_value = mock_audio

            # Pre-existing valid progress file up to chunk 30 (last_idx = 29)
            progress_file = os.path.join(tmpdir, "res_audio_progress.json")
            progress_data = {
                "source_file_id": "file_res_999",
                "original_name": "res_audio.mp3",
                "total_chunks": 40,
                "chunk_duration_ms": 60000,
                "audio_size": len(b"fake audio data"),
                "last_completed_chunk": 30,
                "last_idx": 29,
                "final_output": ["Chunk 1..30 completed"],
                "used_models": ["gemini-3.5-flash-lite"],
                "total_words": 1000
            }
            with open(progress_file, "w", encoding="utf-8") as f:
                json.dump(progress_data, f)

            with patch("config.Config.TEMP_DIR", tmpdir), \
                 patch("main.probe_audio_duration", return_value=2400), \
                 patch("gemini_transcriber.probe_audio_duration", return_value=2400), \
                 patch("gemini_transcriber.extract_audio_chunk", side_effect=lambda s, d, a, b: open(d, "wb").close()), \
                 patch("gemini_transcriber.detect_silence", return_value=[]), \
                 patch("main.verify_drive_file_exists", return_value=(False, None)):

                success, transcript = execute_pipeline(audio_path, "res_audio.mp3", "file_res_999", store, source="drive")

                self.assertTrue(success)

                # Confirm transcribe_chunk was called ONLY 10 times (chunks 31 to 40), skipping 1..30
                self.assertEqual(mock_transcribe_chunk.call_count, 10)

                # Confirm mark_processed called and status is completed
                self.assertTrue(store.is_processed("file_res_999"))

                # Confirm progress file deleted ONLY after final success
                self.assertFalse(os.path.exists(progress_file))

    # =========================================================================
    # E. Telegram Notification Non-Blocking Tests
    # =========================================================================

    @patch("main.send_telegram_message", side_effect=requests.ConnectionError("Network down"))
    def test_telegram_failure_during_dns_error_does_not_replace_original_exception(self, mock_send_tg):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            store = StateStore(file_path=state_file)

            gai_err = socket.gaierror(11001, "getaddrinfo failed")

            # Handle processing failure with DNS error and failing Telegram notification
            handle_processing_failure("item_123", "test_file.mp3", "drive", gai_err, store)

            # Confirm state was persisted as retryable BEFORE/DESPITE Telegram failure
            entry = store._load().get("item_123", {})
            self.assertEqual(entry.get("status"), "retryable")
            self.assertIn("getaddrinfo failed", entry.get("last_error", ""))

if __name__ == "__main__":
    unittest.main()
