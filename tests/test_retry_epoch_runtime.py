import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from retry_epoch_runtime import (
    _build_retry_epoch_save_chunk,
    _reset_retry_epoch_after_progress,
)


class RetryEpochRuntimeTests(unittest.TestCase):
    def test_wrapper_commits_chunk_before_resetting_retry_epoch(self):
        events = []

        def original(*args, **kwargs):
            events.append("save")

        backend = MagicMock()
        wrapped = _build_retry_epoch_save_chunk(original)

        import retry_epoch_runtime
        old = retry_epoch_runtime._reset_retry_epoch_after_progress
        try:
            retry_epoch_runtime._reset_retry_epoch_after_progress = (
                lambda _backend, _source_key: events.append("reset")
            )
            wrapped(backend, "source", 93, "text", "model", 1, "owner")
        finally:
            retry_epoch_runtime._reset_retry_epoch_after_progress = old

        self.assertEqual(events, ["save", "reset"])

    def test_failed_save_never_forgives_retry_budget(self):
        def original(*args, **kwargs):
            raise RuntimeError("chunk persistence failed")

        backend = MagicMock()
        wrapped = _build_retry_epoch_save_chunk(original)

        import retry_epoch_runtime
        reset = MagicMock()
        old = retry_epoch_runtime._reset_retry_epoch_after_progress
        try:
            retry_epoch_runtime._reset_retry_epoch_after_progress = reset
            with self.assertRaises(RuntimeError):
                wrapped(backend, "source", 93, "text", "model", 1, "owner")
        finally:
            retry_epoch_runtime._reset_retry_epoch_after_progress = old

        reset.assert_not_called()

    def test_retry_state_is_reset_transactionally_after_forward_progress(self):
        now = datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)
        backend = MagicMock()
        backend.clock_fn.return_value = now
        ref = MagicMock()
        backend._job.return_value = ref
        backend._require_live_lease.return_value = {
            "status": "retryable",
            "retry_count": 5,
            "network_retry_count": 2,
            "last_error": "old transient error",
            "error_message": "Exceeded max retries: old semantic error",
        }

        transaction = MagicMock()
        backend._transaction.side_effect = lambda callback: callback(transaction)

        _reset_retry_epoch_after_progress(backend, "source")

        transaction.set.assert_called_once()
        _, update = transaction.set.call_args.args[:2]
        self.assertEqual(update["status"], "in_progress")
        self.assertEqual(update["retry_count"], 0)
        self.assertEqual(update["network_retry_count"], 0)
        self.assertIsNone(update["next_retry_at"])
        self.assertIsNone(update["last_error"])
        self.assertIsNone(update["error_message"])
        self.assertEqual(update["retry_epoch_recovered_at"], now)
        self.assertIn("old semantic error", update["last_recovered_error"])

    def test_clean_in_progress_job_is_left_untouched(self):
        backend = MagicMock()
        backend.clock_fn.return_value = datetime.now(timezone.utc)
        backend._job.return_value = MagicMock()
        backend._require_live_lease.return_value = {
            "status": "in_progress",
            "retry_count": 0,
            "network_retry_count": 0,
        }
        transaction = MagicMock()
        backend._transaction.side_effect = lambda callback: callback(transaction)

        _reset_retry_epoch_after_progress(backend, "source")

        transaction.set.assert_not_called()


if __name__ == "__main__":
    unittest.main()
