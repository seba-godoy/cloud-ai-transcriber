import threading
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from state_backend import (FirestoreStateBackend, LeaseLostError, LocalStateBackend, StateConflictError,
                           chunk_document_id, deserialize_job, serialize_job)
from utils import make_source_key


class Snapshot:
    def __init__(self, data): self._data = deepcopy(data); self.exists = data is not None
    def to_dict(self): return deepcopy(self._data)


class Document:
    def __init__(self, client, path): self.client = client; self.path = path
    def get(self, transaction=None): return Snapshot(self.client.data.get(self.path))
    def set(self, data, merge=False): self.client.write(self.path, data, merge)
    def collection(self, name): return Collection(self.client, self.path + "/" + name)


class Collection:
    def __init__(self, client, path): self.client = client; self.path = path
    def document(self, name): return Document(self.client, self.path + "/" + name)
    def stream(self):
        prefix = self.path + "/"
        return [Snapshot(v) for k, v in self.client.data.items()
                if k.startswith(prefix) and "/" not in k[len(prefix):]]


class Transaction:
    def __init__(self, client): self.client = client; self.client.lock.acquire()
    def set(self, ref, data, merge=False): self.client.write(ref.path, data, merge)
    def update(self, ref, data): self.client.write(ref.path, data, True, replace_maps=True)
    def close(self):
        try: self.client.lock.release()
        except RuntimeError: pass
    def __del__(self):
        self.close()


class FakeClient:
    def __init__(self): self.data = {}; self.lock = threading.RLock(); self.transactions = 0
    def collection(self, name): return Collection(self, name)
    def transaction(self): self.transactions += 1; return Transaction(self)
    def write(self, path, data, merge, replace_maps=False):
        with self.lock:
            old = deepcopy(self.data.get(path, {})) if merge else {}
            for key, value in deepcopy(data).items():
                if merge and not replace_maps and isinstance(value, dict) and isinstance(old.get(key), dict): old[key].update(value)
                else: old[key] = value
            self.data[path] = old


class FirestoreBackendTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
        self.client = FakeClient()
        self.backend = FirestoreStateBackend("test", client=self.client, clock_fn=lambda: self.now,
                                             owner_id="worker", lease_ttl_seconds=60)
        self.key = make_source_key("drive", "file-1")
        self.backend.bind_source("drive", "file-1", "audio.mp3")
        self.assertTrue(self.backend.acquire_lease(self.key, "worker", 60))

    def save_chunk(self, index, transcript, model="m", words=1):
        return self.backend.save_chunk(self.key, index, transcript, model, words, owner_id="worker")

    def backend_for(self, owner):
        backend = FirestoreStateBackend("test", client=self.client, clock_fn=lambda: self.now,
                                        owner_id=owner, lease_ttl_seconds=60)
        backend._source_by_id["file-1"] = "drive"
        return backend

    def test_global_cycle_lease_acquire_exclusion_renew_and_release(self):
        other = self.backend_for("other")
        self.assertTrue(self.backend.acquire_global_cycle_lease())
        self.assertFalse(other.acquire_global_cycle_lease())
        self.assertTrue(self.backend.renew_global_cycle_lease())
        self.assertFalse(other.renew_global_cycle_lease())
        self.assertFalse(other.release_global_cycle_lease())
        self.assertTrue(self.backend.release_global_cycle_lease())

    def test_global_cycle_expired_takeover_fences_stale_owner(self):
        other = self.backend_for("other")
        self.assertTrue(self.backend.acquire_global_cycle_lease())
        self.now += timedelta(seconds=61)
        self.assertTrue(other.acquire_global_cycle_lease())
        self.assertFalse(self.backend.renew_global_cycle_lease())
        self.assertFalse(self.backend.release_global_cycle_lease())
        self.assertTrue(other.renew_global_cycle_lease())
        self.assertTrue(other.release_global_cycle_lease())

    def test_job_roundtrip_preserves_aware_timestamps_and_metadata(self):
        data = {"created_at": self.now.isoformat(), "retry_count": 3, "network_retry_count": 2,
                "partial_uploads": {"docx": "d"}, "final_ids": {"pdf": "p"}}
        result = deserialize_job(serialize_job(data))
        self.assertIsNotNone(result["created_at"].tzinfo); self.assertEqual(result["retry_count"], 3)
        self.assertEqual(result["partial_uploads"], {"docx": "d"}); self.assertEqual(result["final_ids"], {"pdf": "p"})

    def test_naive_timestamp_is_rejected(self):
        with self.assertRaises(ValueError): serialize_job({"updated_at": datetime(2026, 1, 1)})

    def test_chunk_id_is_deterministic(self): self.assertEqual(chunk_document_id(30), "000030")
    def test_chunk_save_read_and_numeric_order(self):
        self.save_chunk(0, "zero"); self.save_chunk(1, "one")
        self.assertEqual([x["chunk_index"] for x in self.backend.list_completed_chunks(self.key)], [0, 1])
    def test_identical_chunk_save_is_idempotent(self):
        self.save_chunk(0, "hello"); self.save_chunk(0, "hello")
        self.assertEqual(len(self.backend.list_completed_chunks(self.key)), 1)
    def test_incompatible_completed_chunk_conflicts(self):
        self.save_chunk(0, "hello")
        with self.assertRaises(StateConflictError): self.save_chunk(0, "different")

    def test_chunk_gap_is_rejected(self):
        with self.assertRaises(StateConflictError): self.save_chunk(1, "gap")

    def test_stale_worker_cannot_save_after_takeover(self):
        self.now += timedelta(seconds=61)
        self.assertTrue(self.backend.acquire_lease(self.key, "worker-b", 60))
        with self.assertRaises(LeaseLostError):
            self.backend.save_chunk(self.key, 0, "old", "m", 1, owner_id="worker")
        self.backend.save_chunk(self.key, 0, "new", "m", 1, owner_id="worker-b")
        self.assertEqual(self.backend.list_completed_chunks(self.key)[0]["transcript"], "new")

    def test_partial_upload_maps_preserve_siblings(self):
        self.backend.save_partial_upload("file-1", "docx", "d")
        self.backend.save_partial_upload("file-1", "pdf", "p")
        self.assertEqual(self.backend.get_partial_uploads("file-1"), {"docx": "d", "pdf": "p"})
        self.backend.remove_partial_upload("file-1", "docx")
        self.assertEqual(self.backend.get_partial_uploads("file-1"), {"pdf": "p"})

    def test_youtube_state_updates_preserve_transcript_metadata(self):
        video = "video-1"; self.backend.bind_source("youtube", video, "title")
        self.assertTrue(self.backend.acquire_lease(make_source_key("youtube", video), "worker", 60))
        self.backend.save_youtube_transcript(video, "title", "text", "d", "p")
        self.backend.save_youtube_summary_pending_delivery(video, "title", "summary")
        self.backend.mark_youtube_summary_failed(video, "title", "network", permanent=False)
        state = self.backend.get_youtube_state(video)
        self.assertEqual((state["transcript_status"], state["docx_id"], state["pdf_id"]), ("completed", "d", "p"))
        self.assertEqual(state["summary_status"], "failed_retryable")
        self.backend.mark_youtube_summary_completed(video, "title", "summary")
        state = self.backend.get_youtube_state(video)
        self.assertEqual((state["transcript_status"], state["docx_id"], state["pdf_id"]), ("completed", "d", "p"))

    def test_stale_partial_upload_is_fenced_after_takeover(self):
        self.backend.save_partial_upload("file-1", "docx", "d")
        self.now += timedelta(seconds=61)
        worker_b = self.backend_for("worker-b")
        self.assertTrue(worker_b.acquire_lease(self.key, "worker-b", 60))
        with self.assertRaises(LeaseLostError):
            self.backend.save_partial_upload("file-1", "pdf", "stale-p")
        self.assertEqual(worker_b.get_partial_uploads("file-1"), {"docx": "d"})
        worker_b.save_partial_upload("file-1", "pdf", "p")
        self.assertEqual(worker_b.get_partial_uploads("file-1"), {"docx": "d", "pdf": "p"})

    def test_stale_mark_processed_is_fenced_after_takeover(self):
        self.now += timedelta(seconds=61)
        worker_b = self.backend_for("worker-b")
        self.assertTrue(worker_b.acquire_lease(self.key, "worker-b", 60))
        with self.assertRaises(LeaseLostError):
            self.backend.mark_processed("file-1", "audio", "m", {"docx": "stale"})
        self.assertNotEqual(worker_b.get_job(self.key)["status"], "completed")
        worker_b.mark_processed("file-1", "audio", "m", {"docx": "d"})
        self.assertEqual(worker_b.get_job(self.key)["status"], "completed")

    def test_stale_youtube_state_is_fenced_after_takeover(self):
        video = "video-stale"; key = self.backend.bind_source("youtube", video, "title")
        self.assertTrue(self.backend.acquire_lease(key, "worker", 60))
        self.backend.save_youtube_transcript(video, "title", "text", "d", "p")
        self.now += timedelta(seconds=61)
        worker_b = self.backend_for("worker-b"); worker_b._source_by_id[video] = "youtube"
        self.assertTrue(worker_b.acquire_lease(key, "worker-b", 60))
        with self.assertRaises(LeaseLostError):
            self.backend.mark_youtube_summary_completed(video, "title", "stale")
        self.assertNotEqual(worker_b.get_youtube_state(video)["summary_status"], "completed")
        worker_b.mark_youtube_summary_completed(video, "title", "summary")
        state = worker_b.get_youtube_state(video)
        self.assertEqual((state["transcript_status"], state["docx_id"], state["pdf_id"], state["summary_status"]),
                         ("completed", "d", "p", "completed"))

    def test_lease_lifecycle_and_ownership(self):
        self.assertTrue(self.backend.release_lease(self.key, "worker"))
        self.assertTrue(self.backend.acquire_lease(self.key, "a", 60))
        self.assertFalse(self.backend.acquire_lease(self.key, "b", 60))
        self.assertTrue(self.backend.renew_lease(self.key, "a", 60))
        self.assertFalse(self.backend.renew_lease(self.key, "b", 60))
        self.assertFalse(self.backend.release_lease(self.key, "b"))
        self.assertTrue(self.backend.release_lease(self.key, "a"))
        self.assertTrue(self.backend.acquire_lease(self.key, "b", 60))

    def test_expired_lease_can_be_taken(self):
        self.assertTrue(self.backend.release_lease(self.key, "worker"))
        self.assertTrue(self.backend.acquire_lease(self.key, "a", 10)); self.now += timedelta(seconds=11)
        self.assertTrue(self.backend.acquire_lease(self.key, "b", 10))

    def test_completed_job_blocks_lease(self):
        self.backend.mark_processed("file-1", "audio", "m", {"docx": "d"})
        self.assertFalse(self.backend.acquire_lease(self.key, "a", 10))
        self.assertEqual(self.backend.get_job(self.key)["final_ids"], {"docx": "d"})

    def test_concurrent_acquire_has_one_winner(self):
        self.assertTrue(self.backend.release_lease(self.key, "worker"))
        barrier = threading.Barrier(2); answers = []
        def run(owner): barrier.wait(); answers.append(self.backend.acquire_lease(self.key, owner, 30))
        threads = [threading.Thread(target=run, args=(x,)) for x in ("a", "b")]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(sorted(answers), [False, True])

    def test_completed_job_cannot_be_changed_to_retry(self):
        self.backend.mark_processed("file-1", "audio", "m")
        with self.assertRaises(StateConflictError): self.backend.mark_retryable("file-1", "audio", "drive", "oops")

    def test_retry_backoff_and_metadata_match_local_rules(self):
        self.backend._job(self.key).set({"partial_uploads": {"docx": "d"},
            "final_ids": {"pdf": "p"}, "migrated_from": "legacy"}, merge=True)
        with patch("config.Config.FILE_RETRY_BASE_SECONDS", 10), \
             patch("config.Config.FILE_RETRY_MAX_SECONDS", 25), \
             patch("config.Config.MAX_FILE_RETRIES", 4):
            self.backend.mark_retryable("file-1", "audio", "drive", "first")
            self.backend.mark_retryable("file-1", "audio", "drive", "second")
        job = self.backend.get_job(self.key)
        self.assertEqual(job["retry_count"], 2)
        self.assertEqual(job["next_retry_at"], self.now + timedelta(seconds=20))
        self.assertEqual((job["partial_uploads"], job["final_ids"], job["migrated_from"]),
                         ({"docx": "d"}, {"pdf": "p"}, "legacy"))

    def test_network_retry_is_indefinite_when_maximum_zero_and_bounded(self):
        with patch("config.Config.MAX_NETWORK_FILE_RETRIES", 0), \
             patch("config.Config.NETWORK_FILE_RETRY_BASE_SECONDS", 10), \
             patch("config.Config.NETWORK_FILE_RETRY_MAX_SECONDS", 15):
            self.backend.mark_retryable("file-1", "audio", "drive", "dns", is_network_error=True)
            self.backend.mark_retryable("file-1", "audio", "drive", "dns", is_network_error=True)
        job = self.backend.get_job(self.key)
        self.assertEqual((job["status"], job["network_retry_count"]), ("retryable", 2))
        self.assertEqual(job["next_retry_at"], self.now + timedelta(seconds=15))

    def test_retry_limit_becomes_permanent(self):
        before = self.client.transactions
        with patch("config.Config.MAX_FILE_RETRIES", 1):
            self.backend.mark_retryable("file-1", "audio", "drive", "bad")
        self.assertEqual(self.backend.get_job(self.key)["status"], "failed_permanent")
        self.assertEqual(self.client.transactions, before + 1)

    def test_stale_retryable_is_fenced_after_takeover(self):
        self.now += timedelta(seconds=61); worker_b = self.backend_for("worker-b")
        self.assertTrue(worker_b.acquire_lease(self.key, "worker-b", 60))
        with self.assertRaises(LeaseLostError):
            self.backend.mark_retryable("file-1", "audio", "drive", "stale")
        self.assertNotEqual(worker_b.get_job(self.key)["status"], "retryable")
        worker_b.mark_retryable("file-1", "audio", "drive", "current")
        self.assertEqual(worker_b.get_job(self.key)["status"], "retryable")

    def test_stale_permanent_failure_is_fenced_after_takeover(self):
        self.now += timedelta(seconds=61); worker_b = self.backend_for("worker-b")
        self.assertTrue(worker_b.acquire_lease(self.key, "worker-b", 60))
        with self.assertRaises(LeaseLostError):
            self.backend.mark_permanent_failed("file-1", "audio", "stale", "drive")
        self.assertNotEqual(worker_b.get_job(self.key)["status"], "failed_permanent")
        worker_b.mark_permanent_failed("file-1", "audio", "current", "drive")
        self.assertEqual(worker_b.get_job(self.key)["status"], "failed_permanent")

    def test_stale_worker_at_retry_limit_cannot_mark_permanent(self):
        self.backend._job(self.key).set({"retry_count": 4}, merge=True)
        self.now += timedelta(seconds=61); worker_b = self.backend_for("worker-b")
        self.assertTrue(worker_b.acquire_lease(self.key, "worker-b", 60))
        with patch("config.Config.MAX_FILE_RETRIES", 5), self.assertRaises(LeaseLostError):
            self.backend.mark_retryable("file-1", "audio", "drive", "stale-limit")
        self.assertNotEqual(worker_b.get_job(self.key)["status"], "failed_permanent")

    def test_stale_network_retry_is_fenced_after_takeover(self):
        self.now += timedelta(seconds=61); worker_b = self.backend_for("worker-b")
        self.assertTrue(worker_b.acquire_lease(self.key, "worker-b", 60))
        with self.assertRaises(LeaseLostError):
            self.backend.mark_retryable("file-1", "audio", "drive", "dns", is_network_error=True)
        self.assertEqual(worker_b.get_job(self.key)["network_retry_count"], 0)
        worker_b.mark_retryable("file-1", "audio", "drive", "dns", is_network_error=True)
        self.assertEqual(worker_b.get_job(self.key)["network_retry_count"], 1)


class LocalBackendTests(unittest.TestCase):
    def test_local_backend_needs_no_firestore_project_or_client(self):
        with TemporaryDirectory() as tmp, patch("config.Config.FIRESTORE_PROJECT_ID", ""):
            store = LocalStateBackend(tmp + "/state.json")
            store.mark_processed("x", "x.mp3", "m")
            self.assertTrue(store.is_processed("x"))


if __name__ == "__main__": unittest.main()
