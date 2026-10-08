import unittest
from unittest.mock import patch
from telegram_notifier import is_authorized_telegram_message

class TestTelegramAuth(unittest.TestCase):
    @patch("config.Config.TELEGRAM_CHAT_ID", "5572783241")
    def test_authorized_string_match(self):
        msg = {"chat": {"id": "5572783241"}}
        self.assertTrue(is_authorized_telegram_message(msg))

    @patch("config.Config.TELEGRAM_CHAT_ID", "5572783241")
    def test_authorized_int_match(self):
        msg = {"chat": {"id": 5572783241}}
        self.assertTrue(is_authorized_telegram_message(msg))

    @patch("config.Config.TELEGRAM_CHAT_ID", "5572783241")
    def test_unauthorized_chat_id(self):
        msg = {"chat": {"id": 99999999}}
        self.assertFalse(is_authorized_telegram_message(msg))

    @patch("config.Config.TELEGRAM_CHAT_ID", "5572783241")
    def test_missing_chat_or_id(self):
        self.assertFalse(is_authorized_telegram_message({}))
        self.assertFalse(is_authorized_telegram_message({"chat": {}}))
        self.assertFalse(is_authorized_telegram_message({"chat": {"id": None}}))

    @patch("config.Config.TELEGRAM_CHAT_ID", "")
    def test_empty_config_chat_id(self):
        msg = {"chat": {"id": 5572783241}}
        self.assertFalse(is_authorized_telegram_message(msg))

if __name__ == "__main__":
    unittest.main()
