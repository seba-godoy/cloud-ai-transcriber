import unittest
from unittest.mock import patch, MagicMock
from telegram_notifier import send_long_telegram_message, send_telegram_message_with_retry
from utils import TelegramDeliveryError, is_transient_error
from config import Config

class TestTelegramPlainTextEscaping(unittest.TestCase):
    def setUp(self):
        self.orig_token = Config.TELEGRAM_BOT_TOKEN
        self.orig_chat = Config.TELEGRAM_CHAT_ID
        Config.TELEGRAM_BOT_TOKEN = "test-token"
        Config.TELEGRAM_CHAT_ID = "123"

    def tearDown(self):
        Config.TELEGRAM_BOT_TOKEN = self.orig_token
        Config.TELEGRAM_CHAT_ID = self.orig_chat

    @patch("requests.post")
    def test_transcripts_with_special_symbols_sent_as_plain_text_without_error(self, mock_post):
        mock_post.return_value.json.return_value = {"ok": True}
        mock_post.return_value.status_code = 200

        special_transcripts = [
            "Resultado: 5 < 10 y 20 > 5.",
            "Empresas A & B firmaron el contrato.",
            "Inyección de prueba: <script>alert(1)</script>",
            "Implementación: List<Cliente> listaClientes = new ArrayList<>();"
        ]

        for text in special_transcripts:
            delivered = send_long_telegram_message(text, parse_mode=None, required=True)
            self.assertTrue(delivered)
            
            call_json = mock_post.call_args[1].get("json", {})
            self.assertNotIn("parse_mode", call_json)
            self.assertIn(text, call_json.get("text", ""))

    def test_missing_credentials_handling(self):
        Config.TELEGRAM_BOT_TOKEN = ""
        Config.TELEGRAM_CHAT_ID = ""

        # required=False returns False
        res = send_telegram_message_with_retry("hello", required=False)
        self.assertFalse(res)

        # required=True raises TelegramDeliveryError with transient=False
        with self.assertRaises(TelegramDeliveryError) as ctx:
            send_telegram_message_with_retry("hello", required=True)
        self.assertIn("credentials missing", str(ctx.exception).lower())
        self.assertFalse(is_transient_error(ctx.exception))

    @patch("time.sleep")
    @patch("requests.post")
    def test_delivery_error_transient_vs_permanent_classification(self, mock_post, mock_sleep):
        # 503 Service Unavailable -> transient
        mock_post.return_value.json.return_value = {"ok": False, "error_code": 503, "description": "Service Unavailable"}
        mock_post.return_value.status_code = 503

        with self.assertRaises(TelegramDeliveryError) as ctx503:
            send_telegram_message_with_retry("test msg", required=True)
        self.assertTrue(is_transient_error(ctx503.exception))

        # 400 Bad Request -> permanent
        mock_post.return_value.json.return_value = {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}
        mock_post.return_value.status_code = 400

        with self.assertRaises(TelegramDeliveryError) as ctx400:
            send_telegram_message_with_retry("test msg", required=True)
        self.assertFalse(is_transient_error(ctx400.exception))

if __name__ == "__main__":
    unittest.main()
