import os
import unittest
import uuid
import threading
from datetime import datetime, timedelta, timezone


@unittest.skipUnless(os.getenv("FIRESTORE_EMULATOR_HOST"), "Firestore Emulator is not running")
class FirestoreEmulatorIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from google.cloud import firestore
        from state_backend import FirestoreStateBackend
        cls.now = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
        cls.client = firestore.Client(project="phase2-emulator-test", database="(default)")
        cls.backend_class = FirestoreStateBackend

    def setUp(self):
        self.backend = self.backend_class("phase2-emulator-test", client=self.client,
            clock_fn=lambda: self.now, owner_id="worker-a", lease_ttl_seconds=900)
        self.backend.collection_name = "test_jobs_" + uuid.uuid4().hex
        self.key = self.backend.bind_source("drive", "file-1", "audio.mp3")

    def tearDown(self):
        for chunk in self.backend._job(self.key).collection("chunks").stream():
            chunk.reference.delete()
        self.backend._job(self.key).delete()
        self.backend._global_cycle_lock().delete()

    def test_global_cycle_atomic_race_takeover_and_stale_fencing(self):
        owner_b = self.backend_class("phase2-emulator-test", client=self.client,
            clock_fn=lambda: self.now, owner_id="worker-b", lease_ttl_seconds=900)
        barrier = threading.Barrier(2)
        results = []
        def acquire(backend):
            barrier.wait()
            results.append((backend.owner_id, backend.acquire_global_cycle_lease()))
        threads = [threading.Thread(target=acquire, args=(worker,))
                   for worker in (self.backend, owner_b)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(won for _, won in results), 1)
        winner_id = next(owner for owner, won in results if won)
        winner = self.backend if winner_id == "worker-a" else owner_b
        loser = owner_b if winner is self.backend else self.backend
        self.assertFalse(loser.acquire_global_cycle_lease())
        self.now += timedelta(seconds=901)
        self.assertTrue(loser.acquire_global_cycle_lease())
        self.assertFalse(winner.renew_global_cycle_lease())
        self.assertFalse(winner.release_global_cycle_lease())
        self.assertTrue(loser.release_global_cycle_lease())

    def test_job_roundtrip(self):
        job = self.backend.get_job(self.key)
        self.assertEqual((job["source"], job["file_id"]), ("drive", "file-1"))
        self.assertIsNotNone(job["created_at"].tzinfo)

    def test_notification_claim_is_atomic_and_durable(self):
        self.assertTrue(self.backend.claim_notification(self.key, "initial"))
        self.assertFalse(self.backend.claim_notification(self.key, "initial"))

    def test_concurrent_notification_claim_has_one_winner(self):
        owner_b = self.backend_class("phase2-emulator-test", client=self.client,
            clock_fn=lambda: self.now, owner_id="worker-b", lease_ttl_seconds=900)
        owner_b.collection_name = self.backend.collection_name
        barrier = threading.Barrier(2); results = []
        def claim(backend):
            barrier.wait(); results.append(backend.claim_notification(self.key, "estimate"))
        threads = [threading.Thread(target=claim, args=(backend,))
                   for backend in (self.backend, owner_b)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(results), 1)

    def test_nested_partial_uploads(self):
        self.assertTrue(self.backend.acquire_lease(self.key, "worker-a", 60))
        self.backend.save_partial_upload("file-1", "docx", "d")
        self.backend.save_partial_upload("file-1", "pdf", "p")
        self.backend.remove_partial_upload("file-1", "docx")
        self.assertEqual(self.backend.get_partial_uploads("file-1"), {"pdf": "p"})

    def test_nested_youtube_state(self):
        video = "v1"; self.key = self.backend.bind_source("youtube", video, "title")
        self.assertTrue(self.backend.acquire_lease(self.key, "worker-a", 60))
        self.backend.save_youtube_transcript(video, "title", "text", "d", "p")
        self.backend.save_youtube_summary_pending_delivery(video, "title", "summary")
        state = self.backend.get_youtube_state(video)
        self.assertEqual((state["docx_id"], state["pdf_id"], state["summary_status"]),
                         ("d", "p", "pending_delivery"))

    def test_transactional_lease_contention_expiry_and_completed(self):
        self.assertTrue(self.backend.acquire_lease(self.key, "a", 60))
        self.assertFalse(self.backend.acquire_lease(self.key, "b", 60))
        self.now += timedelta(seconds=61)
        self.assertTrue(self.backend.acquire_lease(self.key, "b", 60))
        worker_b = self.backend_class("phase2-emulator-test", client=self.client,
            clock_fn=lambda: self.now, owner_id="b", lease_ttl_seconds=900)
        worker_b.collection_name = self.backend.collection_name
        worker_b._source_by_id["file-1"] = "drive"
        worker_b.mark_processed("file-1", "audio", "m")
        self.assertFalse(self.backend.acquire_lease(self.key, "c", 60))

    def test_transactional_chunk_idempotency_conflict_gap_and_stale_owner(self):
        from state_backend import LeaseLostError, StateConflictError
        self.assertTrue(self.backend.acquire_lease(self.key, "a", 60))
        self.backend.save_chunk(self.key, 0, "zero", "m", 1, owner_id="a")
        self.backend.save_chunk(self.key, 0, "zero", "m", 1, owner_id="a")
        with self.assertRaises(StateConflictError):
            self.backend.save_chunk(self.key, 0, "other", "m", 1, owner_id="a")
        with self.assertRaises(StateConflictError):
            self.backend.save_chunk(self.key, 2, "gap", "m", 1, owner_id="a")
        self.now += timedelta(seconds=61)
        self.assertTrue(self.backend.acquire_lease(self.key, "b", 60))
        with self.assertRaises(LeaseLostError):
            self.backend.save_chunk(self.key, 1, "old", "m", 1, owner_id="a")
        self.backend.save_chunk(self.key, 1, "one", "m", 1, owner_id="b")
        self.assertEqual(self.backend.get_job(self.key)["completed_chunks"], 2)

    def test_stale_failure_transition_is_fenced_after_takeover(self):
        from state_backend import FirestoreStateBackend, LeaseLostError
        self.assertTrue(self.backend.acquire_lease(self.key, "worker-a", 60))
        self.now += timedelta(seconds=61)
        worker_b = FirestoreStateBackend("phase2-emulator-test", client=self.client,
            clock_fn=lambda: self.now, owner_id="worker-b", lease_ttl_seconds=900)
        worker_b.collection_name = self.backend.collection_name
        worker_b._source_by_id["file-1"] = "drive"
        self.assertTrue(worker_b.acquire_lease(self.key, "worker-b", 60))
        with self.assertRaises(LeaseLostError):
            self.backend.mark_retryable("file-1", "audio.mp3", "drive", "stale")
        self.assertNotEqual(worker_b.get_job(self.key)["status"], "retryable")
        worker_b.mark_retryable("file-1", "audio.mp3", "drive", "current")
        self.assertEqual(worker_b.get_job(self.key)["status"], "retryable")


if __name__ == "__main__":
    unittest.main()
