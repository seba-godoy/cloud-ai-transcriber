"""Rescue semantic chunk-quality failures without weakening infrastructure retries.

Very long recordings should not fail permanently because one 60-second chunk is
semantically untranscribable by both general-purpose Gemini models. This runtime
wraps the existing chunk transcriber narrowly:

* API/network failures keep the historical retry behavior.
* Abnormal/non-STOP model completions keep the historical retry behavior.
* Only the existing semantic "poor quality / empty text" failures trigger a third
  attempt with the dedicated ``gemini-3.5-transcribe`` speech-to-text model.
* A natural STOP with usable rescue text is persisted normally.
* A natural STOP with no usable rescue text becomes ``[audio poco inteligible]``
  so a single irrecoverable minute cannot terminate a multi-hour job.

The wrapper is installed only by the cloud Phase 12 entrypoint. Local/legacy
imports of ``gemini_transcriber`` retain their historical behavior.
"""
from __future__ import annotations

import os
import time

import gemini_transcriber as transcriber
from utils import (
    TranscriptionUnavailableError,
    calculate_backoff,
    is_transient_error,
    log_start,
    log_warn,
    redact_secrets,
)

DEFAULT_RESCUE_MODEL = "gemini-3.5-transcribe"
UNINTELLIGIBLE_LABEL = "[audio poco inteligible]"
FILE_PROCESSING_TIMEOUT_SECONDS = 600


def _rescue_model() -> str:
    return os.getenv("GEMINI_TRANSCRIBE_MODEL", DEFAULT_RESCUE_MODEL).strip()


def _is_semantic_quality_failure(error: BaseException) -> bool:
    text = str(error).casefold()
    return (
        "produced poor quality transcript" in text
        or "returned empty transcript text" in text
    )


def _delete_remote_safely(client, remote, step: str) -> None:
    if remote is None or not getattr(remote, "name", None):
        return
    try:
        client.files.delete(name=remote.name)
    except Exception as exc:
        log_warn(
            step,
            "Could not delete dedicated rescue upload: "
            f"{redact_secrets(str(exc))}",
        )


def _upload_and_wait(client, chunk_path: str):
    transcriber.assert_ascii_upload_basename(chunk_path)
    remote = None
    last_error = None
    for attempt in range(1, 6):
        try:
            remote = client.files.upload(file=chunk_path)
            break
        except Exception as exc:
            last_error = exc
            if not is_transient_error(exc):
                raise
            if attempt < 5:
                delay = calculate_backoff(attempt, base_delay=2.0, max_delay=16.0)
                log_warn(
                    "gemini_transcribe_rescue_upload",
                    f"Transient rescue upload error (Attempt {attempt}/5). "
                    f"Retrying in {delay:.1f}s...",
                )
                time.sleep(delay)
    if remote is None:
        raise last_error or RuntimeError("Dedicated transcription rescue upload failed")

    started = time.time()
    poll_attempt = 0
    try:
        while True:
            poll_attempt += 1
            try:
                remote = client.files.get(name=remote.name)
            except Exception as exc:
                if not is_transient_error(exc):
                    raise
                if time.time() - started > FILE_PROCESSING_TIMEOUT_SECONDS:
                    raise TimeoutError("Timeout waiting for rescue audio file to process") from exc
                time.sleep(min(2 * poll_attempt, 10))
                continue

            state = str(getattr(remote, "state", "")).upper()
            if "FAILED" in state:
                raise RuntimeError("Gemini rescue file processing failed")
            if "ACTIVE" in state:
                return remote
            if time.time() - started > FILE_PROCESSING_TIMEOUT_SECONDS:
                raise TimeoutError("Timeout waiting for rescue audio file to process")
            time.sleep(3)
    except Exception:
        # If polling fails after upload, ownership never returns to the caller;
        # clean the remote object here so the rescue path cannot leak Files API data.
        _delete_remote_safely(client, remote, "gemini_transcribe_rescue_cleanup")
        raise


