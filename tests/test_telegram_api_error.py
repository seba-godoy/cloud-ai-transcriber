import unittest
from unittest.mock import patch, MagicMock
from utils import TelegramAPIError, is_transient_error
from telegram_notifier import get_telegram_file_info
from main import process_telegram_audio
from config import Config

class TestTelegramAPIError(unittest.TestCase):
    def setUp(self):
        self.orig_token = Config.TELEGRAM_BOT_TOKEN
        self.orig_chat = Config.TELEGRAM_CHAT_ID
        Config.TELEGRAM_BOT_TOKEN = "test-token"
        Config.TELEGRAM_CHAT_ID = "123"

    def tearDown(self):
        Config.TELEGRAM_BOT_TOKEN = self.orig_token
        Config.TELEGRAM_CHAT_ID = self.orig_chat

    @patch("requests.get")
    def test_ok_false_raises_telegram_api_error_with_retry_after(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 15",
            "parameters": {"retry_after": 15}
        }
        mock_get.return_value = mock_resp

        with self.assertRaises(TelegramAPIError) as ctx:
            get_telegram_file_info("file_123")

        err = ctx.exception
        self.assertEqual(err.error_code, 429)
        self.assertEqual(err.retry_after, 15)
        self.assertTrue(is_transient_error(err))

    @patch("requests.get")
    def test_ok_true_missing_result_raises_value_error(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ok": True,
            "result": {} # missing file_path
        }
        mock_get.return_value = mock_resp

        with self.assertRaises(ValueError):
            get_telegram_file_info("file_123")

    @patch("requests.get")
    def test_ok_true_with_valid_file_path_returns_result(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ok": True,
            "result": {"file_id": "file_123", "file_path": "documents/file.mp3", "file_size": 1024}
        }
        mock_get.return_value = mock_resp

        res = get_telegram_file_info("file_123")
        self.assertEqual(res["file_path"], "documents/file.mp3")

    @patch("main.handle_processing_failure")
    @patch("main.send_telegram_message")
    @patch("main.get_telegram_file_info")
    def test_process_telegram_audio_missing_file_size_timeout_caught_by_handler(
        self, mock_get_info, mock_send_tg, mock_handle_failure
    ):
        mock_get_info.side_effect = TimeoutError("Telegram getFile request timed out")
        msg = {
            "audio": {
                "file_id": "tg_file_999",
                "file_name": "audio_no_size.mp3"
            }
        }
        mock_store = MagicMock()
        mock_store.is_processed.return_value = False

        process_telegram_audio(msg, telegram_update_id=101, state_store=mock_store)

        mock_handle_failure.assert_called_once()
        args = mock_handle_failure.call_args[0]
        self.assertEqual(args[0], "tg_file_999")
        self.assertEqual(args[1], "audio_no_size.mp3")
        self.assertEqual(args[2], "telegram")
        self.assertIsInstance(args[3], TimeoutError)

if __name__ == "__main__":
    unittest.main()
