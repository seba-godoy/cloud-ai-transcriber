import os
import time
import math
import difflib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pydub import AudioSegment
from pydub.silence import detect_silence
from google import genai
from google.genai import types
from config import Config
from utils import (
    log_error, 
    log_ok, 
    log_start, 
    log_warn, 
    retry, 
    is_transient_error, 
    redact_secrets,
    calculate_backoff,
    make_source_key,
    TranscriptionUnavailableError,
    ProgressPersistenceError
)

SILENCE_NOISE_DB = -45.0
SILENCE_MIN_DURATION_MS = 3000
SILENCE_BLOCK_RATIO = 0.98
WEAK_AUDIO_BLOCK_RATIO = 0.95
DEFENSIVE_HALLUCINATION_RATIO = 0.65
SHORT_SPEECH_VERIFICATION_MS = 30 * 1000
FFMPEG_SPEECH_ENHANCEMENT_FILTER = "highpass=f=100,lowpass=f=6000,afftdn,dynaudnorm"
CHUNK_DURATION_SECONDS = 60


def gemini_working_basename(source: str, source_id: str, chunk_index: int,
                            enhanced: bool = False) -> str:
    """Return a deterministic ASCII basename for files crossing the Gemini boundary."""
    if not source_id:
        raise ValueError("source_id is required for Gemini working files")
    if chunk_index < 0:
        raise ValueError("chunk_index must be non-negative")
    token = make_source_key(source, source_id)
    suffix = "_enhanced" if enhanced else ""
    basename = f"gemini_{token}_chunk_{chunk_index:04d}{suffix}.wav"
    if not basename.isascii():  # Construction invariant; never transliterate input.
        raise ValueError("Gemini working basename must be ASCII")
    return basename


def assert_ascii_upload_basename(path: str) -> None:
    """Reject unsafe metadata at the Gemini Files API boundary."""
    basename = os.path.basename(path)
    if not basename or not basename.isascii():
        raise ValueError("Gemini upload basename must be non-empty ASCII")


DURATION_FALLBACK_TIMEOUT_SECONDS = 300


def _positive_duration(value: str | None) -> float | None:
    """Parse one ffprobe duration value, rejecting N/A/invalid/non-positive data."""
    if value is None:
        return None
    try:
        duration = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    return duration


