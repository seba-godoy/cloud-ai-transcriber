# Architecture

This document explains the **sanitized portfolio snapshot**. It intentionally omits private production infrastructure details, resource IDs, operational incident history, and secrets.

## Design objective

Automatically process long recordings with bounded memory usage while tolerating temporary network failures, interrupted executions, and retries without duplicating completed work.

## Data flow

1. **Ingest:** Google Drive folder polling, Telegram audio/media and messages, or a YouTube URL.
2. **Identify:** Build a source-specific identity to determine whether the media was already processed.
3. **Coordinate:** Acquire durable state/lease ownership for cloud runs. The legacy local runtime has a separate local state implementation.
4. **Extract:** Inspect the audio stream with FFprobe. Extract individual bounded PCM chunks with FFmpeg; avoid loading an entire long recording into RAM.
5. **Transcribe:** Apply optional audio enhancement and call Gemini. Treat transcription quality as a separate concern from network/API success.
6. **Checkpoint:** Persist per-chunk results before advancing so interrupted work can resume without starting over.
7. **Publish:** Assemble DOCX/PDF, use idempotent uploads to Drive, and confirm notification state.
8. **Operate:** Track state and progress with Firestore, Telegram commands, and a separate read-only dashboard.

## Major modules

| Component | Implementation | Responsibility |
| --- | --- | --- |
| Orchestration | `main.py`, `phase12_entrypoint.py` | Local continuous and cloud one-shot modes |
| Configuration | `config.py` | Environment-based settings, validation, logical owner routing |
| Drive | `drive_client.py`, `drive_watcher.py`, `drive_uploader.py` | Authenticated ingestion and idempotent output |
| Telegram | `telegram_notifier.py`, `telegram_commands.py` | Authorized input, notifications, and status commands |
| YouTube | `youtube_ingest.py` | Media discovery, extraction and fallback routes |
| Transcription | `gemini_transcriber.py` | Audio chunking, model calls, quality safeguards |
| Recovery | `long_media_runtime.py`, `quality_rescue_runtime.py`, `retry_epoch_runtime.py` | Long-media and retry/recovery boundaries |
| Persistence | `state_store.py`, `state_backend.py` | Local state, Firestore state, checkpoints, leases |
| Presentation | `docx_writer.py`, `pdf_writer.py` | Documents |
| Status UI | `dashboard_app.py`, `dashboard/index.html` | Read-only job summaries |

## Run modes and guarantees

- **Local:** Continuous polling, local state and file checkpoints for legacy compatibility.
- **Cloud-oriented:** One-shot execution suitable for Cloud Run Jobs; Firestore stores durable job state. Cloud Scheduler or an external trigger starts work.
- **Concurrency:** Source-level and global lease checks reduce overlapping execution.
- **Idempotency:** Source-derived keys, chunk indexing and Drive output IDs protect against duplicated processing.
- **Recovery:** Retryable and permanent failures are distinguished; interrupted chunks can be resumed.
- **Data lifecycle:** Temporary media is excluded from source control. Treat recordings/transcripts as private user content.

## Privacy caveat

The source code is a **portfolio demonstration** of the architecture, not a turnkey publicly accessible hosted service. Any new deployment requires independent setup, credentials, IAM, secure OAuth scopes, rate and cost controls, and end-to-end security review. The actual operational environment is not reproduced here.
