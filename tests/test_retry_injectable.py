import unittest
from unittest.mock import MagicMock
from utils import retry, is_transient_error
import drive_client

class TestInjectableRetry(unittest.TestCase):
    def test_retry_attempts_and_no_sleep_on_last_attempt(self):
        mock_sleep = MagicMock()
        mock_jitter = MagicMock(return_value=0.5)

        operation = MagicMock(side_effect=TimeoutError("Transient network timeout"))

        @retry(attempts=3, delay=2, sleep_fn=mock_sleep, jitter_fn=mock_jitter)
        def run_op():
            return operation()

        with self.assertRaises(TimeoutError):
            run_op()

        self.assertEqual(operation.call_count, 3)
        # Should sleep exactly 2 times (after attempt 1 and 2), NOT after attempt 3
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_has_calls([unittest.mock.call(2.5), unittest.mock.call(4.5)])

    def test_get_drive_service_is_not_decorated(self):
        # Confirm get_drive_service is a plain function without decorator wrapper attributes
        self.assertFalse(hasattr(drive_client.get_drive_service, "__wrapped__"))

if __name__ == "__main__":
    unittest.main()
