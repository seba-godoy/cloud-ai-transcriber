import unittest
from unittest.mock import MagicMock, patch

from main import execute_pipeline, process_file
from utils import StatePersistenceError


class NotificationClaimPersistenceTests(unittest.TestCase):
    @staticmethod
    def _firestore_store():
        store = MagicMock(backend_name="firestore", owner_id="worker-a",
                          lease_ttl_seconds=900)
        store.bind_source.return_value = "drive:file-1"
        store.acquire_lease.return_value = True
        store.is_retry_due.return_value = False
        return store

    def test_initial_claim_persistence_failure_is_fatal_before_download(self):
        store = self._firestore_store()
        store.claim_notification.side_effect = StatePersistenceError("claim unavailable")
        with patch("main.download_file") as download, \
             patch("main.execute_pipeline") as pipeline, \
             patch("main.send_telegram_message"):
            with self.assertRaisesRegex(StatePersistenceError, "claim unavailable"):
                process_file({"id": "file-1", "name": "class.m4a"}, store)
        download.assert_not_called()
        pipeline.assert_not_called()

    def test_estimate_claim_persistence_failure_is_fatal_before_transcription(self):
        store = self._firestore_store()
        store.claim_notification.side_effect = StatePersistenceError("estimate claim unavailable")
        with patch("main.probe_audio_duration", return_value=60), \
             patch("main.transcribe_audio") as transcribe, \
             patch("main.send_telegram_message"):
            with self.assertRaisesRegex(StatePersistenceError, "estimate claim unavailable"):
                execute_pipeline("missing.m4a", "class.m4a", "file-1", store,
                                 source="drive")
        transcribe.assert_not_called()

    def test_initial_telegram_failure_after_claim_is_best_effort(self):
        store = self._firestore_store()
        store.claim_notification.return_value = True
        with patch("main.send_telegram_message", side_effect=RuntimeError("Telegram down")), \
             patch("main.download_file", return_value="downloaded.m4a") as download, \
             patch("main.execute_pipeline") as pipeline:
            process_file({"id": "file-1", "name": "class.m4a"}, store)
        download.assert_called_once_with("file-1", "class.m4a")
        pipeline.assert_called_once()

    def test_false_initial_claim_skips_telegram_and_continues(self):
        store = self._firestore_store()
        store.claim_notification.return_value = False
        with patch("main.send_telegram_message") as notify, \
             patch("main.download_file", return_value="downloaded.m4a") as download, \
             patch("main.execute_pipeline") as pipeline:
            process_file({"id": "file-1", "name": "class.m4a"}, store)
        notify.assert_not_called()
        download.assert_called_once()
        pipeline.assert_called_once()

    def test_estimate_telegram_failure_after_claim_is_best_effort(self):
        store = MagicMock(backend_name="local")
        with patch("main.probe_audio_duration", return_value=60), \
             patch("main.send_telegram_message", side_effect=RuntimeError("Telegram down")), \
             patch("main.transcribe_audio", return_value=("transcript", "model")) as transcribe, \
             patch("main.create_docx", side_effect=RuntimeError("stop after transcription")):
            success, _ = execute_pipeline("missing.m4a", "class.m4a", "file-1", store,
                                          source="drive")
        self.assertFalse(success)
        transcribe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
