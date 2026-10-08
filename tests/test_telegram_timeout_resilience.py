import unittest
from unittest.mock import MagicMock, patch

import requests

from config import Config
from telegram_notifier import get_telegram_updates


class TestTelegramPollingTimeoutResilience(unittest.TestCase):
    def setUp(self):
        self.orig_token = Config.TELEGRAM_BOT_TOKEN
        Config.TELEGRAM_BOT_TOKEN = "test-token"

    def tearDown(self):
        Config.TELEGRAM_BOT_TOKEN = self.orig_token

    @patch("requests.get")
    def test_read_timeout_retries_then_succeeds(self, mock_get):
        timeout_error = requests.ReadTimeout("Telegram read timed out")
        ok_response = MagicMock()
        ok_response.status_code = 200
        ok_response.json.return_value = {
            "ok": True,
            "result": [{"update_id": 123}],
        }
        mock_get.side_effect = [timeout_error, ok_response]
        mock_sleep = MagicMock()

        result = get_telegram_updates(
            offset=10,
            timeout=5,
            attempts=3,
            sleep_fn=mock_sleep,
            jitter_fn=lambda _a, _b: 0.0,
        )

        self.assertEqual(result, [{"update_id": 123}])
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(2.0)

    @patch("requests.get")
    def test_exhausted_read_timeouts_raise_original_timeout_not_nameerror(self, mock_get):
        mock_get.side_effect = [
            requests.ReadTimeout("timeout 1"),
            requests.ReadTimeout("timeout 2"),
            requests.ReadTimeout("timeout 3"),
        ]
        mock_sleep = MagicMock()

        with self.assertRaises(requests.ReadTimeout) as ctx:
            get_telegram_updates(
                offset=10,
                timeout=5,
                attempts=3,
                sleep_fn=mock_sleep,
                jitter_fn=lambda _a, _b: 0.0,
            )

        self.assertEqual(str(ctx.exception), "timeout 3")
        self.assertEqual(mock_get.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("requests.get")
    def test_connection_error_retries_then_succeeds(self, mock_get):
        connection_error = requests.ConnectionError("temporary network failure")
        ok_response = MagicMock()
        ok_response.status_code = 200
        ok_response.json.return_value = {"ok": True, "result": []}
        mock_get.side_effect = [connection_error, ok_response]
        mock_sleep = MagicMock()

        result = get_telegram_updates(
            timeout=5,
            attempts=3,
            sleep_fn=mock_sleep,
            jitter_fn=lambda _a, _b: 0.0,
        )

        self.assertEqual(result, [])
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(2.0)


if __name__ == "__main__":
    unittest.main()
