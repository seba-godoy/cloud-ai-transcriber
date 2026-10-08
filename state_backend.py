"""Durable state boundary for the legacy filesystem and future Cloud Run workers."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re
from typing import Callable, Protocol, runtime_checkable

from config import Config
from state_store import StateStore
from utils import StatePersistenceError, make_source_key, redact_secrets


class StateConflictError(StatePersistenceError):
    """A completed chunk was about to be replaced by incompatible data."""


class LeaseLostError(StatePersistenceError):
    """The worker no longer owns a live lease and must stop writing."""


class RecoveryRefusedError(StatePersistenceError):
    """An explicit operator recovery did not match its safety guards."""


@runtime_checkable
class StateBackend(Protocol):
    backend_name: str
    def bind_source(self, source: str, source_id: str, name: str = "") -> str: ...
    def is_processed(self, file_id: str) -> bool: ...
    def is_permanently_failed(self, file_id: str) -> bool: ...
    def is_retry_due(self, file_id: str) -> bool: ...
    def get_partial_uploads(self, file_id: str) -> dict: ...
    def save_partial_upload(self, file_id: str, doc_type: str, drive_id: str) -> None: ...
    def remove_partial_upload(self, file_id: str, doc_type: str) -> None: ...
    def mark_processed(self, file_id: str, name: str, model_used: str, final_ids: dict | None = None) -> None: ...
    def mark_retryable(self, file_id: str, name: str, source: str, error_message: str,
                       is_network_error: bool = False) -> None: ...
    def mark_permanent_failed(self, file_id: str, name: str, error_message: str,
                              source: str = "drive") -> None: ...
    def get_youtube_state(self, video_id: str) -> dict: ...
    def save_youtube_transcript(self, video_id: str, title: str, transcript_text: str,
                                docx_id: str | None = None, pdf_id: str | None = None) -> None: ...
    def get_youtube_transcript(self, video_id: str) -> str | None: ...
    def get_youtube_summary(self, video_id: str) -> str | None: ...
    def mark_youtube_summary_completed(self, video_id: str, title: str,
                                       summary_text: str | None = None) -> None: ...
    def save_youtube_summary_pending_delivery(self, video_id: str, title: str,
                                              summary_text: str) -> None: ...
    def mark_youtube_summary_failed(self, video_id: str, title: str, error_msg: str,
                                    permanent: bool = False) -> None: ...
    def get_telegram_offset(self) -> int: ...
    def set_telegram_offset(self, offset: int) -> None: ...
    def save_chunk(self, source_key: str, chunk_index: int, transcript: str,
                   model_used: str, word_count: int, owner_id: str, **metadata) -> None: ...
    def list_completed_chunks(self, source_key: str) -> list[dict]: ...
    def acquire_lease(self, source_key: str, owner_id: str, ttl_seconds: int) -> bool: ...
    def renew_lease(self, source_key: str, owner_id: str, ttl_seconds: int) -> bool: ...
    def release_lease(self, source_key: str, owner_id: str) -> bool: ...
    def claim_notification(self, source_key: str, notification: str) -> bool: ...


class LocalStateBackend(StateStore):
    """Compatibility backend: the existing StateStore remains authoritative."""

    backend_name = "local"

    def claim_notification(self, source_key: str, notification: str) -> bool:
        return True


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def chunk_document_id(chunk_index: int) -> str:
    if chunk_index < 0:
        raise ValueError("chunk_index must be non-negative")
    return f"{chunk_index:06d}"


def serialize_job(data: dict) -> dict:
    result = deepcopy(data)
    for key in ("created_at", "updated_at", "first_error_at", "last_attempt_at",
                "next_retry_at", "lease_expires_at"):
        value = result.get(key)
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError(f"{key} must be timezone-aware")
            result[key] = value.astimezone(timezone.utc)
    return result


def deserialize_job(data: dict) -> dict:
    return serialize_job(data)


class FirestoreStateBackend:
    """Firestore implementation; client construction is lazy and injectable for tests."""

    backend_name = "firestore"
    collection_name = "transcription_jobs"
    runtime_locks_collection = "runtime_locks"
    global_cycle_lock_id = "one_shot_cycle"

    def __init__(self, project_id: str | None = None, database_id: str | None = None,
                 client=None, clock_fn: Callable[[], datetime] = utc_now,
                 owner_id: str | None = None, lease_ttl_seconds: int | None = None):
        self.project_id = project_id or Config.FIRESTORE_PROJECT_ID
        self.database_id = database_id or Config.FIRESTORE_DATABASE_ID
        self._client = client
        self.clock_fn = clock_fn
        self.owner_id = owner_id or Config.WORKER_ID
        self.lease_ttl_seconds = lease_ttl_seconds or Config.FIRESTORE_LEASE_TTL_SECONDS
        self._source_by_id: dict[str, str] = {}
        self.global_cycle_lease_active = False

    @property
    def client(self):
        if self._client is None:
            if not self.project_id:
                raise StatePersistenceError("FIRESTORE_PROJECT_ID is required for Firestore state")
            from google.cloud import firestore
            self._client = firestore.Client(project=self.project_id, database=self.database_id)
        return self._client

    def _job(self, source_key: str):
        return self.client.collection(self.collection_name).document(source_key)

    def _global_cycle_lock(self):
        return self.client.collection(self.runtime_locks_collection).document(
            self.global_cycle_lock_id)

    def acquire_global_cycle_lease(self) -> bool:
        """Acquire the application-wide one-shot cycle lease transactionally."""
        now = self.clock_fn(); ref = self._global_cycle_lock()
        def operation(tx):
            snap = ref.get(transaction=tx); data = snap.to_dict() if snap.exists else {}
            expires = data.get("lease_expires_at")
            if expires and expires > now and data.get("lease_owner") != self.owner_id:
                return False
            tx.set(ref, {"lease_owner": self.owner_id,
                         "lease_expires_at": now + timedelta(seconds=self.lease_ttl_seconds),
                         "updated_at": now}, merge=True)
            return True
        acquired = self._transaction(operation)
        self.global_cycle_lease_active = acquired
        return acquired

    def renew_global_cycle_lease(self) -> bool:
        """Renew only while this live owner still holds the global fence."""
        now = self.clock_fn(); ref = self._global_cycle_lock()
        def operation(tx):
            snap = ref.get(transaction=tx); data = snap.to_dict() if snap.exists else {}
            expires = data.get("lease_expires_at")
            if data.get("lease_owner") != self.owner_id or not expires or expires <= now:
                return False
            tx.set(ref, {"lease_expires_at": now + timedelta(seconds=self.lease_ttl_seconds),
                         "updated_at": now}, merge=True)
            return True
        renewed = self._transaction(operation)
        if not renewed:
            self.global_cycle_lease_active = False
        return renewed

    def renew_global_cycle_lease_or_raise(self) -> None:
        """Fence progress when an acquired global cycle lease has been lost."""
        if self.global_cycle_lease_active and not self.renew_global_cycle_lease():
            raise LeaseLostError("Global one-shot cycle lease was lost")

    def release_global_cycle_lease(self) -> bool:
        """Release only a currently live lease owned by this worker."""
        now = self.clock_fn(); ref = self._global_cycle_lock()
        def operation(tx):
            snap = ref.get(transaction=tx); data = snap.to_dict() if snap.exists else {}
            expires = data.get("lease_expires_at")
            if data.get("lease_owner") != self.owner_id or not expires or expires <= now:
                return False
            tx.set(ref, {"lease_owner": None, "lease_expires_at": None,
                         "updated_at": now}, merge=True)
            return True
        released = self._transaction(operation)
        self.global_cycle_lease_active = False
        return released

    def bind_source(self, source: str, source_id: str, name: str = "") -> str:
        key = make_source_key(source, source_id)
        self._source_by_id[source_id] = source
        identity_field = "video_id" if source == "youtube" else "file_id"
        self.ensure_job(key, source=source, source_id=source_id, name=name, **{identity_field: source_id})
        return key

    def _key(self, source_id: str, source: str | None = None) -> str:
        resolved = source or self._source_by_id.get(source_id)
        if not resolved:
            raise ValueError(f"Source must be bound before accessing {source_id}")
        return make_source_key(resolved, source_id)

    def ensure_job(self, source_key: str, **fields) -> None:
        now = self.clock_fn()
        ref = self._job(source_key)
        snap = ref.get()
        if snap.exists:
            ref.set({"updated_at": now, **fields}, merge=True)
        else:
            ref.set({"source_key": source_key, "status": "in_progress",
                     "created_at": now, "updated_at": now, "retry_count": 0,
                     "network_retry_count": 0, "partial_uploads": {},
                     "final_ids": {}, "completed_chunks": 0, **fields})

    def get_job(self, source_key: str) -> dict | None:
        snap = self._job(source_key).get()
        return deserialize_job(snap.to_dict()) if snap.exists else None

    def requeue_permanent_failure(self, source_key: str, expected_error: str,
                                  reason: str = "operator_unicode_filename_recovery") -> bool:
        """Transactionally requeue exactly one guarded permanent failure.

        Only lease/retry metadata is updated. Chunk documents, identities, output
        IDs, notification claims, and original error fields remain untouched.
        """
        if not source_key or not expected_error:
            raise ValueError("source_key and expected_error are required")
        now = self.clock_fn(); ref = self._job(source_key)
        def operation(tx):
            snap = ref.get(transaction=tx)
            if not snap.exists:
                raise RecoveryRefusedError("Job does not exist")
            data = snap.to_dict()
            if data.get("status") != "failed_permanent":
                raise RecoveryRefusedError(
                    f"Only failed_permanent jobs may be requeued (found {data.get('status')!r})")
            evidence = " ".join(str(data.get(k, "")) for k in ("error_message", "last_error"))
            # Exception renderings may quote the codec name (``'ascii'``). Match
            # words rather than punctuation while retaining a narrow fingerprint.
            normalize = lambda value: " ".join(
                re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())
            if normalize(expected_error) not in normalize(evidence):
                raise RecoveryRefusedError("Expected error fingerprint did not match preserved evidence")
            tx.update(ref, {"status": "retryable", "next_retry_at": now,
                            "lease_owner": None, "lease_expires_at": None,
                            "requeued_at": now, "requeue_reason": reason,
                            "updated_at": now})
            return True
        return self._transaction(operation)

    def claim_notification(self, source_key: str, notification: str) -> bool:
        """Atomically claim a non-critical notification before sending it."""
        if notification not in ("initial", "estimate"):
            raise ValueError("Invalid notification name")
        now = self.clock_fn(); ref = self._job(source_key)
        field = f"{notification}_notification_claimed_at"
        def operation(tx):
            snap = ref.get(transaction=tx); data = snap.to_dict() if snap.exists else {}
            if data.get(field) is not None:
                return False
            tx.set(ref, {field: now, "updated_at": now}, merge=True)
            return True
        return self._transaction(operation)

    def save_chunk(self, source_key: str, chunk_index: int, transcript: str,
                   model_used: str, word_count: int, owner_id: str, **metadata) -> None:
        now = self.clock_fn()
        ref = self._job(source_key).collection("chunks").document(chunk_document_id(chunk_index))
        candidate = {"chunk_index": chunk_index, "status": "completed", "transcript": transcript,
                     "model_used": model_used, "word_count": word_count, **metadata}
        job_ref = self._job(source_key)
        def operation(tx):
            job_snap = job_ref.get(transaction=tx)
            job = job_snap.to_dict() if job_snap.exists else {}
            expires = job.get("lease_expires_at")
            if (job.get("status") == "completed" or job.get("lease_owner") != owner_id
                    or not expires or expires <= now):
                raise LeaseLostError(f"Worker {owner_id} does not own a live lease for {source_key}")
            existing = ref.get(transaction=tx)
            if existing.exists:
                old = existing.to_dict()
                comparable = {k: old.get(k) for k in candidate}
                if comparable != candidate:
                    raise StateConflictError(f"Completed chunk {chunk_index} has incompatible content")
                return
            completed = job.get("completed_chunks", 0)
            if chunk_index != completed:
                raise StateConflictError(
                    f"Chunk gap: expected index {completed}, received {chunk_index}")
            tx.set(ref, {**candidate, "created_at": now, "updated_at": now})
            tx.set(job_ref, {"updated_at": now, "completed_chunks": chunk_index + 1}, merge=True)
        try:
            self._transaction(operation)
        except (StateConflictError, LeaseLostError):
            raise
        except Exception as exc:
            raise StatePersistenceError(f"Firestore chunk persistence failed: {redact_secrets(str(exc))}") from exc

    def list_completed_chunks(self, source_key: str) -> list[dict]:
        try:
            docs = self._job(source_key).collection("chunks").stream()
            return sorted((d.to_dict() for d in docs if d.to_dict().get("status") == "completed"),
                          key=lambda item: item["chunk_index"])
        except Exception as exc:
            raise StatePersistenceError(f"Firestore chunk read failed: {redact_secrets(str(exc))}") from exc

    def _transaction(self, callback):
        transaction = self.client.transaction()
        if not type(transaction).__module__.startswith("google.cloud"):
            try:
                return callback(transaction)
            finally:
                close = getattr(transaction, "close", None)
                if close: close()
        try:
            from google.cloud import firestore
            return firestore.transactional(callback)(transaction)
        except (StateConflictError, LeaseLostError, RecoveryRefusedError):
            raise
        except Exception as exc:
            raise StatePersistenceError(f"Firestore transaction failed: {redact_secrets(str(exc))}") from exc

    def _require_live_lease(self, tx, job_ref, now: datetime) -> dict:
        """Read and fence a job inside the caller's write transaction."""
        snap = job_ref.get(transaction=tx)
        job = snap.to_dict() if snap.exists else {}
        expires = job.get("lease_expires_at")
        if (job.get("status") == "completed" or job.get("lease_owner") != self.owner_id
                or not expires or expires <= now):
            job_id = getattr(job_ref, "id", getattr(job_ref, "path", "job"))
            raise LeaseLostError(f"Worker {self.owner_id} does not own a live lease for {job_id}")
        return job

    def acquire_lease(self, source_key: str, owner_id: str, ttl_seconds: int) -> bool:
        now = self.clock_fn()
        ref = self._job(source_key)
        def operation(tx):
            snap = ref.get(transaction=tx)
            data = snap.to_dict() if snap.exists else {}
            if data.get("status") == "completed": return False
            expires = data.get("lease_expires_at")
            if expires and expires > now and data.get("lease_owner") != owner_id: return False
            tx.set(ref, {"source_key": source_key, "status": data.get("status", "in_progress"),
                         "lease_owner": owner_id, "lease_expires_at": now + timedelta(seconds=ttl_seconds),
                         "updated_at": now}, merge=True)
            return True
        return self._transaction(operation)

    def renew_lease(self, source_key: str, owner_id: str, ttl_seconds: int) -> bool:
        now = self.clock_fn(); ref = self._job(source_key)
        def operation(tx):
            snap = ref.get(transaction=tx); data = snap.to_dict() if snap.exists else {}
            expires = data.get("lease_expires_at")
            if (data.get("lease_owner") != owner_id or data.get("status") == "completed"
                    or not expires or expires <= now): return False
            tx.set(ref, {"lease_expires_at": now + timedelta(seconds=ttl_seconds), "updated_at": now}, merge=True)
            return True
        return self._transaction(operation)

    def release_lease(self, source_key: str, owner_id: str) -> bool:
        now = self.clock_fn(); ref = self._job(source_key)
        def operation(tx):
            snap = ref.get(transaction=tx); data = snap.to_dict() if snap.exists else {}
            if data.get("lease_owner") != owner_id: return False
            tx.set(ref, {"lease_owner": None, "lease_expires_at": None, "updated_at": now}, merge=True)
            return True
        return self._transaction(operation)

    # Business-facing StateStore compatibility methods.
    def is_processed(self, file_id):
        job = self.get_job(self._key(file_id)); return bool(job and job.get("status") == "completed")
    def is_permanently_failed(self, file_id):
        job = self.get_job(self._key(file_id)); return bool(job and job.get("status") in ("failed", "failed_permanent"))
    is_failed = is_permanently_failed
    def is_retry_due(self, file_id):
        job = self.get_job(self._key(file_id))
        return bool(job and job.get("status") == "retryable" and (not job.get("next_retry_at") or self.clock_fn() >= job["next_retry_at"]))
    def get_partial_uploads(self, file_id):
        return (self.get_job(self._key(file_id)) or {}).get("partial_uploads", {})
    def save_partial_upload(self, file_id, doc_type, drive_id):
        if doc_type not in ("docx", "pdf"): raise ValueError("Invalid doc_type")
        key = self._key(file_id); ref = self._job(key); now = self.clock_fn()
        def operation(tx):
            data = self._require_live_lease(tx, ref, now)
            partials = dict(data.get("partial_uploads", {})); partials[doc_type] = drive_id
            tx.update(ref, {"partial_uploads": partials, "updated_at": now})
        self._transaction(operation)
    def remove_partial_upload(self, file_id, doc_type):
        key = self._key(file_id); ref = self._job(key); now = self.clock_fn()
        def operation(tx):
            data = self._require_live_lease(tx, ref, now)
            partials = dict(data.get("partial_uploads", {})); partials.pop(doc_type, None)
            tx.update(ref, {"partial_uploads": partials, "updated_at": now})
        self._transaction(operation)
    def mark_processed(self, file_id, name, model_used, final_ids=None):
        ref = self._job(self._key(file_id)); now = self.clock_fn(); expected_ids = final_ids or {}
        def operation(tx):
            snap = ref.get(transaction=tx); data = snap.to_dict() if snap.exists else {}
            if data.get("status") == "completed":
                compatible = (data.get("name") == name and data.get("model_used") == model_used
                              and data.get("final_ids", {}) == expected_ids)
                if compatible: return
                raise StateConflictError("Completed job has incompatible output metadata")
            self._require_live_lease(tx, ref, now)
            tx.update(ref, {"name": name, "status": "completed", "model_used": model_used,
                            "final_ids": expected_ids, "updated_at": now})
        self._transaction(operation)
    def mark_retryable(self, file_id, name, source, error_message, is_network_error=False):
        key = self._key(file_id, source=source); ref = self._job(key); now = self.clock_fn()
        def operation(tx):
            snapshot = ref.get(transaction=tx); current = snapshot.to_dict() if snapshot.exists else {}
            if current.get("status") == "completed":
                raise StateConflictError("Completed job cannot become retryable")
            old = self._require_live_lease(tx, ref, now)
            count = old.get("retry_count", 0) + 1
            network_count = old.get("network_retry_count")
            if is_network_error:
                network_count = old.get("network_retry_count", 0) + 1
                maximum = Config.MAX_NETWORK_FILE_RETRIES
                if maximum > 0 and network_count >= maximum:
                    update = dict(old)
                    update.update({"status": "failed_permanent", "source": source, "name": name,
                        "error_message": redact_secrets(f"Exceeded max network retries ({maximum}): {error_message}"),
                        "retry_count": count, "network_retry_count": network_count,
                        "last_attempt_at": now, "updated_at": now})
                    tx.update(ref, update)
                    return None
                delay = min(Config.NETWORK_FILE_RETRY_BASE_SECONDS * (2 ** (network_count - 1)),
                            Config.NETWORK_FILE_RETRY_MAX_SECONDS)
            else:
                if count >= Config.MAX_FILE_RETRIES:
                    update = dict(old)
                    update.update({"status": "failed_permanent", "source": source, "name": name,
                        "error_message": redact_secrets(f"Exceeded max retries ({Config.MAX_FILE_RETRIES}): {error_message}"),
                        "retry_count": count, "last_attempt_at": now, "updated_at": now})
                    tx.update(ref, update)
                    return None
                delay = min(Config.FILE_RETRY_BASE_SECONDS * (2 ** (count - 1)),
                            Config.FILE_RETRY_MAX_SECONDS)
            update = dict(old)
            update.update({"status": "retryable", "source": source, "name": name,
                "retry_count": count, "last_error": redact_secrets(error_message),
                "first_error_at": old.get("first_error_at", now), "last_attempt_at": now,
                "next_retry_at": now + timedelta(seconds=delay), "updated_at": now})
            if network_count is not None: update["network_retry_count"] = network_count
            tx.update(ref, update)
            return network_count if is_network_error else None
        network_count = self._transaction(operation)
        if is_network_error and network_count and network_count % 12 == 0:
            from utils import log_warn
            log_warn("persistent_network_failure", f"Network/DNS has failed for {network_count} consecutive file retries. Check internet/DNS configuration. Progress remains preserved and retryable.")
    def mark_permanent_failed(self, file_id, name, error_message, source="drive"):
        key = self._key(file_id, source=source); ref = self._job(key); now = self.clock_fn()
        def operation(tx):
            snapshot = ref.get(transaction=tx); current = snapshot.to_dict() if snapshot.exists else {}
            if current.get("status") == "completed":
                raise StateConflictError("Completed job cannot become failed")
            old = self._require_live_lease(tx, ref, now)
            update = dict(old)
            update.update({"status": "failed_permanent", "source": source, "name": name,
                           "error_message": redact_secrets(error_message), "updated_at": now})
            tx.update(ref, update)
        self._transaction(operation)
    def get_youtube_state(self, video_id):
        return (self.get_job(make_source_key("youtube", video_id)) or {}).get("youtube_state", {})
    def save_youtube_transcript(self, video_id, title, transcript_text, docx_id=None, pdf_id=None):
        key = self._key(video_id, source="youtube")
        # The long transcript remains represented by chunk documents; only workflow
        # metadata and output identifiers belong in the job document.
        self._update_youtube_state(key, {"transcript_status": "completed", "docx_id": docx_id,
                                   "pdf_id": pdf_id, "summary_status": "pending"})
    def get_youtube_transcript(self, video_id):
        chunks = self.list_completed_chunks(make_source_key("youtube", video_id))
        return "\n".join(c.get("transcript", "") for c in chunks) or None
    def mark_youtube_summary_completed(self, video_id, title, summary_text=None):
        key = self._key(video_id, source="youtube")
        self._update_youtube_state(key, {"summary_status": "completed", "summary_text": summary_text},
                                   job_fields={"status": "completed"})
    def get_youtube_summary(self, video_id): return self.get_youtube_state(video_id).get("summary_text")
    def save_youtube_summary_pending_delivery(self, video_id, title, summary_text):
        key = self._key(video_id, source="youtube")
        self._update_youtube_state(key, {"summary_status": "pending_delivery", "summary_text": summary_text})
    def mark_youtube_summary_failed(self, video_id, title, error_msg, permanent=False):
        key = self._key(video_id, source="youtube")
        self._update_youtube_state(key, {"summary_status": "failed_permanent" if permanent else "failed_retryable",
                                   "summary_error": redact_secrets(error_msg)})
    def _update_youtube_state(self, source_key, changes, job_fields=None):
        ref = self._job(source_key); now = self.clock_fn()
        def operation(tx):
            data = self._require_live_lease(tx, ref, now)
            state = dict(data.get("youtube_state", {})); state.update(changes)
            tx.update(ref, {"youtube_state": state, "updated_at": now, **(job_fields or {})})
        self._transaction(operation)
    def get_telegram_offset(self):
        snap = self.client.collection("runtime_state").document("telegram").get()
        return (snap.to_dict() or {}).get("offset", 0) if snap.exists else 0
    def set_telegram_offset(self, offset): self.client.collection("runtime_state").document("telegram").set({"offset": offset})


def create_state_backend(config=Config, *, client=None, clock_fn=utc_now):
    if config.STATE_BACKEND == "local":
        return LocalStateBackend()
    if config.STATE_BACKEND == "firestore":
        return FirestoreStateBackend(config.FIRESTORE_PROJECT_ID, config.FIRESTORE_DATABASE_ID,
                                     client=client, clock_fn=clock_fn, owner_id=config.WORKER_ID,
                                     lease_ttl_seconds=config.FIRESTORE_LEASE_TTL_SECONDS)
    raise ValueError(f"Unsupported STATE_BACKEND: {config.STATE_BACKEND}")
