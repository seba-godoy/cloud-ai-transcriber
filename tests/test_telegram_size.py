import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from telegram_notifier import download_telegram_file
from config import Config

class TestTelegramFileSize(unittest.TestCase):
    def setUp(self):
        self.orig_token = Config.TELEGRAM_BOT_TOKEN
        self.orig_chat = Config.TELEGRAM_CHAT_ID
        Config.TELEGRAM_BOT_TOKEN = "fake_token"
        Config.TELEGRAM_CHAT_ID = "12345"

    def tearDown(self):
        Config.TELEGRAM_BOT_TOKEN = self.orig_token
        Config.TELEGRAM_CHAT_ID = self.orig_chat

    @patch("config.Config.TELEGRAM_MAX_DOWNLOAD_BYTES", 1000)
    def test_expected_size_exceeded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            save_path = os.path.join(tmpdir, "test.mp3")
            with self.assertRaises(ValueError) as ctx:
                download_telegram_file("dummy_path", save_path, expected_size=5000)
            self.assertIn("exceeds limit", str(ctx.exception))
            self.assertFalse(os.path.exists(save_path))

    @patch("requests.get")
    @patch("config.Config.TELEGRAM_MAX_DOWNLOAD_BYTES", 100)
    def test_streaming_size_exceeded_deletes_temp(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.iter_content.return_value = [b"a" * 80, b"b" * 80]
        mock_get.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            save_path = os.path.join(tmpdir, "streaming_test.mp3")
            with self.assertRaises(ValueError) as ctx:
                download_telegram_file("dummy_path", save_path)
            self.assertIn("exceeded limit", str(ctx.exception))
            self.assertFalse(os.path.exists(save_path))

if __name__ == "__main__":
    unittest.main()
