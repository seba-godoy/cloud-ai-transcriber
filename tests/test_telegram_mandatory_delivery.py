import unittest
from unittest.mock import patch, MagicMock
from telegram_notifier import send_long_telegram_message, send_telegram_message_with_retry
from utils import TelegramDeliveryError, TelegramAPIError
from config import Config

class TestTelegramMandatoryDelivery(unittest.TestCase):
    def setUp(self):
        self.orig_token = Config.TELEGRAM_BOT_TOKEN
        self.orig_chat = Config.TELEGRAM_CHAT_ID
        Config.TELEGRAM_BOT_TOKEN = "test-token"
        Config.TELEGRAM_CHAT_ID = "123"

    def tearDown(self):
        Config.TELEGRAM_BOT_TOKEN = self.orig_token
        Config.TELEGRAM_CHAT_ID = self.orig_chat

    @patch("telegram_notifier.requests.post")
    def test_all_chunks_delivered_returns_true(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "result": {}}
        mock_post.return_value = mock_resp

        text = "Paragraph 1\n\n" + ("x" * 3000) + "\n\n" + ("y" * 3000)
        res = send_long_telegram_message(text, max_chars=4000, required=True)

        self.assertTrue(res)
        self.assertGreater(mock_post.call_count, 1)

    @patch("telegram_notifier.requests.post")
    def test_first_chunk_fails_raises_telegram_delivery_error(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.json.return_value = {"ok": False, "error_code": 500, "description": "Internal server error"}
        mock_post.return_value = mock_resp

        with patch("time.sleep"):
            with self.assertRaises(TelegramDeliveryError):
                send_long_telegram_message("Test text", required=True)

    @patch("telegram_notifier.requests.post")
    def test_intermediate_chunk_fails_stops_subsequent_chunks(self, mock_post):
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {"ok": True, "result": {}}

        err_resp = MagicMock()
        err_resp.status_code = 500
        err_resp.json.return_value = {"ok": False, "error_code": 500, "description": "Internal server error"}

        mock_post.side_effect = [ok_resp, err_resp, err_resp, err_resp]

        chunk1 = "a" * 3500
        chunk2 = "b" * 3500
        chunk3 = "c" * 3500
        full_text = f"{chunk1}\n\n{chunk2}\n\n{chunk3}"

        with patch("time.sleep"):
            with self.assertRaises(TelegramDeliveryError):
                send_long_telegram_message(full_text, max_chars=4000, required=True)

        self.assertEqual(mock_post.call_count, 4)

    @patch("telegram_notifier.requests.post")
    def test_retry_after_respected_exactly(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.json.return_value = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 12}
        }
        mock_post.return_value = mock_resp

        mock_sleep = MagicMock()
        with patch("time.sleep", mock_sleep):
            with self.assertRaises(TelegramDeliveryError):
                send_telegram_message_with_retry("hello", required=True)

        mock_sleep.assert_any_call(12.0)

    @patch("telegram_notifier.requests.post")
    def test_permanent_400_does_not_make_3_attempts(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: can't parse entities"
        }
        mock_post.return_value = mock_resp

        mock_sleep = MagicMock()
        with patch("time.sleep", mock_sleep):
            with self.assertRaises(TelegramDeliveryError):
                send_telegram_message_with_retry("hello", required=True)

        self.assertEqual(mock_post.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("telegram_notifier.requests.post")
    def test_permanent_404_does_not_make_3_attempts(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.return_value = {
            "ok": False,
            "error_code": 404,
            "description": "Not Found: bot token invalid"
        }
        mock_post.return_value = mock_resp

        mock_sleep = MagicMock()
        with patch("time.sleep", mock_sleep):
            with self.assertRaises(TelegramDeliveryError):
                send_telegram_message_with_retry("hello", required=True)

        self.assertEqual(mock_post.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("telegram_notifier.requests.post")
    def test_permanent_418_json_does_not_retry(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 418
        mock_resp.json.return_value = {
            "ok": False,
            "error_code": 418,
            "description": "I'm a teapot (unknown 4xx client error)"
        }
        mock_post.return_value = mock_resp

        mock_sleep = MagicMock()
        with patch("time.sleep", mock_sleep):
            with self.assertRaises(TelegramDeliveryError):
                send_telegram_message_with_retry("hello", required=True)

        self.assertEqual(mock_post.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("telegram_notifier.requests.post")
    def test_permanent_418_html_does_not_retry(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 418
        mock_resp.json.side_effect = ValueError("Non-JSON HTML body")
        mock_post.return_value = mock_resp

        mock_sleep = MagicMock()
        with patch("time.sleep", mock_sleep):
            with self.assertRaises(TelegramDeliveryError):
                send_telegram_message_with_retry("hello", required=True)

        self.assertEqual(mock_post.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("telegram_notifier.requests.post")
    def test_transient_408_retries(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 408
        mock_resp.json.return_value = {
            "ok": False,
            "error_code": 408,
            "description": "Request Timeout"
        }
        mock_post.return_value = mock_resp

        mock_sleep = MagicMock()
        with patch("time.sleep", mock_sleep):
            with self.assertRaises(TelegramDeliveryError):
                send_telegram_message_with_retry("hello", required=True, attempts=3)

        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("telegram_notifier.requests.post")
    def test_transient_429_retries(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.json.return_value = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests"
        }
        mock_post.return_value = mock_resp

        mock_sleep = MagicMock()
        with patch("time.sleep", mock_sleep):
            with self.assertRaises(TelegramDeliveryError):
                send_telegram_message_with_retry("hello", required=True, attempts=3)

        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("telegram_notifier.requests.post")
    def test_transient_503_html_retries(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.json.side_effect = ValueError("Non-JSON 503 HTML body")
        mock_post.return_value = mock_resp

        mock_sleep = MagicMock()
        with patch("time.sleep", mock_sleep):
            with self.assertRaises(TelegramDeliveryError):
                send_telegram_message_with_retry("hello", required=True, attempts=3)

        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("telegram_notifier.requests.post")
    def test_required_false_returns_false_on_failure(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.json.return_value = {"ok": False, "error_code": 503, "description": "Service Unavailable"}
        mock_post.return_value = mock_resp

        with patch("time.sleep"):
            res = send_telegram_message_with_retry("hello", required=False)

        self.assertFalse(res)

if __name__ == "__main__":
    unittest.main()
