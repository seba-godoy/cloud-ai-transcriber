import unittest
from copy import deepcopy
from datetime import datetime, timezone
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from state_backend import FirestoreStateBackend, RecoveryRefusedError
from tests.test_state_backend import FakeClient
from utils import StatePersistenceError
from tools import requeue_firestore_job


class FirestoreRequeueTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
        self.client = FakeClient()
        self.backend = FirestoreStateBackend("test", client=self.client,
                                             clock_fn=lambda: self.now)
        self.key = "a" * 64
        self.path = f"transcription_jobs/{self.key}"
        self.original = {
            "source_key": self.key, "source": "drive", "file_id": "original-id",
            "name": "260826_GDC_Reunión parte 1.m4a", "status": "failed_permanent",
            "error_message": "'ascii' codec can't encode character in position 16",
            "completed_chunks": 3, "partial_uploads": {"docx": "d1"},
            "final_ids": {"pdf": "p1"}, "initial_notification_claimed_at": self.now,
            "estimate_notification_claimed_at": self.now, "lease_owner": "stale",
            "lease_expires_at": self.now, "retry_count": 4,
        }
        self.client.data[self.path] = deepcopy(self.original)
        self.client.data[f"{self.path}/chunks/000000"] = {"status": "completed", "chunk_index": 0}

    def test_guarded_requeue_is_transactional_and_preserves_durable_fields(self):
        before_transactions = self.client.transactions
        self.assertTrue(self.backend.requeue_permanent_failure(
            self.key, "ascii codec can't encode character"))
        job = self.client.data[self.path]
        self.assertEqual(self.client.transactions, before_transactions + 1)
        self.assertEqual(job["status"], "retryable")
        self.assertLessEqual(job["next_retry_at"], self.now)
        self.assertIsNone(job["lease_owner"]); self.assertIsNone(job["lease_expires_at"])
        for field in ("source", "file_id", "name", "error_message", "completed_chunks",
                      "partial_uploads", "final_ids", "initial_notification_claimed_at",
                      "estimate_notification_claimed_at", "retry_count"):
            self.assertEqual(job[field], self.original[field])
        self.assertIn(f"{self.path}/chunks/000000", self.client.data)

    def test_wrong_fingerprint_refuses_without_mutation(self):
        before = deepcopy(self.client.data)
        with self.assertRaises(RecoveryRefusedError):
            self.backend.requeue_permanent_failure(self.key, "different incident")
        self.assertEqual(self.client.data, before)

    def test_non_permanent_states_and_second_invocation_are_safe(self):
        for status in ("completed", "in_progress", "retryable"):
            with self.subTest(status=status):
                self.client.data[self.path] = {**deepcopy(self.original), "status": status}
                before = deepcopy(self.client.data)
                with self.assertRaises(RecoveryRefusedError):
                    self.backend.requeue_permanent_failure(
                        self.key, "ascii codec can't encode character")
                self.assertEqual(self.client.data, before)
        self.client.data[self.path] = deepcopy(self.original)
        self.backend.requeue_permanent_failure(self.key, "ASCII CODEC CAN'T ENCODE CHARACTER")
        after = deepcopy(self.client.data)
        with self.assertRaises(RecoveryRefusedError):
            self.backend.requeue_permanent_failure(self.key, "ascii codec can't encode character")
        self.assertEqual(self.client.data, after)

    def test_required_guards(self):
        for source_key, fingerprint in (("", "ascii"), (self.key, "")):
            with self.assertRaises(ValueError):
                self.backend.requeue_permanent_failure(source_key, fingerprint)

    def test_real_firestore_transaction_path_preserves_recovery_refusal(self):
        import google.cloud

        transaction_type = type("Transaction", (), {"__module__": "google.cloud.firestore_v1.transaction"})
        client = MagicMock()
        transaction = transaction_type()
        client.transaction.return_value = transaction
        backend = FirestoreStateBackend("test", client=client)
        expected = RecoveryRefusedError("intentional refusal")

        def transactional(callback):
            return lambda tx: callback(tx)

        firestore = SimpleNamespace(transactional=transactional)
        with patch.object(google.cloud, "firestore", firestore, create=True):
            with self.assertRaises(RecoveryRefusedError) as raised:
                backend._transaction(lambda _tx: (_ for _ in ()).throw(expected))
        self.assertIs(raised.exception, expected)
        self.assertIs(type(raised.exception), RecoveryRefusedError)


class RequeueCliTests(unittest.TestCase):
    ARGS = ["a" * 64, "--project", "test-project", "--expected-error",
            "ascii codec can't encode character"]

    def run_refusal(self, message):
        backend = MagicMock()
        backend.requeue_permanent_failure.side_effect = RecoveryRefusedError(message)
        output = StringIO()
        with patch.object(requeue_firestore_job, "FirestoreStateBackend", return_value=backend), \
             redirect_stdout(output):
            code = requeue_firestore_job.main(self.ARGS)
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), f"REFUSED: {message}\n")
        self.assertNotIn("Traceback", output.getvalue())

    def test_wrong_fingerprint_is_safe_refusal(self):
        self.run_refusal("Expected error fingerprint did not match preserved evidence")

    def test_already_requeued_job_is_safe_refusal(self):
        self.run_refusal("Only failed_permanent jobs may be requeued (found 'retryable')")

    def test_operational_firestore_failure_is_not_mislabeled_as_refusal(self):
        backend = MagicMock()
        failure = StatePersistenceError("Firestore transaction unavailable")
        backend.requeue_permanent_failure.side_effect = failure
        output = StringIO()
        with patch.object(requeue_firestore_job, "FirestoreStateBackend", return_value=backend), \
             redirect_stdout(output), self.assertRaises(StatePersistenceError) as raised:
            requeue_firestore_job.main(self.ARGS)
        self.assertIs(raised.exception, failure)
        self.assertNotIn("REFUSED:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
