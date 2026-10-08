import unittest
from unittest.mock import patch, MagicMock
from utils import calculate_backoff, retry, TelegramAPIError

class TestBackoffUtility(unittest.TestCase):
    def test_calculate_backoff_exponential_growth(self):
        def fixed_jitter(a, b):
            return 0.5

        b1 = calculate_backoff(1, base_delay=2.0, max_delay=60.0, jitter_fn=fixed_jitter)
        b2 = calculate_backoff(2, base_delay=2.0, max_delay=60.0, jitter_fn=fixed_jitter)
        b3 = calculate_backoff(3, base_delay=2.0, max_delay=60.0, jitter_fn=fixed_jitter)

        self.assertEqual(b1, 2.5) # 2 * (2^0) + 0.5
        self.assertEqual(b2, 4.5) # 2 * (2^1) + 0.5
        self.assertEqual(b3, 8.5) # 2 * (2^2) + 0.5

    def test_calculate_backoff_respects_max_delay(self):
        def fixed_jitter(a, b):
            return 0.2

        b_max = calculate_backoff(10, base_delay=2.0, max_delay=30.0, jitter_fn=fixed_jitter)
        self.assertEqual(b_max, 30.2) # min(30.0, 2 * 2^9) + 0.2

    @patch("utils.calculate_backoff", wraps=calculate_backoff)
    def test_retry_decorator_uses_calculate_backoff(self, mock_calc):
        mock_sleep = MagicMock()
        mock_func = MagicMock(side_effect=[TelegramAPIError(503, "Service Unavailable"), "success"])

        @retry(attempts=3, delay=5.0, sleep_fn=mock_sleep)
        def dummy_action():
            return mock_func()

        res = dummy_action()
        self.assertEqual(res, "success")
        mock_calc.assert_called_once()
        self.assertEqual(mock_calc.call_args[1]["base_delay"], 5.0)
        mock_sleep.assert_called_once()

if __name__ == "__main__":
    unittest.main()
