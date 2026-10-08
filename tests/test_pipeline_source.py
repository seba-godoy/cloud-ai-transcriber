import unittest
from unittest.mock import MagicMock, patch
from main import execute_pipeline
from config import Config

class TestPipelineSourceObligation(unittest.TestCase):
    def setUp(self):
        self.orig_token = Config.TELEGRAM_BOT_TOKEN
        self.orig_chat = Config.TELEGRAM_CHAT_ID
        Config.TELEGRAM_BOT_TOKEN = "test-token"
        Config.TELEGRAM_CHAT_ID = "123"

    def tearDown(self):
        Config.TELEGRAM_BOT_TOKEN = self.orig_token
        Config.TELEGRAM_CHAT_ID = self.orig_chat

    @patch("time.sleep")
    def test_missing_source_keyword_arg_raises_type_error(self, mock_sleep):
        mock_store = MagicMock()
        with self.assertRaises(TypeError):
            execute_pipeline("dummy.mp3", "dummy.mp3", "id123", mock_store)

    @patch("time.sleep")
    def test_invalid_source_value_raises_value_error(self, mock_sleep):
        mock_store = MagicMock()
        with self.assertRaises(ValueError):
            execute_pipeline("dummy.mp3", "dummy.mp3", "id123", mock_store, source="invalid_source")

if __name__ == "__main__":
    unittest.main()
