import unittest
from unittest.mock import patch
import requests
from telegram_notifier import send_telegram_message
from config import Config

class TestNonBlockingTelegram(unittest.TestCase):
    def setUp(self):
        self.orig_token = Config.TELEGRAM_BOT_TOKEN
        self.orig_chat = Config.TELEGRAM_CHAT_ID
        Config.TELEGRAM_BOT_TOKEN = "fake_token"
        Config.TELEGRAM_CHAT_ID = "12345"

    def tearDown(self):
        Config.TELEGRAM_BOT_TOKEN = self.orig_token
        Config.TELEGRAM_CHAT_ID = self.orig_chat

    @patch("time.sleep")
    @patch("requests.post")
    def test_send_telegram_message_network_failure(self, mock_post, mock_sleep):
        mock_post.side_effect = requests.ConnectionError("Network disconnected")
        
        # Should return False cleanly without raising exception
        success = send_telegram_message("Test message")
        self.assertFalse(success)

if __name__ == "__main__":
    unittest.main()
