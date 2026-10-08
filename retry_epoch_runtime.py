"""Reset a file's retry epoch after a newly durable chunk proves recovery.

The historical retry counter is file-wide. That is acceptable for short files, but a
multi-hour job can encounter several unrelated transient failures over many hours. If
successful forward progress does not reset the budget, five scattered failures can
permanently fail an otherwise healthy job.

This cloud-only runtime wraps Firestore ``save_chunk`` before long-media batching is
installed. A chunk is committed first. Only after the durable write succeeds do we
reset active retry/backoff state for the next failure epoch. The last recovered error
is retained as audit metadata rather than remaining an active operator-facing error.
"""
from __future__ import annotations

from state_backend import FirestoreStateBackend
from utils import redact_secrets


def _reset_retry_epoch_after_progress(backend: FirestoreStateBackend, source_key: str) -> None:
    now = backend.clock_fn()
    ref = backend._job(source_key)

    def operation(tx):
        job = backend._require_live_lease(tx, ref, now)
        retry_count = int(job.get("retry_count", 0) or 0)
        network_retry_count = int(job.get("network_retry_count", 0) or 0)
        retry_state = job.get("status") == "retryable"
        has_active_error = bool(job.get("last_error") or job.get("error_message"))

        if not (retry_count or network_retry_count or retry_state or has_active_error):
            return False

        recovered_error = job.get("error_message") or job.get("last_error")
        update = {
            "status": "in_progress",
            "retry_count": 0,
            "network_retry_count": 0,
            "next_retry_at": None,
            "first_error_at": None,
            "last_error": None,
            "error_message": None,
            "retry_epoch_recovered_at": now,
            "updated_at": now,
        }
        if recovered_error:
            update["last_recovered_error"] = redact_secrets(str(recovered_error))
        tx.set(ref, update, merge=True)
        return True

    backend._transaction(operation)


def _build_retry_epoch_save_chunk(original):
    def save_chunk_with_retry_epoch_reset(
        self,
        source_key: str,
        chunk_index: int,
        transcript: str,
        model_used: str,
        word_count: int,
        owner_id: str,
        **metadata,
    ) -> None:
        # The chunk must be durable before the retry budget is forgiven.
        original(
            self,
            source_key,
            chunk_index,
            transcript,
            model_used,
            word_count,
            owner_id,
            **metadata,
        )
        _reset_retry_epoch_after_progress(self, source_key)

    return save_chunk_with_retry_epoch_reset


def install_retry_epoch_runtime() -> None:
    """Install exactly once, before long-media wraps ``save_chunk`` itself."""
    if getattr(FirestoreStateBackend, "_retry_epoch_runtime_installed", False):
        return
    FirestoreStateBackend.save_chunk = _build_retry_epoch_save_chunk(
        FirestoreStateBackend.save_chunk
    )
    FirestoreStateBackend._retry_epoch_runtime_installed = True