def _generate_dedicated_transcription(client, model: str, remote):
    """Call the dedicated speech-to-text model using the uploaded audio only."""
    last_error = None
    for attempt in range(1, 6):
        try:
            response = client.models.generate_content(model=model, contents=[remote])
            return transcriber._result_from_response(response)
        except Exception as exc:
            last_error = exc
            if not is_transient_error(exc):
                raise
            if attempt < 5:
                delay = calculate_backoff(attempt, base_delay=2.0, max_delay=16.0)
                log_warn(
                    "gemini_transcribe_rescue_generate",
                    f"Transient rescue generation error on {model} (Attempt {attempt}/5). "
                    f"Retrying in {delay:.1f}s...",
                )
                time.sleep(delay)
    raise last_error or RuntimeError("Dedicated transcription rescue failed")


def _transcribe_with_dedicated_model(client, chunk_path: str, model: str):
    remote = None
    try:
        remote = _upload_and_wait(client, chunk_path)
        return _generate_dedicated_transcription(client, model, remote)
    finally:
        _delete_remote_safely(client, remote, "gemini_transcribe_rescue_cleanup")


def _build_rescuing_transcribe_chunk(original):
    def transcribe_chunk_with_quality_rescue(
        client,
        chunk_path: str,
        default_model: str,
        fallback_model: str,
        prompt: str,
        chunk_duration_ms: int,
        silence_ratio: float | None = None,
    ) -> tuple[str, str]:
        try:
            return original(
                client,
                chunk_path,
                default_model,
                fallback_model,
                prompt,
                chunk_duration_ms,
                silence_ratio,
            )
        except TranscriptionUnavailableError as original_error:
            if not _is_semantic_quality_failure(original_error):
                raise

            model = _rescue_model()
            if not model:
                raise

            log_start(
                f"General-model quality consensus failed; trying dedicated rescue model {model}"
            )
            try:
                rescue = _transcribe_with_dedicated_model(client, chunk_path, model)
            except Exception as rescue_error:
                if is_transient_error(rescue_error):
                    raise TranscriptionUnavailableError(
                        f"Dedicated rescue model {model} failed transiently after semantic "
                        f"quality failure: {redact_secrets(str(rescue_error))}"
                    ) from rescue_error

                # The rescue model is deliberately an extra safety net rather than
                # a new single point of failure. Two general models already reached
                # a semantic quality failure, so a deterministic rescue-integration
                # error degrades this one minute instead of killing a multi-hour job.
                log_warn(
                    "gemini_transcribe_rescue_unavailable",
                    f"Dedicated rescue model {model} failed permanently; preserving forward "
                    f"progress with {UNINTELLIGIBLE_LABEL}: "
                    f"{redact_secrets(str(rescue_error))}",
                )
                return UNINTELLIGIBLE_LABEL, "local-filter"

            transcriber._log_attempt(
                model,
                rescue,
                True,
                chunk_duration_ms,
                silence_ratio,
            )

            if rescue.completed:
                text = (rescue.text or "").strip()
                if text and not transcriber.is_poor_quality(text, chunk_duration_ms):
                    log_warn(
                        "gemini_transcribe_rescue_recovered",
                        f"Dedicated model {model} recovered a chunk rejected by both "
                        "general transcription models.",
                    )
                    return text, model

                log_warn(
                    "gemini_quality_consensus_unintelligible",
                    "Both general models and the dedicated transcription model completed "
                    f"without usable speech; persisting {UNINTELLIGIBLE_LABEL} and continuing.",
                )
                return UNINTELLIGIBLE_LABEL, "local-filter"

            reason = rescue.finish_reason or "MISSING"
            raise TranscriptionUnavailableError(
                f"Dedicated rescue model {model} ended with finish_reason={reason}; "
                "retrying instead of silently dropping possible speech."
            ) from original_error

    return transcribe_chunk_with_quality_rescue


def install_quality_rescue_runtime() -> None:
    """Install the cloud-only chunk rescue wrapper exactly once."""
    if getattr(transcriber, "_quality_rescue_runtime_installed", False):
        return
    transcriber.transcribe_chunk = _build_rescuing_transcribe_chunk(
        transcriber.transcribe_chunk
    )
    transcriber._quality_rescue_runtime_installed = True
