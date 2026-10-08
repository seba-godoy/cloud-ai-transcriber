"""Runtime layer for automatic long-media batching on Cloud Run.

The existing transcription pipeline already persists completed 60-second chunks in
Firestore. This module adds a cooperative execution budget on top of that durable
checkpointing so very long files can span many Cloud Run Job executions without
operator intervention or consuming the whole 90-minute task timeout.

When a Cloud Storage volume is mounted through ``LONG_MEDIA_CACHE_DIR``, Drive
sources are downloaded once into that persistent cache and reused by later batches.
The core pipeline receives only a short-lived symlink, so its normal cleanup does
not delete the cached source until the transcription really completes.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass

from config import Config
from state_backend import FirestoreStateBackend


DEFAULT_MAX_CHUNKS_PER_EXECUTION = 90
DEFAULT_MAX_EXECUTION_SECONDS = 45 * 60
DEFAULT_DIRECT_MP4_THRESHOLD_SECONDS = 2 * 60 * 60
DEFAULT_LOCAL_STAGE_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_CACHE_DIR = ""
_STAGED_CACHE_ENTRIES: dict[str, tuple[str, str]] = {}


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class CooperativeBatchInfo:
    source_key: str
    completed_chunks: int
    total_chunks: int
    saved_this_execution: int
    elapsed_seconds: float
    reason: str


class CooperativeBatchYield(RuntimeError):
    """Intentional, non-error stop after durable progress has been saved."""

    def __init__(self, info: CooperativeBatchInfo):
        self.info = info
        super().__init__(
            "Cooperative long-media batch complete: "
            f"{info.completed_chunks}/{info.total_chunks} chunks; reason={info.reason}"
        )


def _batch_limits() -> tuple[int, int]:
    return (
        _positive_int_env(
            "LONG_MEDIA_MAX_CHUNKS_PER_EXECUTION",
            DEFAULT_MAX_CHUNKS_PER_EXECUTION,
        ),
        _positive_int_env(
            "LONG_MEDIA_MAX_EXECUTION_SECONDS",
            DEFAULT_MAX_EXECUTION_SECONDS,
        ),
    )


def _direct_mp4_threshold_seconds() -> int:
    return _positive_int_env(
        "LONG_MEDIA_DIRECT_MP4_THRESHOLD_SECONDS",
        DEFAULT_DIRECT_MP4_THRESHOLD_SECONDS,
    )


def _cache_dir() -> str:
    return os.getenv("LONG_MEDIA_CACHE_DIR", DEFAULT_CACHE_DIR).strip()


def _local_stage_max_bytes() -> int:
    return _positive_int_env(
        "LONG_MEDIA_LOCAL_STAGE_MAX_BYTES",
        DEFAULT_LOCAL_STAGE_MAX_BYTES,
    )


def _stage_cached_source_locally(app_module, cached_path: str, marker_path: str) -> str:
    """Copy ordinary-sized cached media onto Cloud Run's local filesystem.

    Cloud Storage FUSE is ideal for persistence across executions, but media
    demuxers perform metadata and random reads that are safer and faster on the
    local ephemeral filesystem. Very large sources stay on the mounted cache so
    the long-media path keeps its bounded disk behavior.
    """
    size = os.path.getsize(cached_path)
    if size > _local_stage_max_bytes():
        return cached_path

    os.makedirs(Config.TEMP_DIR, exist_ok=True)
    local_name = "localcache_" + os.path.basename(cached_path)
    local_path = os.path.join(Config.TEMP_DIR, local_name)
    if os.path.lexists(local_path):
        os.remove(local_path)
    shutil.copyfile(cached_path, local_path)
    _STAGED_CACHE_ENTRIES[os.path.realpath(local_path)] = (cached_path, marker_path)
    app_module.log_ok(
        f"Staged cached Drive source on local filesystem for media probing; bytes={size}"
    )
    return local_path


def _reset_batch_state(state_store) -> None:
    state_store._long_media_batch_started_at = time.monotonic()
    state_store._long_media_saved_chunks = 0
    state_store._long_media_batch_info = None


def _should_yield_after_chunk(state_store, source_key: str, chunk_index: int,
                              total_chunks: int | None) -> CooperativeBatchInfo | None:
    if not total_chunks or total_chunks <= 0:
        return None

    max_chunks, max_seconds = _batch_limits()
    completed_chunks = chunk_index + 1
    if completed_chunks >= total_chunks:
        return None

    # Small and medium jobs retain their historical single-execution behavior.
    if total_chunks <= max_chunks:
        return None

    state_store._long_media_saved_chunks = (
        int(getattr(state_store, "_long_media_saved_chunks", 0)) + 1
    )
    saved_this_execution = state_store._long_media_saved_chunks
    started = getattr(state_store, "_long_media_batch_started_at", None)
    if started is None:
        started = time.monotonic()
        state_store._long_media_batch_started_at = started
    elapsed_seconds = max(0.0, time.monotonic() - started)

    reason = None
    if saved_this_execution >= max_chunks:
        reason = "chunk_budget"
    elif elapsed_seconds >= max_seconds:
        reason = "wall_clock_budget"

    if reason is None:
        return None

    return CooperativeBatchInfo(
        source_key=source_key,
        completed_chunks=completed_chunks,
        total_chunks=total_chunks,
        saved_this_execution=saved_this_execution,
        elapsed_seconds=elapsed_seconds,
        reason=reason,
    )


def _cache_paths(app_module, file_id: str, file_name: str) -> tuple[str, str] | None:
    cache_dir = _cache_dir()
    if not cache_dir:
        return None
    os.makedirs(cache_dir, exist_ok=True)
    suffix = file_id[:8] if file_id else ""
    cached_name = app_module.sanitize_filename(
        file_name, fallback="audio_drive", suffix=suffix
    )
    cached_path = os.path.join(cache_dir, cached_name)
    return cached_path, cached_path + ".cache.json"


def _remote_drive_identity(app_module, file_id: str) -> dict:
    metadata = app_module.get_drive_service().files().get(
        fileId=file_id,
        fields="id,size,modifiedTime",
    ).execute()
    return {
        "file_id": file_id,
        "size": str(metadata.get("size", "")),
        "modified_time": str(metadata.get("modifiedTime", "")),
    }


def _read_cache_marker(marker_path: str) -> dict | None:
    try:
        with open(marker_path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _write_cache_marker(marker_path: str, identity: dict) -> None:
    temp_path = marker_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(identity, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, marker_path)


def _cache_entry_complete(cached_path: str, marker_path: str, identity: dict) -> bool:
    if not os.path.isfile(cached_path) or os.path.getsize(cached_path) <= 0:
        return False
    marker = _read_cache_marker(marker_path)
    if marker != identity:
        return False
    expected_size = identity.get("size")
    return not expected_size or str(os.path.getsize(cached_path)) == str(expected_size)


def _remove_cache_entry(cached_path: str | None, marker_path: str | None) -> None:
    for path in (cached_path, marker_path):
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def install_long_media_runtime(app_module) -> None:
    """Install long-media batching once into the Phase 12 cloud entrypoint."""
    if getattr(app_module, "_long_media_runtime_installed", False):
        return

    original_save_chunk = FirestoreStateBackend.save_chunk
    original_handle_processing_failure = app_module.handle_processing_failure
    original_process_file = app_module.process_file
    original_run_polling_cycle = app_module.run_polling_cycle
    original_execute_pipeline = app_module.execute_pipeline
    original_download_file = app_module.download_file

    def save_chunk_with_cooperative_budget(self, source_key, chunk_index, transcript,
                                           model_used, word_count, owner_id, **metadata):
        # Persist first. Only after Firestore confirms the chunk is durable may
        # the worker intentionally yield the execution.
        original_save_chunk(
            self,
            source_key,
            chunk_index,
            transcript,
            model_used,
            word_count,
            owner_id,
            **metadata,
        )
        info = _should_yield_after_chunk(
            self,
            source_key,
            chunk_index,
            metadata.get("total_chunks"),
        )
        if info is not None:
            self._long_media_batch_info = info
            raise CooperativeBatchYield(info)

    def handle_processing_failure_with_cooperative_yield(
        file_id, file_name, source, error, state_store
    ):
        if isinstance(error, CooperativeBatchYield):
            info = error.info
            state_store._long_media_batch_info = info
            app_module.log_ok(
                "Long-media batch checkpointed cleanly: "
                f"{info.completed_chunks}/{info.total_chunks} chunks; "
                f"saved_this_execution={info.saved_this_execution}; "
                f"elapsed={info.elapsed_seconds:.1f}s; reason={info.reason}. "
                "The next scheduler execution will resume automatically."
            )
            return
        return original_handle_processing_failure(
            file_id, file_name, source, error, state_store
        )

    def download_file_with_persistent_cache(file_id: str, file_name: str) -> str:
        paths = _cache_paths(app_module, file_id, file_name)
        if paths is None:
            return original_download_file(file_id, file_name)
        cached_path, marker_path = paths

        try:
            identity = _remote_drive_identity(app_module, file_id)
        except Exception as exc:
            app_module.log_warn(
                "long_media_cache_identity",
                "Could not verify persistent Drive cache identity; downloading through "
                f"the normal path: {app_module.redact_secrets(str(exc))}",
            )
            return original_download_file(file_id, file_name)

        if _cache_entry_complete(cached_path, marker_path, identity):
            # download_file normally activates the owner-specific output route.
            # A cache hit must preserve the same behavior.
            Config.activate_drive_file_route(file_id)
            app_module.log_ok(
                f"Reusing persistent long-media cache for {file_name}; "
                f"bytes={os.path.getsize(cached_path)}"
            )
            return _stage_cached_source_locally(app_module, cached_path, marker_path)

        _remove_cache_entry(cached_path, marker_path)
        previous_temp_dir = Config.TEMP_DIR
        try:
            Config.TEMP_DIR = os.path.dirname(cached_path)
            downloaded_path = original_download_file(file_id, file_name)
        finally:
            Config.TEMP_DIR = previous_temp_dir

        if os.path.abspath(downloaded_path) != os.path.abspath(cached_path):
            os.replace(downloaded_path, cached_path)
        if identity.get("size") and str(os.path.getsize(cached_path)) != identity["size"]:
            _remove_cache_entry(cached_path, marker_path)
            raise IOError("Persistent Drive cache size did not match source metadata")
        _write_cache_marker(marker_path, identity)
        app_module.log_ok(
            f"Stored Drive source in persistent long-media cache; bytes={os.path.getsize(cached_path)}"
        )
        return _stage_cached_source_locally(app_module, cached_path, marker_path)

    def process_file_with_batch_budget(item, state_store):
        # Once one long file yields, do not start another Drive transcription in
        # the same one-shot cycle. This keeps total execution time bounded while
        # still allowing the Telegram portion of the cycle to run afterward.
        if getattr(state_store, "_long_media_cycle_yielded", False):
            app_module.log_ok(
                "Long-media execution budget already consumed; deferring remaining "
                "Drive files to the next scheduler cycle."
            )
            return None

        _reset_batch_state(state_store)
        result = original_process_file(item, state_store)
        if getattr(state_store, "_long_media_batch_info", None) is not None:
            state_store._long_media_cycle_yielded = True
        return result

    def run_polling_cycle_with_batch_reset(state_store, telegram_offset):
        state_store._long_media_cycle_yielded = False
        return original_run_polling_cycle(state_store, telegram_offset)

    def execute_pipeline_with_long_media_support(
        local_audio_path,
        file_name,
        file_id,
        state_store,
        *,
        source,
        is_telegram_direct=False,
    ):
        cached_path = None
        marker_path = None
        working_path = local_audio_path
        cache_dir = _cache_dir()
        staged_entry = _STAGED_CACHE_ENTRIES.pop(os.path.realpath(local_audio_path), None)
        if source == "drive" and staged_entry:
            cached_path, marker_path = staged_entry
        elif source == "drive" and cache_dir:
            try:
                real_cache_dir = os.path.realpath(cache_dir)
                real_input = os.path.realpath(local_audio_path)
                if os.path.commonpath([real_cache_dir, real_input]) == real_cache_dir:
                    cached_path = local_audio_path
                    marker_path = cached_path + ".cache.json"
                    os.makedirs(Config.TEMP_DIR, exist_ok=True)
                    link_name = "longmedia_" + os.path.basename(cached_path)
                    working_path = os.path.join(Config.TEMP_DIR, link_name)
                    if os.path.lexists(working_path):
                        os.remove(working_path)
                    os.symlink(cached_path, working_path)
            except Exception as exc:
                working_path = local_audio_path
                cached_path = None
                marker_path = None
                app_module.log_warn(
                    "long_media_cache_link",
                    "Could not isolate persistent cache from normal cleanup; "
                    f"using source directly: {app_module.redact_secrets(str(exc))}",
                )

        if (
            source == "drive"
            and isinstance(working_path, str)
            and working_path.lower().endswith(".mp4")
        ):
            try:
                duration_seconds = app_module.probe_audio_duration(working_path)
                if duration_seconds >= _direct_mp4_threshold_seconds():
                    # The core chunk extractor already asks ffmpeg for bounded
                    # intervals. Renaming only the disposable symlink/local path
                    # prevents full-file MP4->MP3 transcoding on every batch.
                    direct_path = working_path + ".longmedia"
                    if os.path.lexists(direct_path):
                        os.remove(direct_path)
                    os.replace(working_path, direct_path)
                    working_path = direct_path
                    app_module.log_ok(
                        f"Long MP4 detected ({duration_seconds / 3600:.2f}h); "
                        "using direct bounded ffmpeg chunk extraction."
                    )
            except Exception as exc:
                app_module.log_warn(
                    "long_media_direct_mp4",
                    "Could not activate direct long-MP4 chunking; falling back to "
                    f"legacy extraction: {app_module.redact_secrets(str(exc))}",
                )

        try:
            result = original_execute_pipeline(
                working_path,
                file_name,
                file_id,
                state_store,
                source=source,
                is_telegram_direct=is_telegram_direct,
            )
        finally:
            # Usually the core pipeline already removed this disposable path.
            if working_path != cached_path and os.path.lexists(working_path):
                try:
                    os.remove(working_path)
                except OSError:
                    pass

        if cached_path:
            completed = bool(result and result[0])
            permanently_failed = False
            if not completed and getattr(state_store, "backend_name", None) == "firestore":
                try:
                    job = state_store.get_job(state_store._key(file_id, source=source)) or {}
                    permanently_failed = job.get("status") in ("failed", "failed_permanent")
                except Exception:
                    permanently_failed = False
            if completed or permanently_failed:
                _remove_cache_entry(cached_path, marker_path)
                app_module.log_ok(
                    "Persistent long-media cache released after terminal job state."
                )

        return result

    FirestoreStateBackend.save_chunk = save_chunk_with_cooperative_budget
    app_module.handle_processing_failure = handle_processing_failure_with_cooperative_yield
    app_module.download_file = download_file_with_persistent_cache
    app_module.process_file = process_file_with_batch_budget
    app_module.run_polling_cycle = run_polling_cycle_with_batch_reset
    app_module.execute_pipeline = execute_pipeline_with_long_media_support
    app_module._long_media_runtime_installed = True