def _ffmpeg_progress_duration(progress_text: str) -> float | None:
    """Return the greatest out_time reported by ffmpeg machine-readable progress."""
    greatest = None
    for raw_line in (progress_text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("out_time="):
            continue
        value = line.split("=", 1)[1].strip()
        try:
            hours_s, minutes_s, seconds_s = value.split(":", 2)
            seconds = int(hours_s) * 3600 + int(minutes_s) * 60 + float(seconds_s)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds > 0:
            greatest = max(greatest or 0.0, seconds)
    return greatest


def _subprocess_error_detail(exc: BaseException) -> str:
    """Expose bounded, redacted ffmpeg/ffprobe stderr instead of only exit code 1."""
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    detail = str(stderr or exc).strip()
    if len(detail) > 1600:
        detail = detail[-1600:]
    return redact_secrets(detail)


def probe_audio_duration(path: str) -> float:
    """Determine audio duration with layered metadata probes and bounded decode recovery."""
    attempts = [
        (
            "container",
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
        ),
        (
            "audio_stream",
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
        ),
    ]
    diagnostics = []

    for label, command in attempts:
        try:
            result = subprocess.run(
                command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            candidates = [
                _positive_duration(line)
                for line in result.stdout.splitlines()
                if line.strip()
            ]
            duration = next((item for item in candidates if item is not None), None)
            if duration is not None:
                if label != "container":
                    log_warn(
                        "duration_probe_recovered",
                        f"Recovered media duration from {label} metadata after container probe failed.",
                    )
                return duration
            diagnostics.append(
                f"{label}: no usable duration ({result.stdout.strip() or 'empty output'})"
            )
        except (OSError, subprocess.SubprocessError) as exc:
            diagnostics.append(f"{label}: {_subprocess_error_detail(exc)}")

    log_warn(
        "duration_probe_decode_fallback",
        "ffprobe metadata probes failed; attempting bounded ffmpeg decode-to-null recovery.",
    )
    decode_command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
        "-progress", "pipe:1", "-nostats",
        "-i", path, "-map", "0:a:0", "-vn", "-f", "null", "-",
    ]
    try:
        decoded = subprocess.run(
            decode_command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=DURATION_FALLBACK_TIMEOUT_SECONDS,
        )
        duration = _ffmpeg_progress_duration(decoded.stdout)
        if duration is not None:
            log_warn(
                "duration_probe_recovered",
                f"Recovered media duration by decoding audio timeline ({duration:.3f}s).",
            )
            return duration
        diagnostics.append("decode: ffmpeg completed but emitted no usable out_time")
    except (OSError, subprocess.SubprocessError) as exc:
        diagnostics.append(f"decode: {_subprocess_error_detail(exc)}")

    detail = " | ".join(diagnostics)
    if len(detail) > 3500:
        detail = detail[-3500:]
    raise ValueError(f"Could not determine audio duration. {detail}")

def audio_chunk_boundaries(duration_seconds: float) -> list[tuple[int, float, float]]:
    """Build stable, ordered 60-second intervals, including the final partial one."""
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("Audio duration must be positive and finite")
    count = math.ceil(duration_seconds / CHUNK_DURATION_SECONDS)
    return [(index, index * CHUNK_DURATION_SECONDS,
             min((index + 1) * CHUNK_DURATION_SECONDS, duration_seconds))
            for index in range(count)]


def extract_audio_chunk(source_path: str, destination_path: str,
                        start_seconds: float, duration_seconds: float) -> None:
    """Decode one bounded interval to mono 16 kHz PCM WAV (~1.9 MB per minute)."""
    command = [
        "ffmpeg", "-y", "-v", "error", "-ss", f"{start_seconds:.6f}",
        "-i", source_path, "-t", f"{duration_seconds:.6f}",
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", destination_path,
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Failed to extract bounded audio chunk: {redact_secrets(str(exc))}") from exc

if not (0 <= DEFENSIVE_HALLUCINATION_RATIO < WEAK_AUDIO_BLOCK_RATIO < SILENCE_BLOCK_RATIO <= 1):
    raise ValueError("Invalid silence ratio thresholds configuration.")

def classify_audio_block(silence_ratio: float) -> str:
    """
    Pure function to classify an audio block based on silence ratio.
    Returns: 'silence', 'weak', or 'speech'
    """
    if silence_ratio >= SILENCE_BLOCK_RATIO:
        return "silence"
    elif silence_ratio >= WEAK_AUDIO_BLOCK_RATIO:
        return "weak"
    return "speech"

def is_poor_quality(transcript: str, chunk_duration_ms: int) -> bool:
    """
    Heuristics to determine if the transcription result is of poor quality.
    """
    if not transcript or not transcript.strip():
        return True
        
    text_lower = transcript.lower()
    if text_lower.count("[inaudible]") > 5:
        return True
        
    return False

def format_time(seconds: int) -> str:
    m = seconds // 60
    s = seconds % 60
    return f"{m:02d}:{s:02d}"

def is_duplicate_block(text1: str, text2: str) -> bool:
    """Checks if two consecutive blocks are almost identical."""
    if not text1 or not text2:
        return False
        
    seq = difflib.SequenceMatcher(None, text1.lower()[:200], text2.lower()[:200])
    if seq.ratio() > 0.8:
        return True
    return False

@dataclass(frozen=True)
class GenerationResult:
    """The non-sensitive parts of a Gemini generation needed for validation."""
    text: str
    finish_reason: str | None
    finish_message: str | None
    number_of_candidates: int

    @property
    def completed(self) -> bool:
        return self.finish_reason == "STOP"


def normalize_finish_reason(value) -> str | None:
    """Normalize SDK enums and strings without treating an unknown value as success."""
    if value is None:
        return None
    raw = getattr(value, "name", None) or getattr(value, "value", None) or str(value)
    raw = str(raw).strip().upper()
    if not raw:
        return None
    # google.genai enums stringify as ``FinishReason.STOP`` in some releases.
    raw = raw.rsplit(".", 1)[-1].replace("FINISH_REASON_", "")
    return raw or None


def _result_from_response(response) -> GenerationResult:
    candidates = list(getattr(response, "candidates", None) or []) if response else []
    candidate = candidates[0] if candidates else None
    return GenerationResult(
        text=(getattr(response, "text", None) or "") if response else "",
        finish_reason=normalize_finish_reason(getattr(candidate, "finish_reason", None)),
        finish_message=getattr(candidate, "finish_message", None),
        number_of_candidates=len(candidates),
    )


def _generate(client, model: str, gemini_file, prompt: str) -> GenerationResult:
    """
    Helper to generate content safely with retry for transient API errors.
    Uses 5 attempts with calculate_backoff for network resilience.
    """
    file_part = types.Part.from_uri(
        file_uri=gemini_file.uri,
        mime_type=gemini_file.mime_type
    )
    
    last_err = None
    for attempt in range(1, 6):
        try:
            response = client.models.generate_content(
                model=model,
                contents=[file_part, prompt]
            )
            return _result_from_response(response)
        except Exception as e:
            last_err = e
            if not is_transient_error(e):
                raise
            if attempt < 5:
                sleep_dur = calculate_backoff(attempt, base_delay=2.0, max_delay=16.0)
                log_warn("_generate", f"Transient network/API error on {model} (Attempt {attempt}/5). Retrying in {sleep_dur:.1f}s...")
                time.sleep(sleep_dur)
    raise last_err

def _log_attempt(model: str, result: GenerationResult, fallback_triggered: bool,
                 chunk_duration_ms: int, silence_ratio: float | None) -> None:
    speech_class = classify_audio_block(silence_ratio) if silence_ratio is not None else "unknown"
    reason = result.finish_reason or "MISSING"
    log_ok("Gemini attempt: " +
           f"model={model}, finish_reason={reason}, fallback_triggered={fallback_triggered}, "
           f"chunk_duration_ms={chunk_duration_ms}, silence_ratio={silence_ratio if silence_ratio is not None else 'unknown'}, "
           f"audio_class={speech_class}, output_word_count={len(result.text.split())}")


def _usable(result: GenerationResult, chunk_duration_ms: int) -> bool:
    return result.completed and not is_poor_quality(result.text, chunk_duration_ms)


def transcribe_chunk(client, chunk_path: str, default_model: str, fallback_model: str,
                     prompt: str, chunk_duration_ms: int,
                     silence_ratio: float | None = None) -> tuple[str, str]:
    """Uploads, transcribes, and cleans up a single chunk cleanly."""
    assert_ascii_upload_basename(chunk_path)
    log_start(f"Uploading ASCII working chunk {os.path.basename(chunk_path)}")
    gemini_file = None
    
    try:
        upload_err = None
        for attempt in range(1, 6):
            try:
                gemini_file = client.files.upload(file=chunk_path)
                break
            except Exception as e:
                upload_err = e
                if not is_transient_error(e):
                    raise
                if attempt < 5:
                    sleep_dur = calculate_backoff(attempt, base_delay=2.0, max_delay=16.0)
                    log_warn("gemini_upload_chunk", f"Transient upload error (Attempt {attempt}/5). Retrying in {sleep_dur:.1f}s...")
                    time.sleep(sleep_dur)
        if gemini_file is None:
            raise upload_err or Exception("Failed to upload chunk to Gemini.")

        timeout = 600
        start = time.time()
        poll_attempt = 0
        
        while True:
            poll_attempt += 1
            try:
                gemini_file = client.files.get(name=gemini_file.name)
            except Exception as e:
                if not is_transient_error(e):
                    log_error("gemini_file_get", f"Permanent failure checking Gemini file state: {redact_secrets(str(e))}")
                    raise
                if time.time() - start > timeout:
                    raise Exception("Timeout waiting for audio chunk to process.")
                time.sleep(min(2 * poll_attempt, 10))
                continue
                
            state_str = str(getattr(gemini_file, 'state', '')).upper()
            if "FAILED" in state_str:
                raise Exception("Gemini file processing failed for chunk.")
            elif "ACTIVE" in state_str:
                break
                
            if time.time() - start > timeout:
                raise Exception("Timeout waiting for audio chunk to process.")
            time.sleep(3)

        transcript = None
        used_model = default_model
        needs_fallback = False
        primary_result = None
        primary_usable = False
        
        try:
            primary_result = _generate(client, default_model, gemini_file, prompt)
            transcript = primary_result.text
            primary_usable = _usable(primary_result, chunk_duration_ms)
            needs_fallback = not primary_usable
            # A bounded second opinion catches clean opening-only answers without
            # imposing a fixed word minimum on naturally quiet/slow clips.
            if chunk_duration_ms <= SHORT_SPEECH_VERIFICATION_MS and classify_audio_block(
                    silence_ratio if silence_ratio is not None else 0.0) == "speech":
                needs_fallback = True
            _log_attempt(default_model, primary_result, needs_fallback,
                         chunk_duration_ms, silence_ratio)
        except Exception as e:
            log_warn("gemini_transcribe_chunk", f"{default_model} failed: {redact_secrets(str(e))}")
            needs_fallback = True

        if needs_fallback:
            log_start(f"Falling back to {fallback_model} for chunk")
            try:
                fallback_result = _generate(client, fallback_model, gemini_file, prompt)
                _log_attempt(fallback_model, fallback_result, True, chunk_duration_ms, silence_ratio)
                fallback_usable = _usable(fallback_result, chunk_duration_ms)
                if not fallback_usable:
                    if primary_usable:
                        log_warn(
                            "verification_fallback_unusable",
                            f"{fallback_model} verification was unusable; retaining valid {default_model} result."
                        )
                        transcript, used_model = primary_result.text, default_model
                    else:
                        log_warn("quality_check", f"{fallback_model} also produced poor quality transcript.")
                        raise TranscriptionUnavailableError(
                            f"Both {default_model} and {fallback_model} produced poor quality transcript."
                        )
                elif primary_usable:
                    # For verification (rather than primary failure), retain a valid
                    # primary unless the second opinion is materially more complete.
                    primary_words = len(primary_result.text.split())
                    fallback_words = len(fallback_result.text.split())
                    if fallback_words >= primary_words + 8 and fallback_words >= primary_words * 1.5:
                        transcript, used_model = fallback_result.text, fallback_model
                    else:
                        transcript, used_model = primary_result.text, default_model
                else:
                    transcript, used_model = fallback_result.text, fallback_model
            except Exception as e:
                if primary_usable:
                    log_warn(
                        "verification_fallback_failed",
                        f"{fallback_model} verification failed; retaining valid {default_model} result: "
                        f"{redact_secrets(str(e))}"
                    )
                    transcript, used_model = primary_result.text, default_model
                elif isinstance(e, TranscriptionUnavailableError):
                    raise
                else:
                    log_error("gemini_fallback_chunk", f"{fallback_model} failed: {redact_secrets(str(e))}")
                    if not is_transient_error(e):
                        raise e
                    raise TranscriptionUnavailableError(f"Both default and fallback models failed to transcribe chunk: {str(e)}") from e

        if not transcript or not transcript.strip():
            raise TranscriptionUnavailableError("Both default and fallback models returned empty transcript text.")

        return transcript, used_model
        
    finally:
        if gemini_file and getattr(gemini_file, 'name', None):
            try:
                client.files.delete(name=gemini_file.name)
            except Exception as ex:
                log_warn("gemini_cleanup_remote", f"Could not delete remote Gemini file: {redact_secrets(str(ex))}")

def save_progress_checkpoint(progress_file: str, state_data: dict):
    """
    Saves progress checkpoint atomically using .tmp, flush, fsync, os.replace.
    Retries up to 3 attempts with calculate_backoff for PermissionError/OSError.
    Does not sleep after 3rd attempt.
    Raises ProgressPersistenceError if checkpoint save fails after 3 attempts.
    """
    temp_prog = f"{progress_file}.tmp"
    last_err = None
    for attempt in range(1, 4):
        try:
            with open(temp_prog, "w", encoding="utf-8") as f:
                json.dump(state_data, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_prog, progress_file)
            return
        except (PermissionError, OSError) as ex:
            last_err = ex
            if os.path.exists(temp_prog):
                try:
                    os.remove(temp_prog)
                except Exception:
                    pass
            if attempt < 3:
                sleep_dur = calculate_backoff(attempt, base_delay=1.0, max_delay=5.0)
                log_warn("progress_save", f"Attempt {attempt}/3 failed saving progress checkpoint: {redact_secrets(str(ex))}. Retrying in {sleep_dur:.1f}s...")
                time.sleep(sleep_dur)
    raise ProgressPersistenceError(f"Failed to save progress checkpoint after 3 attempts: {str(last_err)}") from last_err

def load_and_validate_progress_checkpoint(
    progress_file: str, 
    file_id: str | None, 
    file_name: str | None, 
    base_name: str, 
    total_chunks: int, 
    chunk_duration_ms: int,
    audio_size: int
) -> tuple[int, list, list, int, str]:
    """
    Reads and validates progress checkpoint.
    Returns (last_idx, final_output, used_models, total_words, prev_transcript).
    Raises ProgressPersistenceError if file is locked or unreadable due to OS errors after 3 attempts.
    """
    if not os.path.exists(progress_file):
        return -1, [], [], 0, ""

    raw_data = None
    read_err = None
    for attempt in range(1, 4):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                raw_data = f.read()
            break
        except (PermissionError, OSError) as e:
            read_err = e
            if attempt < 3:
                sleep_dur = calculate_backoff(attempt, base_delay=1.0, max_delay=5.0)
                log_warn("progress_read", f"Attempt {attempt}/3 failed reading progress checkpoint: {redact_secrets(str(e))}. Retrying in {sleep_dur:.1f}s...")
                time.sleep(sleep_dur)

    if raw_data is None:
        raise ProgressPersistenceError(f"Failed to read progress file due to OS lock after 3 attempts: {str(read_err)}") from read_err

    try:
        state = json.loads(raw_data)
        if not isinstance(state, dict):
            raise ValueError("Progress content is not a dict.")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        base_p, ext_p = os.path.splitext(progress_file)
        corrupt_p = f"{base_p}.corrupt_{timestamp_str}{ext_p or '.json'}"
        try:
            if os.path.exists(progress_file):
                os.replace(progress_file, corrupt_p)
                log_warn("progress_load", f"Renamed corrupt progress file to {corrupt_p}")
        except Exception as ren_err:
            log_warn("progress_load", f"Failed to rename corrupt progress file: {redact_secrets(str(ren_err))}")
        log_warn("progress_load", f"Could not load corrupt progress file ({type(e).__name__}): {redact_secrets(str(e))}. Starting fresh.")
        return -1, [], [], 0, ""

    full_v1_keys = {"source_file_id", "original_name", "total_chunks", "chunk_duration_ms", "audio_size", "last_completed_chunk", "last_idx", "final_output", "used_models"}
    modern_keys = {"total_chunks", "chunk_duration_ms", "audio_size"}

    schema_ver = state.get("schema_version")
    state_keys = set(state.keys())

    is_schema_2 = (schema_ver == 2)
    is_full_v1 = (schema_ver is None and full_v1_keys.issubset(state_keys))
    has_modern_fields = bool(modern_keys.intersection(state_keys))

    incompatible_reason = None

    if is_schema_2 or is_full_v1:
        ckpt_file_id = state.get("source_file_id")
        ckpt_name = state.get("original_name")
        ckpt_total_chunks = state.get("total_chunks")
        ckpt_chunk_duration_ms = state.get("chunk_duration_ms")
        ckpt_audio_size = state.get("audio_size")
        ckpt_completed = state.get("last_completed_chunk")
        ckpt_last_idx = state.get("last_idx")
        ckpt_output = state.get("final_output")
        ckpt_models = state.get("used_models")

        curr_has_id = bool(file_id and isinstance(file_id, str) and file_id.strip())

        if curr_has_id and (not isinstance(ckpt_file_id, str) or not ckpt_file_id.strip() or ckpt_file_id != file_id):
            incompatible_reason = f"source_file_id mismatch: expected '{file_id}', found '{ckpt_file_id}'"
        elif not isinstance(ckpt_name, str) or not ckpt_name.strip() or (file_name and ckpt_name != file_name):
            incompatible_reason = f"original_name mismatch: expected '{file_name or base_name}', found '{ckpt_name}'"
        elif not isinstance(ckpt_total_chunks, int) or ckpt_total_chunks <= 0 or ckpt_total_chunks != total_chunks:
            incompatible_reason = f"total_chunks mismatch: expected {total_chunks}, found {ckpt_total_chunks}"
        elif not isinstance(ckpt_chunk_duration_ms, int) or ckpt_chunk_duration_ms <= 0 or ckpt_chunk_duration_ms != chunk_duration_ms:
            incompatible_reason = f"chunk_duration_ms mismatch: expected {chunk_duration_ms}, found {ckpt_chunk_duration_ms}"
        elif not isinstance(ckpt_audio_size, int) or ckpt_audio_size < 0 or (audio_size > 0 and ckpt_audio_size != audio_size):
            incompatible_reason = f"audio_size mismatch: expected {audio_size}, found {ckpt_audio_size}"
        elif not isinstance(ckpt_completed, int) or not (0 <= ckpt_completed <= total_chunks):
            incompatible_reason = f"last_completed_chunk out of range [0, {total_chunks}]: found {ckpt_completed}"
        elif not isinstance(ckpt_last_idx, int) or ckpt_last_idx != ckpt_completed - 1:
            incompatible_reason = f"last_idx ({ckpt_last_idx}) inconsistent with last_completed_chunk ({ckpt_completed})"
        elif not isinstance(ckpt_output, list):
            incompatible_reason = f"final_output is not a list: found {type(ckpt_output).__name__}"
        elif not isinstance(ckpt_models, list):
            incompatible_reason = f"used_models is not a list: found {type(ckpt_models).__name__}"

    elif has_modern_fields:
        missing_keys = sorted(list(full_v1_keys - state_keys))
        incompatible_reason = f"incomplete modern checkpoint (missing keys: {missing_keys})"

    else:
        allowed_legacy_keys = {"source_file_id", "original_name", "last_completed_chunk", "last_idx", "final_output", "used_models", "total_words", "prev_transcript"}
        extra_keys = state_keys - allowed_legacy_keys
        if extra_keys:
            incompatible_reason = f"unrecognized checkpoint format with extra keys: {sorted(list(extra_keys))}"
        else:
            ckpt_file_id = state.get("source_file_id")
            ckpt_name = state.get("original_name")
            ckpt_completed = state.get("last_completed_chunk")
            ckpt_last_idx = state.get("last_idx")
            ckpt_output = state.get("final_output")
            ckpt_models = state.get("used_models")
            ckpt_words = state.get("total_words")
            ckpt_prev = state.get("prev_transcript")

            if "source_file_id" in state:
                if not isinstance(ckpt_file_id, str) or not ckpt_file_id.strip():
                    incompatible_reason = f"legacy source_file_id invalid type or empty: found {ckpt_file_id!r}"
                elif file_id and isinstance(file_id, str) and file_id.strip() and ckpt_file_id != file_id:
                    incompatible_reason = f"legacy source_file_id mismatch: expected '{file_id}', found '{ckpt_file_id}'"

            if not incompatible_reason and "original_name" in state:
                if not isinstance(ckpt_name, str) or not ckpt_name.strip():
                    incompatible_reason = f"legacy original_name invalid type or empty: found {ckpt_name!r}"
                elif file_name and ckpt_name != file_name and base_name not in ckpt_name:
                    incompatible_reason = f"legacy original_name mismatch: expected '{file_name or base_name}', found '{ckpt_name}'"

            if not incompatible_reason:
                if not isinstance(ckpt_last_idx, int) or ckpt_last_idx < -1 or ckpt_last_idx >= total_chunks:
                    incompatible_reason = f"legacy last_idx out of range [-1, {total_chunks-1}]: found {ckpt_last_idx}"
                elif ckpt_completed is not None and (not isinstance(ckpt_completed, int) or not (0 <= ckpt_completed <= total_chunks)):
                    incompatible_reason = f"legacy last_completed_chunk out of range [0, {total_chunks}]: found {ckpt_completed}"
                elif ckpt_completed is not None and ckpt_last_idx is not None and ckpt_last_idx != ckpt_completed - 1:
                    incompatible_reason = f"legacy last_idx ({ckpt_last_idx}) inconsistent with last_completed_chunk ({ckpt_completed})"
                elif not isinstance(ckpt_output, list):
                    incompatible_reason = f"legacy final_output is not a list: found {type(ckpt_output).__name__}"
                elif not isinstance(ckpt_models, list):
                    incompatible_reason = f"legacy used_models is not a list: found {type(ckpt_models).__name__}"
                elif ckpt_words is not None and (not isinstance(ckpt_words, int) or ckpt_words < 0):
                    incompatible_reason = f"legacy total_words invalid: found {ckpt_words}"
                elif ckpt_prev is not None and not isinstance(ckpt_prev, str):
                    incompatible_reason = f"legacy prev_transcript invalid: found {type(ckpt_prev).__name__}"

    if incompatible_reason:
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        base_p, ext_p = os.path.splitext(progress_file)
        incomp_p = f"{base_p}.incompatible_{timestamp_str}{ext_p or '.json'}"
        try:
            if os.path.exists(progress_file):
                os.replace(progress_file, incomp_p)
                log_warn("progress_load", f"Checkpoint metadata incompatible with current file ({incompatible_reason}). Backed up to {incomp_p}. Starting fresh.")
        except Exception as ren_err:
            log_warn("progress_load", f"Failed to rename incompatible progress file: {redact_secrets(str(ren_err))}")
        return -1, [], [], 0, ""

    if is_full_v1 or not is_schema_2:
        log_ok(f"Migrating checkpoint for {file_name or base_name} to schema_version 2.")

    last_completed = state.get("last_completed_chunk", state.get("last_idx", -1) + 1)
    last_idx = state.get("last_idx", last_completed - 1)
    final_output = state.get("final_output", [])
    used_models = state.get("used_models", [])
    total_words = state.get("total_words", 0)
    prev_transcript = state.get("prev_transcript", "")

    return last_idx, final_output, used_models, total_words, prev_transcript

def transcribe_audio(local_path: str, use_pro_model: bool = False, file_id: str = None,
                     file_name: str = None, state_backend=None, source: str = "drive") -> tuple[str, str]:
    """Transcribe sequential 60-second ffmpeg extracts; never decode the full source."""
    client = genai.Client(api_key=Config.GEMINI_API_KEY)
    base_name = os.path.splitext(os.path.basename(local_path))[0]
    progress_file = os.path.join(Config.TEMP_DIR, f"{base_name}_progress.json")
    duration_seconds = probe_audio_duration(local_path)
    boundaries = audio_chunk_boundaries(duration_seconds)
    total_ms = math.ceil(duration_seconds * 1000)
    chunk_length_ms = CHUNK_DURATION_SECONDS * 1000
    log_ok(f"Source duration={duration_seconds:.3f}s; total_chunks={len(boundaries)}")
    prompt = (
        "Transcribe every intelligible spoken word from the beginning through the end of this audio chunk. "
        "Do not summarize or omit later speakers or later portions. Continue through speaker changes, "
        "music beds, jingles, and advertisement-style delivery. Never stop after only the first sentence "
        "when more intelligible speech follows. Output only transcript text, with no introduction, bullets, "
        "or formatting. Clean only obvious stutters."
    )
    audio_size = os.path.getsize(local_path) if os.path.exists(local_path) else total_ms
    cloud_chunks = state_backend is not None and getattr(state_backend, "backend_name", None) == "firestore"
    source_key = make_source_key(source, file_id) if file_id else None
    if cloud_chunks:
        state_backend.ensure_job(source_key, source=source, source_id=file_id,
                                 name=file_name or base_name, total_chunks=len(boundaries))
        saved = state_backend.list_completed_chunks(source_key)
        contiguous = []
        for expected, item in enumerate(saved):
            if item.get("chunk_index") != expected:
                break
            contiguous.append(item)
        last_idx = len(contiguous) - 1
        final_output, used_models, total_words, prev_transcript = [], [], 0, ""
        for item in contiguous:
            final_output.extend(item.get("output_lines", []))
            model = item.get("model_used")
            if model and model not in used_models and model != "local-filter": used_models.append(model)
            total_words += item.get("word_count", 0)
            if item.get("prev_transcript"): prev_transcript = item["prev_transcript"]
    else:
        last_idx, final_output, used_models, total_words, prev_transcript = load_and_validate_progress_checkpoint(
            progress_file, file_id, file_name, base_name, len(boundaries), chunk_length_ms, audio_size)
    completed = last_idx + 1
    log_ok(f"Completed/resumed chunks={completed}/{len(boundaries)}")
    if completed:
        log_ok("Resuming post-transcription pipeline." if completed == len(boundaries)
               else f"Resuming {file_name or base_name} from chunk {completed + 1}/{len(boundaries)}.")
    default_model, fallback_model = ((Config.GEMINI_FALLBACK_MODEL, Config.GEMINI_MODEL)
                                     if use_pro_model else
                                     (Config.GEMINI_MODEL, Config.GEMINI_FALLBACK_MODEL))
    for idx, start_seconds, end_seconds in boundaries:
        if idx <= last_idx:
            continue
        if cloud_chunks:
            if not state_backend.renew_lease(source_key, state_backend.owner_id,
                                             state_backend.lease_ttl_seconds):
                from state_backend import LeaseLostError
                raise LeaseLostError(f"Lease lost before chunk {idx}")
            renew_global = getattr(state_backend, "renew_global_cycle_lease_or_raise", None)
            if renew_global: renew_global()
        chunk_duration_ms = max(1, round((end_seconds - start_seconds) * 1000))
        start_str, end_str = format_time(int(start_seconds)), format_time(math.ceil(end_seconds))
        time_header = f"[{start_str}-{end_str}]"
        # A stable source identity, rather than the Unicode display name, owns every
        # file that may be uploaded. Full SHA-256 keys avoid prefix collisions.
        working_id = file_id or os.path.abspath(local_path)
        chunk_path = os.path.join(
            Config.TEMP_DIR, gemini_working_basename(source, working_id, idx))
        enhanced_path = os.path.join(
            Config.TEMP_DIR, gemini_working_basename(source, working_id, idx, enhanced=True))
        chunk = None
        output_start = len(final_output)
        chunk_model = "local-filter"
        try:
            log_start(f"Processing chunk {idx + 1}/{len(boundaries)} {time_header}; "
                      f"start={start_seconds:.3f}s end={end_seconds:.3f}s")
            extract_audio_chunk(local_path, chunk_path, start_seconds, end_seconds - start_seconds)
            chunk_temp_size = os.path.getsize(chunk_path) if os.path.exists(chunk_path) else 0
            log_ok(f"[{time_header}] Chunk temp size={chunk_temp_size} bytes")
            if cloud_chunks:
                if not state_backend.renew_lease(source_key, state_backend.owner_id,
                                                 state_backend.lease_ttl_seconds):
                    from state_backend import LeaseLostError
                    raise LeaseLostError(f"Lease lost after extraction for chunk {idx}")
                if renew_global: renew_global()
            chunk = AudioSegment.from_file(chunk_path)
            silences = detect_silence(chunk, min_silence_len=SILENCE_MIN_DURATION_MS,
                                      silence_thresh=SILENCE_NOISE_DB)
            silence_ratio = sum(stop - begin for begin, stop in silences) / len(chunk) if len(chunk) else 1.0
            log_ok(f"[{time_header}] Silence ratio: {silence_ratio:.0%}")
            block_class = classify_audio_block(silence_ratio)
            if block_class in ("silence", "weak"):
                label = "[silencio]" if block_class == "silence" else "[audio poco inteligible]"
                final_output.extend([time_header, label, ""])
            else:
                command = ["ffmpeg", "-y", "-v", "error", "-i", chunk_path, "-af",
                           FFMPEG_SPEECH_ENHANCEMENT_FILTER, enhanced_path]
                upload_path = chunk_path
                try:
                    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    upload_path = enhanced_path
                except Exception as exc:
                    log_warn("ffmpeg_preprocess", f"[{time_header}] Preprocessing failed; using extracted chunk: {redact_secrets(str(exc))}")
                if cloud_chunks:
                    if not state_backend.renew_lease(source_key, state_backend.owner_id,
                                                     state_backend.lease_ttl_seconds):
                        from state_backend import LeaseLostError
                        raise LeaseLostError(f"Lease lost before Gemini for chunk {idx}")
                    if renew_global: renew_global()
                transcript, model = transcribe_chunk(client, upload_path, default_model,
                                                     fallback_model, prompt, chunk_duration_ms,
                                                     silence_ratio)
                chunk_model = model
                if model not in used_models: used_models.append(model)
                transcript = transcript.strip() if transcript else ""
                if silence_ratio >= DEFENSIVE_HALLUCINATION_RATIO and len(transcript.split()) > 20:
                    transcript = "[audio poco inteligible]"
                if transcript and not is_duplicate_block(prev_transcript, transcript):
                    final_output.extend([time_header, transcript, ""])
                    total_words += len(transcript.split())
                    if transcript not in ("[silencio]", "[audio poco inteligible]"):
                        prev_transcript = transcript
            last_idx = idx
            state_data = {"schema_version": 2, "source_file_id": file_id or "",
                "original_name": file_name or base_name, "total_chunks": len(boundaries),
                "chunk_duration_ms": chunk_length_ms, "last_completed_chunk": idx + 1,
                "last_idx": idx, "audio_size": audio_size, "final_output": final_output,
                "used_models": used_models, "total_words": total_words,
                "prev_transcript": prev_transcript}
            if cloud_chunks:
                output_lines = final_output[output_start:]
                state_backend.save_chunk(source_key, idx, "\n".join(output_lines), chunk_model,
                    sum(len(line.split()) for line in output_lines if not line.startswith("[")),
                    owner_id=state_backend.owner_id, output_lines=output_lines,
                    prev_transcript=prev_transcript, total_chunks=len(boundaries),
                    audio_size=audio_size, chunk_duration_ms=chunk_length_ms)
                if not state_backend.renew_lease(source_key, state_backend.owner_id,
                                                 state_backend.lease_ttl_seconds):
                    from state_backend import LeaseLostError
                    raise LeaseLostError(f"Lease lost after chunk {idx}")
                if renew_global: renew_global()
            else:
                save_progress_checkpoint(progress_file, state_data)
        finally:
            chunk = None
            for path in (chunk_path, enhanced_path):
                if os.path.exists(path):
                    try: os.remove(path)
                    except Exception as exc:
                        log_warn("cleanup", f"Could not remove chunk temp file: {redact_secrets(str(exc))}")
        if idx < len(boundaries) - 1:
            time.sleep(Config.CHUNK_SLEEP_SECONDS)
    total_minutes = duration_seconds / 60
    if total_minutes and total_words / total_minutes > 500:
        log_warn("validation", "WARNING: Abnormally long transcript speech rate; possible hallucination loop.")
    return "\n".join(final_output), ", ".join(used_models) if used_models else default_model