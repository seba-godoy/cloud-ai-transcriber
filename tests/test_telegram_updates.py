import unittest
from unittest.mock import patch, MagicMock

from config import Config
from utils import TelegramAPIError
from telegram_notifier import get_telegram_updates

class TestTelegramUpdates(unittest.TestCase):
    def setUp(self):
        self.orig_token = Config.TELEGRAM_BOT_TOKEN
        Config.TELEGRAM_BOT_TOKEN = "test-token"

    def tearDown(self):
        Config.TELEGRAM_BOT_TOKEN = self.orig_token

    @patch("requests.get")
    def test_ok_true_and_empty_list_returns_empty_list(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "result": []}
        mock_get.return_value = mock_resp

        res = get_telegram_updates(offset=10, timeout=5)
        self.assertEqual(res, [])

    @patch("requests.get")
    def test_ok_true_result_dict_raises_value_error(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True, "result": {}}
        mock_get.return_value = mock_resp

        with self.assertRaises(ValueError) as ctx:
            get_telegram_updates(offset=10, timeout=5)

        self.assertIn("not a list", str(ctx.exception))

    @patch("requests.get")
    def test_json_400_one_attempt(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"ok": False, "error_code": 400, "description": "Bad Request"}
        mock_get.return_value = mock_resp

        mock_sleep = MagicMock()
        with self.assertRaises(TelegramAPIError) as ctx:
            get_telegram_updates(offset=10, timeout=5, attempts=3, sleep_fn=mock_sleep)

        self.assertEqual(ctx.exception.error_code, 400)
        self.assertEqual(mock_get.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("requests.get")
    def test_http_401_does_not_retry_three_times(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"ok": False, "error_code": 401, "description": "Unauthorized token"}
        mock_get.return_value = mock_resp

        mock_sleep = MagicMock()
        with self.assertRaises(TelegramAPIError) as ctx:
            get_telegram_updates(offset=10, timeout=5, sleep_fn=mock_sleep)

        self.assertEqual(ctx.exception.error_code, 401)
        self.assertEqual(mock_get.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("requests.get")
    def test_http_404_one_attempt(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.return_value = {"ok": False, "error_code": 404, "description": "Not Found"}
        mock_get.return_value = mock_resp

        mock_sleep = MagicMock()
        with self.assertRaises(TelegramAPIError) as ctx:
            get_telegram_updates(offset=10, timeout=5, sleep_fn=mock_sleep)

        self.assertEqual(ctx.exception.error_code, 404)
        self.assertEqual(mock_get.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("requests.get")
    def test_permanent_418_json_one_attempt(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 418
        mock_resp.json.return_value = {"ok": False, "error_code": 418, "description": "I'm a teapot"}
        mock_get.return_value = mock_resp

        mock_sleep = MagicMock()
        with self.assertRaises(TelegramAPIError) as ctx:
            get_telegram_updates(offset=10, timeout=5, attempts=3, sleep_fn=mock_sleep)

        self.assertEqual(ctx.exception.error_code, 418)
        self.assertEqual(mock_get.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("requests.get")
    def test_permanent_418_html_one_attempt(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 418
        mock_resp.json.side_effect = ValueError("Non-JSON HTML body")
        mock_get.return_value = mock_resp

        mock_sleep = MagicMock()
        with self.assertRaises(TelegramAPIError) as ctx:
            get_telegram_updates(offset=10, timeout=5, attempts=3, sleep_fn=mock_sleep)

        self.assertEqual(ctx.exception.error_code, 418)
        self.assertEqual(mock_get.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("requests.get")
    def test_http_409_conflict_logs_and_does_not_retry(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 409
        mock_resp.json.return_value = {
            "ok": False, 
            "error_code": 409, 
            "description": "Conflict: terminated by other getUpdates request"
        }
        mock_get.return_value = mock_resp

        mock_sleep = MagicMock()
        with self.assertRaises(TelegramAPIError) as ctx:
            get_telegram_updates(offset=10, timeout=5, sleep_fn=mock_sleep)

        self.assertEqual(ctx.exception.error_code, 409)
        self.assertEqual(mock_get.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("requests.get")
    def test_transient_408_retries(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 408
        mock_resp.json.return_value = {
            "ok": False,
            "error_code": 408,
            "description": "Request Timeout"
        }
        mock_get.return_value = mock_resp

        mock_sleep = MagicMock()
        with self.assertRaises(TelegramAPIError) as ctx:
            get_telegram_updates(offset=10, timeout=5, attempts=3, sleep_fn=mock_sleep)

        self.assertEqual(ctx.exception.error_code, 408)
        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("requests.get")
    def test_http_429_retry_after_120_calls_sleep_120(self, mock_get):
        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429
        mock_resp_429.json.return_value = {
            "ok": False, 
            "error_code": 429, 
            "description": "Too Many Requests", 
            "parameters": {"retry_after": 120}
        }
        mock_get.return_value = mock_resp_429

        mock_sleep = MagicMock()
        with self.assertRaises(TelegramAPIError) as ctx:
            get_telegram_updates(offset=10, timeout=5, attempts=3, sleep_fn=mock_sleep)

        self.assertEqual(ctx.exception.error_code, 429)
        self.assertEqual(ctx.exception.retry_after, 120)
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_called_with(120.0)

    @patch("requests.get")
    def test_http_429_succeeds_on_second_attempt(self, mock_get):
        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429
        mock_resp_429.json.return_value = {
            "ok": False, 
            "error_code": 429, 
            "description": "Too Many Requests", 
            "parameters": {"retry_after": 5}
        }

        mock_resp_ok = MagicMock()
        mock_resp_ok.status_code = 200
        mock_resp_ok.json.return_value = {"ok": True, "result": [{"update_id": 100}]}

        mock_get.side_effect = [mock_resp_429, mock_resp_ok]

        mock_sleep = MagicMock()
        res = get_telegram_updates(offset=10, timeout=5, attempts=3, sleep_fn=mock_sleep)

        self.assertEqual(res, [{"update_id": 100}])
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(5.0)

    @patch("requests.get")
    def test_http_503_succeeds_on_subsequent_attempt(self, mock_get):
        mock_resp_503 = MagicMock()
        mock_resp_503.status_code = 503
        mock_resp_503.json.return_value = {"ok": False, "error_code": 503, "description": "Service Unavailable"}

        mock_resp_ok = MagicMock()
        mock_resp_ok.status_code = 200
        mock_resp_ok.json.return_value = {"ok": True, "result": [{"update_id": 200}]}

        mock_get.side_effect = [mock_resp_503, mock_resp_ok]

        mock_sleep = MagicMock()
        res = get_telegram_updates(offset=10, timeout=5, attempts=3, sleep_fn=mock_sleep)

        self.assertEqual(res, [{"update_id": 200}])
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 1)

    @patch("requests.get")
    def test_html_503_retry(self, mock_get):
        mock_resp_html503 = MagicMock()
        mock_resp_html503.status_code = 503
        mock_resp_html503.json.side_effect = ValueError("Non-JSON HTML body")

        mock_get.return_value = mock_resp_html503

        mock_sleep = MagicMock()
        with self.assertRaises(TelegramAPIError) as ctx:
            get_telegram_updates(offset=10, timeout=5, attempts=3, sleep_fn=mock_sleep)

        self.assertEqual(ctx.exception.error_code, 503)
        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("requests.get")
    def test_html_503_then_json_ok_success(self, mock_get):
        mock_resp_html503 = MagicMock()
        mock_resp_html503.status_code = 503
        mock_resp_html503.json.side_effect = ValueError("Non-JSON HTML body")

        mock_resp_ok = MagicMock()
        mock_resp_ok.status_code = 200
        mock_resp_ok.json.return_value = {"ok": True, "result": [{"update_id": 300}]}

        mock_get.side_effect = [mock_resp_html503, mock_resp_ok]

        mock_sleep = MagicMock()
        res = get_telegram_updates(offset=10, timeout=5, attempts=3, sleep_fn=mock_sleep)

        self.assertEqual(res, [{"update_id": 300}])
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 1)

    @patch("requests.get")
    def test_html_400_no_retry(self, mock_get):
        mock_resp_html400 = MagicMock()
        mock_resp_html400.status_code = 400
        mock_resp_html400.json.side_effect = ValueError("Non-JSON HTML body")

        mock_get.return_value = mock_resp_html400

        mock_sleep = MagicMock()
        with self.assertRaises(TelegramAPIError) as ctx:
            get_telegram_updates(offset=10, timeout=5, attempts=3, sleep_fn=mock_sleep)

        self.assertEqual(ctx.exception.error_code, 400)
        self.assertEqual(mock_get.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("requests.get")
    def test_200_non_json_raises_value_error(self, mock_get):
        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        mock_resp_200.json.side_effect = ValueError("Invalid JSON")

        mock_get.return_value = mock_resp_200

        with self.assertRaises(ValueError) as ctx:
            get_telegram_updates(offset=10, timeout=5)

        self.assertIn("non-JSON response on 2xx", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
