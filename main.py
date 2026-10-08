import time
import os
import sys
import traceback
import math
import re
import html
import subprocess
from typing import Literal
from pydub import AudioSegment  # compatibility import; duration probing never uses it

from config import Config
from state_store import StateStore
from state_backend import LeaseLostError, StateBackend, create_state_backend
from drive_watcher import list_pending_audio_files, download_file
from gemini_transcriber import transcribe_audio, probe_audio_duration
from docx_writer import create_docx
from pdf_writer import create_pdf
from googleapiclient.errors import HttpError
from drive_client import get_drive_service
from drive_uploader import upload_file_to_drive, generate_drive_file_id
from telegram_notifier import (
    send_telegram_message, 
    get_telegram_updates,
    get_telegram_file_info,
    download_telegram_file,
    send_long_telegram_message,
    is_authorized_telegram_message
)
from youtube_ingest import get_youtube_metadata, download_youtube_audio
from utils import (
    log_start, 
    log_ok, 
    log_error, 
    log_warn, 
    sanitize_filename, 
    is_transient_error, 
    is_network_or_dns_error,
    redact_secrets,
    make_source_key,
    StatePersistenceError,
    TranscriptionUnavailableError,
    DriveIntegrityError,
    TelegramAPIError,
    TelegramDeliveryError,
    DriveUploadUnconfirmedError,
    SingleInstanceLock
)

def _renew_processing_lease_or_raise(state_store, source_key: str) -> None:
    if getattr(state_store, "backend_name", None) != "firestore":
        return
    if not state_store.renew_lease(source_key, state_store.owner_id, state_store.lease_ttl_seconds):
        raise LeaseLostError(f"Lease lost while producing outputs for {source_key}")
    state_store.renew_global_cycle_lease_or_raise()

def _renew_global_cycle_lease_or_raise(state_store) -> None:
    renew = getattr(state_store, "renew_global_cycle_lease_or_raise", None)
    if renew:
        renew()

def handle_processing_failure(file_id: str, file_name: str, source: Literal["drive", "telegram", "youtube"], error: Exception, state_store: StateStore | StateBackend):
    """
    Centralized failure handler for non-fatal processing errors.
    StatePersistenceError is NEVER handled here (it must terminate the process).
    """
    if isinstance(error, StatePersistenceError):
        raise error

    err_msg = str(error)
    redacted_msg = redact_secrets(err_msg)
    safe_name = html.escape(file_name, quote=True)
    is_net_err = is_network_or_dns_error(error)
    is_trans = is_transient_error(error)
    
    if is_trans:
        if is_net_err:
            log_warn("transient_network_failure", f"DNS/network unavailable while processing {file_name}. Progress preserved. File marked retryable.")
        else:
            log_warn("processing_failed_transient", f"Transient error for {file_name} [{source}]: {redacted_msg}")
            
        state_store.mark_retryable(file_id, file_name, source, err_msg, is_network_error=is_net_err)
        
        try:
            if source == "drive":
                send_telegram_message(f"⚠️ <b>Aviso de procesamiento:</b> Error temporal al procesar <code>{safe_name}</code>. Se reintentará automáticamente.", parse_mode="HTML")
            else:
                send_telegram_message(f"⚠️ <b>Aviso:</b> Ocurrió un error temporal al procesar <code>{safe_name}</code>. Puedes reenviar el archivo o enlace para reintentar.", parse_mode="HTML")
        except Exception as tg_err:
            log_warn("telegram_notification_skipped", f"Failed to send Telegram notification during transient failure for {file_name}: {redact_secrets(str(tg_err))}")
    else:
        log_error("processing_failed_permanent", f"Permanent failure for {file_name} [{source}]: {redacted_msg}")
        state_store.mark_permanent_failed(file_id, file_name, err_msg, source=source)
        try:
            if source == "drive":
                send_telegram_message(f"❌ <b>Error de transcripción:</b> No se pudo procesar <code>{safe_name}</code>.", parse_mode="HTML")
            else:
                send_telegram_message(f"❌ <b>Error:</b> No se pudo procesar <code>{safe_name}</code>. Puedes intentar reenviarlo.", parse_mode="HTML")
        except Exception as tg_err:
            log_warn("telegram_notification_skipped", f"Failed to send Telegram notification during permanent failure for {file_name}: {redact_secrets(str(tg_err))}")

def verify_drive_file_exists(drive_id: str, file_id: str, source: str, doc_type: str, state_store: StateStore) -> tuple[bool, str | None]:
    """
    Verifies if a partial Drive upload ID is valid.
    Only starts recovery via appProperties when files.get returns an HttpError 404, file is trashed, or properties mismatch.
    Raises all other errors (429, 500, 503, 401, 403, network errors, etc.) directly.
    """
    if not drive_id:
        return False, None

    service = get_drive_service()
    source_key = make_source_key(source, file_id)
    
    should_search_fallback = False
    try:
        file_obj = service.files().get(
            fileId=drive_id, 
            fields='id, trashed, parents, appProperties'
        ).execute()
        
        if file_obj and not file_obj.get('trashed', False):
            parents = file_obj.get('parents', [])
            props = file_obj.get('appProperties', {})
            if (Config.DRIVE_OUTPUT_FOLDER_ID in parents and 
                props.get('source_key') == source_key and 
                props.get('document_type') == doc_type):
                return True, drive_id
            else:
                should_search_fallback = True
        else:
            should_search_fallback = True
    except HttpError as e:
        status_code = getattr(e.resp, 'status', None) or getattr(e, 'status_code', None)
        if status_code == 404:
            should_search_fallback = True
        else:
            raise
    except Exception:
        raise

    if not should_search_fallback:
        return False, None

    log_warn("drive_verify", f"Partial ID {drive_id} invalid or 404 for {doc_type}. Searching Drive via appProperties...")
    
    query = (
        f"'{Config.DRIVE_OUTPUT_FOLDER_ID}' in parents "
        "and trashed = false "
        f"and appProperties has {{ key='source_key' and value='{source_key}' }} "
        f"and appProperties has {{ key='document_type' and value='{doc_type}' }}"
    )
    
    matching_files = []
    page_token = None
    
    while True:
        res = service.files().list(
            q=query,
            fields='nextPageToken, files(id, name, appProperties)',
            pageToken=page_token
        ).execute()
        
        matching_files.extend(res.get('files', []))
        page_token = res.get('nextPageToken')
        if not page_token:
            break
            
    if len(matching_files) == 1:
        repaired_id = matching_files[0]['id']
        log_ok(f"Found matching output file on Drive ({repaired_id}). Repairing state...")
        state_store.save_partial_upload(file_id, doc_type, repaired_id)
        return True, repaired_id
    elif len(matching_files) > 1:
        raise DriveIntegrityError(f"Multiple matching files found on Drive for source_key {source_key} and doc_type {doc_type}.")
        
    state_store.remove_partial_upload(file_id, doc_type)
    return False, None

def execute_pipeline(
    local_audio_path: str, 
    file_name: str, 
    file_id: str, 
    state_store: StateStore | StateBackend,
    *, 
    source: Literal["drive", "telegram", "youtube"], 
    is_telegram_direct: bool = False
) -> tuple[bool, str]:
    """
    Core transcription pipeline. Accepts explicit source ('drive', 'telegram', 'youtube').
    Returns (success_flag, transcript_text).
    """
    if source not in ("drive", "telegram", "youtube"):
        raise ValueError(f"Invalid source '{source}'. Must be one of ('drive', 'telegram', 'youtube').")
        
    docx_path = None
    pdf_path = None
    original_mp4_path = None
    safe_file_name = html.escape(file_name, quote=True)
    source_key = make_source_key(source, file_id)
    
    try:
        if local_audio_path.lower().endswith('.mp4'):
            log_start(f"MP4 file detected: {file_name}")
            
            probe_command = [
                "ffprobe", "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                local_audio_path
            ]
            try:
                probe_result = subprocess.run(probe_command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                audio_streams = probe_result.stdout.strip()
                if not audio_streams:
                    raise ValueError("No valid audio track found in the video file.")
                log_ok(f"Audio track detected: {audio_streams.splitlines()[0]}")
            except Exception as e:
                log_error("mp4_probe_failed", f"Video validation failed: {redact_secrets(str(e))}")
                raise ValueError(f"Video validation failed (no audio track or corrupt file): {str(e)}")
                
            suffix = file_id[:8] if file_id else ""
            extracted_filename = sanitize_filename(f"{os.path.splitext(file_name)[0]}_extracted.mp3", suffix=suffix)
            audio_extracted_path = os.path.join(Config.TEMP_DIR, extracted_filename)
            
            log_start(f"Extracting audio for {file_name}")
            command = [
                "ffmpeg", "-y", "-i", local_audio_path,
                "-vn", "-map", "0:a:0",
                "-ac", "1", "-ar", "16000",
                audio_extracted_path
            ]
            try:
                subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                log_ok(f"Audio converted to working format: {os.path.basename(audio_extracted_path)}")
                original_mp4_path = local_audio_path
                local_audio_path = audio_extracted_path
                log_start(f"Normal pipeline started for {file_name}")
            except Exception as e:
                log_error("mp4_extraction_failed", f"Failed to extract audio from {file_name}: {redact_secrets(str(e))}")
                raise Exception(f"Audio extraction from video failed: {str(e)}")

        log_start(f"Transcription started for {file_name}")
        
        estimate_message = None
        try:
            total_ms = math.ceil(probe_audio_duration(local_audio_path) * 1000)
            duration_min = round(total_ms / 60000.0, 1)
            num_blocks = max(1, math.ceil(total_ms / 60000))
            
            min_est_min = max(0.1, round((num_blocks * 15) / 60.0, 1))
            max_est_min = max(0.2, round((num_blocks * 40) / 60.0, 1))
            
            estimate_message = (
                f"⏱ <b>Tiempo estimado para \"{safe_file_name}\":</b>\n"
                f"• Duración del audio: {duration_min} min\n"
                f"• Bloques a procesar: {num_blocks}\n"
                f"• Tiempo estimado: {min_est_min} - {max_est_min} minutos"
            )
        except Exception as est_err:
            duration_min = 999.0
            log_warn("estimation", f"Could not calculate exact time: {redact_secrets(str(est_err))}")

        if estimate_message is not None:
            should_notify = True
            claim = getattr(state_store, "claim_notification", None)
            if source == "drive" and claim:
                # This is durable state, not best-effort notification delivery.
                # In particular, StatePersistenceError must reach the pipeline's
                # fatal persistence handler below.
                should_notify = claim(source_key, "estimate")
            if should_notify:
                try:
                    send_telegram_message(estimate_message, parse_mode="HTML")
                except Exception as notify_err:
                    log_warn("telegram_notification_skipped", f"Failed to send estimate message: {redact_secrets(str(notify_err))}")
            
        is_short_audio = duration_min < 5.0
        is_short_telegram = is_telegram_direct and is_short_audio
        if is_short_audio:
            log_ok(f"Short audio: using high-precision model ({Config.GEMINI_FALLBACK_MODEL})")
            
        if hasattr(state_store, "bind_source"):
            state_store.bind_source(source, file_id, file_name)
        transcript, used_model = transcribe_audio(
            local_audio_path, use_pro_model=is_short_audio, file_id=file_id,
            file_name=file_name, state_backend=state_store, source=source)
        if not transcript or not transcript.strip():
            raise TranscriptionUnavailableError("Transcription returned empty text.")
            
        log_ok(f"Transcription finished using {used_model}")
        _renew_processing_lease_or_raise(state_store, source_key)
        
        if is_telegram_direct and duration_min < 5.0:
            log_ok(f"Audio < 5 mins, skipping Drive and sending text directly")
            header = f"✅ Transcripción completada ({file_name}):\n\n"
            full_msg = header + transcript
            delivered = send_long_telegram_message(full_msg, parse_mode=None, required=True)
            if delivered:
                _renew_processing_lease_or_raise(state_store, source_key)
                state_store.mark_processed(file_id, file_name, used_model)
                base_name = os.path.splitext(os.path.basename(local_audio_path))[0]
                progress_file = os.path.join(Config.TEMP_DIR, f"{base_name}_progress.json")
                if os.path.exists(progress_file):
                    try:
                        os.remove(progress_file)
                    except Exception:
                        pass
                return True, transcript

        log_start(f"Generating documents for {file_name}")
        _renew_processing_lease_or_raise(state_store, source_key)
        docx_path = create_docx(transcript, file_name)
        _renew_processing_lease_or_raise(state_store, source_key)
        pdf_path = create_pdf(transcript, file_name)
        log_ok("Documents generated")
        
        log_start(f"Uploading documents for {file_name}")
        partials = state_store.get_partial_uploads(file_id)
        
        # 1. DOCX Upload
        docx_drive_id = partials.get("docx")
        verified_docx, docx_drive_id = verify_drive_file_exists(docx_drive_id, file_id, source, "docx", state_store)
        
        if not verified_docx or not docx_drive_id:
            _renew_processing_lease_or_raise(state_store, source_key)
            docx_drive_id = generate_drive_file_id()
            state_store.save_partial_upload(file_id, "docx", docx_drive_id)
            upload_file_to_drive(
                docx_path, 
                mime_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                source_key=source_key,
                doc_type="docx",
                preallocated_id=docx_drive_id
            )
            _renew_processing_lease_or_raise(state_store, source_key)
            
        # 2. PDF Upload
        pdf_drive_id = partials.get("pdf")
        verified_pdf, pdf_drive_id = verify_drive_file_exists(pdf_drive_id, file_id, source, "pdf", state_store)
        
        if not verified_pdf or not pdf_drive_id:
            _renew_processing_lease_or_raise(state_store, source_key)
            pdf_drive_id = generate_drive_file_id()
            state_store.save_partial_upload(file_id, "pdf", pdf_drive_id)
            upload_file_to_drive(
                pdf_path, 
                mime_type='application/pdf',
                source_key=source_key,
                doc_type="pdf",
                preallocated_id=pdf_drive_id
            )
            _renew_processing_lease_or_raise(state_store, source_key)
            
        log_ok(f"Documents uploaded successfully. DOCX ID: {docx_drive_id}, PDF ID: {pdf_drive_id}")
        
        if source == "youtube":
            _renew_processing_lease_or_raise(state_store, source_key)
            state_store.save_youtube_transcript(file_id, file_name, transcript, docx_drive_id, pdf_drive_id)
        else:
            _renew_processing_lease_or_raise(state_store, source_key)
            state_store.mark_processed(file_id, file_name, used_model, final_ids={"docx": docx_drive_id, "pdf": pdf_drive_id})
        
        base_name = os.path.splitext(os.path.basename(local_audio_path))[0]
        progress_file = os.path.join(Config.TEMP_DIR, f"{base_name}_progress.json")
        if os.path.exists(progress_file):
            try:
                os.remove(progress_file)
            except Exception:
                pass

        if is_telegram_direct:
            try:
                send_telegram_message("✅ Transcripción completada. Documentos disponibles en Drive.")
            except Exception as ex:
                log_warn("telegram_notification_skipped", f"Failed to send completion notification: {redact_secrets(str(ex))}")
        else:
            try:
                send_telegram_message(f"✅ <b>Subida completada</b>\nArchivo: <code>{safe_file_name}</code>", parse_mode="HTML")
            except Exception as ex:
                log_warn("telegram_notification_skipped", f"Failed to send completion notification: {redact_secrets(str(ex))}")
            
        return True, transcript
        
    except StatePersistenceError as spe:
        raise spe
    except TelegramDeliveryError as tde:
        log_warn("execute_pipeline", f"Telegram delivery failed for {file_name}: {redact_secrets(str(tde))}")
        if is_transient_error(tde):
            state_store.mark_retryable(file_id, file_name, source, f"Telegram delivery failed: {str(tde)}")
        else:
            state_store.mark_permanent_failed(file_id, file_name, f"Telegram delivery failed: {str(tde)}", source)
        try:
            send_telegram_message(f"⚠️ No se pudo entregar la transcripción de \"{safe_file_name}\" por Telegram. Puedes reenviar el archivo si deseas intentarlo nuevamente.")
        except Exception as ex:
            log_warn("telegram_notification_skipped", f"Failed to send TelegramDeliveryError notification: {redact_secrets(str(ex))}")
        raise tde
    except Exception as e:
        handle_processing_failure(file_id, file_name, source, e, state_store)
        return False, ""
        
    finally:
        log_start(f"Cleaning up local files for {file_name}")
        for path in [local_audio_path, docx_path, pdf_path, original_mp4_path]:
            if path and os.path.exists(path):
                for attempt in range(1, 4):
                    try:
                        os.remove(path)
                        break
                    except Exception as ex:
                        if attempt == 3:
                            log_warn("cleanup", f"Could not remove {path} after 3 attempts: {redact_secrets(str(ex))}")
                        else:
                            time.sleep(1)

def _acquire_processing_lease(state_store: StateStore | StateBackend, source: str, source_id: str, name: str) -> str | None:
    if getattr(state_store, "backend_name", None) != "firestore":
        return None
    source_key = state_store.bind_source(source, source_id, name)
    if not state_store.acquire_lease(source_key, state_store.owner_id, state_store.lease_ttl_seconds):
        log_warn("lease_contended", f"Skipping {source}:{source_id}; another worker owns it or it is completed.")
        return ""
    return source_key


def _release_processing_lease(state_store: StateStore | StateBackend, source_key: str | None) -> None:
    if source_key:
        state_store.release_lease(source_key, state_store.owner_id)


def process_file(item: dict, state_store: StateStore | StateBackend):
    file_id = item.get('id')
    file_name = item.get('name')
    safe_name = html.escape(file_name, quote=True)
    
    lease_key = _acquire_processing_lease(state_store, "drive", file_id, file_name)
    if lease_key == "": return
    if state_store.is_retry_due(file_id):
        log_start(f"Retrying previously interrupted file: {file_name}")
    else:
        log_start(f"Processing audio: {file_name}")
        
    try:
        claim = getattr(state_store, "claim_notification", None)
        # The claim is a durable operation. Do not place it inside the
        # best-effort Telegram exception boundary.
        should_notify = not claim or claim(lease_key, "initial")
        if should_notify:
            try:
                send_telegram_message(f"🎙 <b>Audio recibido:</b> <code>{safe_name}</code>", parse_mode="HTML")
            except Exception as ex:
                log_warn("telegram_notification_skipped", f"Failed to send initial audio received message: {redact_secrets(str(ex))}")

        log_start(f"Downloading {file_name}")
        local_audio_path = download_file(file_id, file_name)
        log_ok(f"Downloaded {file_name}")
        
        execute_pipeline(local_audio_path, file_name, file_id, state_store, source="drive")
        
    except StatePersistenceError as spe:
        raise spe
    except Exception as e:
        handle_processing_failure(file_id, file_name, "drive", e, state_store)
    finally:
        _release_processing_lease(state_store, lease_key)

def process_youtube_url(url: str, state_store: StateStore | StateBackend):
    log_start(f"YouTube URL detected")
    send_telegram_message(f"📺 <b>Enlace de YouTube detectado</b>", parse_mode="HTML")
    
    lease_key = None
    try:
        title, video_id = get_youtube_metadata(url)
        if getattr(state_store, "backend_name", None) == "firestore":
            state_store.bind_source("youtube", video_id, title)
        yt_state = state_store.get_youtube_state(video_id)
        if not isinstance(yt_state, dict):
            yt_state = {}

        summary_status = yt_state.get("summary_status")
        summary_status_str = summary_status if isinstance(summary_status, str) else None

        if state_store.is_processed(video_id) and summary_status_str == "completed":
            log_warn("youtube_duplicate", f"Video {video_id} already completed.")
            return
        lease_key = _acquire_processing_lease(state_store, "youtube", video_id, title)
        if lease_key == "": return

        # Check if summary was ALREADY generated but failed delivery
        existing_summary = state_store.get_youtube_summary(video_id)
        if existing_summary and summary_status_str in ("pending_delivery", "failed_retryable"):
            log_ok("YouTube summary already generated. Attempting delivery only...")
            delivered = send_long_telegram_message(existing_summary, parse_mode=None, required=True)
            if delivered:
                _renew_processing_lease_or_raise(state_store, lease_key)
                state_store.mark_youtube_summary_completed(video_id, title, existing_summary)
                log_ok("YouTube summary delivered successfully on resend.")
            else:
                _renew_processing_lease_or_raise(state_store, lease_key)
                state_store.save_youtube_summary_pending_delivery(video_id, title, existing_summary)
                _renew_processing_lease_or_raise(state_store, lease_key)
                state_store.mark_youtube_summary_failed(video_id, title, "Telegram delivery failed on resend", permanent=False)
            return

        transcript_text = state_store.get_youtube_transcript(video_id)
        if transcript_text:
            log_ok("YouTube transcript already completed. Reusing existing transcript for summary generation.")
        else:
            local_audio_path, file_name = download_youtube_audio(url, title, video_id)
            success, transcript_text = execute_pipeline(local_audio_path, file_name, video_id, state_store, source="youtube")
            if not success or not transcript_text:
                return

        if transcript_text:
            summary_text = None
            try:
                from summary_generator import generate_summary
                _renew_processing_lease_or_raise(state_store, lease_key)
                summary_text = generate_summary(transcript_text, title, Config.GEMINI_FALLBACK_MODEL)
                if summary_text:
                    log_ok("Sending summary via Telegram")
                    delivered = send_long_telegram_message(summary_text, parse_mode=None, required=True)
                    if delivered:
                        _renew_processing_lease_or_raise(state_store, lease_key)
                        state_store.mark_youtube_summary_completed(video_id, title, summary_text)
                    else:
                        _renew_processing_lease_or_raise(state_store, lease_key)
                        state_store.save_youtube_summary_pending_delivery(video_id, title, summary_text)
                        send_telegram_message("⚠️ Resumen generado pero la entrega por Telegram no pudo completarse. Puedes reenviar el enlace para reintentar la entrega.")
                else:
                    _renew_processing_lease_or_raise(state_store, lease_key)
                    state_store.mark_youtube_summary_failed(video_id, title, "Summary text returned empty", permanent=False)
                    send_telegram_message("⚠️ Transcript procesado pero el resumen devolvió texto vacío.")
            except TelegramDeliveryError as tde:
                log_warn("youtube_summary_delivery", redact_secrets(str(tde)))
                is_transient = is_transient_error(tde)
                if summary_text:
                    _renew_processing_lease_or_raise(state_store, lease_key)
                    state_store.save_youtube_summary_pending_delivery(video_id, title, summary_text)
                _renew_processing_lease_or_raise(state_store, lease_key)
                state_store.mark_youtube_summary_failed(video_id, title, str(tde), permanent=not is_transient)
                send_telegram_message("⚠️ Resumen generado pero falló la entrega por Telegram. Puedes reenviar el enlace para intentar reentregarlo.")
            except Exception as sum_err:
                log_error("summary_generation", redact_secrets(str(sum_err)))
                is_transient = is_transient_error(sum_err)
                _renew_processing_lease_or_raise(state_store, lease_key)
                state_store.mark_youtube_summary_failed(video_id, title, str(sum_err), permanent=not is_transient)
                send_telegram_message("⚠️ Transcript subido a Drive pero no se pudo generar el resumen.")
    except StatePersistenceError as spe:
        raise spe
    except Exception as e:
        if getattr(state_store, "backend_name", None) == "firestore" and not lease_key:
            # Metadata failed before a stable source ID/job could be leased. Persisting
            # a synthetic durable job here would bypass fencing and create orphan state.
            log_error("youtube_prelease_failure", redact_secrets(str(e)))
            try:
                send_telegram_message("⚠️ No se pudo obtener la metadata de YouTube. Intenta nuevamente más tarde.")
            except Exception as notify_error:
                log_warn("telegram_notification_skipped", redact_secrets(str(notify_error)))
            return
        fallback_id = f"yt_{int(time.time())}"
        handle_processing_failure(fallback_id, "YouTube Video", "youtube", e, state_store)
    finally:
        _release_processing_lease(state_store, lease_key)

def process_telegram_audio(message: dict, telegram_update_id: int, state_store: StateStore | StateBackend):
    media_obj = None
    file_id = None
    file_name = None
    file_size = None
    
    if "audio" in message:
        media_obj = message["audio"]
        file_id = media_obj.get("file_id")
        file_name = media_obj.get("file_name", f"audio_{telegram_update_id}.mp3")
        file_size = media_obj.get("file_size")
    elif "voice" in message:
        media_obj = message["voice"]
        file_id = media_obj.get("file_id")
        file_name = f"voice_{telegram_update_id}.ogg"
        file_size = media_obj.get("file_size")
    elif "document" in message:
        doc = message["document"]
        mime = doc.get("mime_type", "")
        if mime.startswith("audio/") or mime.startswith("video/"):
            media_obj = doc
            file_id = doc.get("file_id")
            file_name = doc.get("file_name", f"document_{telegram_update_id}.mp3")
            file_size = doc.get("file_size")
        else:
            return
    elif "video" in message:
        media_obj = message["video"]
        file_id = media_obj.get("file_id")
        file_name = message["video"].get("file_name", f"video_{telegram_update_id}.mp4")
        file_size = media_obj.get("file_size")
    else:
        return
        
    if not file_id:
        return
        
    valid_exts = ('.mp3', '.m4a', '.ogg', '.wav', '.mp4')
    if not file_name.lower().endswith(valid_exts):
        send_telegram_message("❌ Formato no soportado. Envía un archivo .mp3, .m4a, .ogg, .wav o .mp4")
        return
        
    if getattr(state_store, "backend_name", None) == "firestore":
        state_store.bind_source("telegram", file_id, file_name)
    if state_store.is_processed(file_id):
        log_warn("telegram_duplicate", f"File {file_id} already completed.")
        return
    lease_key = _acquire_processing_lease(state_store, "telegram", file_id, file_name)
    if lease_key == "": return

    log_start(f"Processing Telegram direct audio: {file_name}")
    send_telegram_message("✅ Audio recibido. Procesando transcripción...")
    
    try:
        file_path_info = ""
        if file_size is None or not file_path_info:
            info = get_telegram_file_info(file_id)
            if file_size is None:
                file_size = info.get("file_size")
            file_path_info = info.get("file_path", "")

        if file_size and file_size > Config.TELEGRAM_MAX_DOWNLOAD_BYTES:
            size_mb = file_size / (1024 * 1024)
            log_warn("telegram_file_size", f"File size ({size_mb:.1f} MB) exceeds limit ({Config.TELEGRAM_MAX_DOWNLOAD_BYTES / (1024*1024):.1f} MB). Skipping download.")
            send_telegram_message("El archivo supera el límite de descarga directa de Telegram. Súbelo a la carpeta de entrada de Google Drive para procesarlo.")
            return

        if not file_path_info:
            raise Exception("Cannot retrieve file path from Telegram.")
            
        suffix = file_id[:8] if file_id else str(telegram_update_id)
        local_filename = sanitize_filename(file_name, fallback="telegram_audio", suffix=suffix)
        local_audio_path = os.path.join(Config.TEMP_DIR, local_filename)
        
        log_start(f"Downloading from Telegram: {local_filename}")
        download_telegram_file(file_path_info, local_audio_path, expected_size=file_size)
        log_ok(f"Downloaded {local_filename}")
        
        execute_pipeline(local_audio_path, file_name, file_id, state_store, source="telegram", is_telegram_direct=True)
        
    except StatePersistenceError as spe:
        raise spe
    except TelegramDeliveryError as tde:
        log_warn("process_telegram_audio", f"Telegram delivery failed for {file_name}: {redact_secrets(str(tde))}")
        return
    except Exception as e:
        handle_processing_failure(file_id, file_name, "telegram", e, state_store)
    finally:
        _release_processing_lease(state_store, lease_key)

def run_polling_cycle(state_store: StateStore | StateBackend, telegram_offset: int) -> int:
    """Run one complete, sequential ingestion cycle and return its durable offset."""
    _renew_global_cycle_lease_or_raise(state_store)
    pending_items = list_pending_audio_files(state_store)
    for item in pending_items:
        _renew_global_cycle_lease_or_raise(state_store)
        process_file(item, state_store)

    # Fence the boundary between Drive listing and Telegram/YouTube ingestion.
    _renew_global_cycle_lease_or_raise(state_store)
    yt_regex = re.compile(r'(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[^\s]+')
    updates = get_telegram_updates(offset=telegram_offset, timeout=5)
    for update in updates:
        _renew_global_cycle_lease_or_raise(state_store)
        update_id = update.get("update_id")
        try:
            message = update.get("message")
            if not message or not isinstance(message, dict):
                continue

            if not is_authorized_telegram_message(message):
                chat_id = message.get("chat", {}).get("id") if isinstance(message.get("chat"), dict) else None
                log_warn("telegram_unauthorized", f"Unauthorized update from chat_id={chat_id}")
                continue

            text = message.get("text", "")
            if text:
                match = yt_regex.search(text)
                if match:
                    process_youtube_url(match.group(0), state_store)
            elif any(k in message for k in ["audio", "voice", "document", "video"]):
                process_telegram_audio(message, update_id or telegram_offset, state_store)
        finally:
            if update_id is not None:
                new_offset = update_id + 1
                if new_offset > telegram_offset:
                    telegram_offset = new_offset
                    state_store.set_telegram_offset(telegram_offset)
    return telegram_offset


def run_continuous(state_store: StateStore | StateBackend, telegram_offset: int) -> None:
    """Preserve the legacy polling loop, including its generic error handling."""
    while True:
        try:
            telegram_offset = run_polling_cycle(state_store, telegram_offset)
        except StatePersistenceError as spe:
            log_error("main_loop", f"FATAL: State persistence error encountered in polling loop: {redact_secrets(str(spe))}. Stopping process immediately.")
            sys.exit(1)
        except Exception as e:
            log_error("main_loop", f"Unhandled exception in polling loop: {redact_secrets(str(e))}")
            try:
                # The failed cycle may already have durably advanced Telegram's
                # offset. Preserve the legacy in-memory behavior before retrying.
                telegram_offset = state_store.get_telegram_offset()
            except Exception as recovery_error:
                log_error("main_loop", f"FATAL: Could not recover the durable Telegram offset after a polling failure: {redact_secrets(str(recovery_error))}. Stopping process immediately.")
                sys.exit(1)
        time.sleep(Config.POLL_INTERVAL_SECONDS)


def main(instance_lock: SingleInstanceLock = None):
    log_start("Application booting")
    
    lock = instance_lock or SingleInstanceLock()
    lock_required = Config.STATE_BACKEND == "local"
    if lock_required and not lock.acquire():
        log_error("main_init", "FATAL: Otra instancia del Bot Transcriptor ya está ejecutándose.")
        sys.exit(1)
        
    try:
        try:
            Config.validate()
            log_ok("Configuration validated")
        except Exception as e:
            log_error("config_validation", redact_secrets(str(e)))
            sys.exit(1)

        try:
            # Keep the exact legacy construction seam (also used by recovery tests).
            # Cloud selection is the only path that can construct a Firestore client.
            state_store = StateStore() if Config.STATE_BACKEND == "local" else create_state_backend()
            log_ok("State store initialized")
            # A competing Firestore one-shot must no-op before reading any
            # ingestion state. Other modes retain their existing initialization.
            telegram_offset = (None if Config.RUN_MODE == "once" and
                               Config.STATE_BACKEND == "firestore"
                               else state_store.get_telegram_offset())
        except StatePersistenceError as spe:
            log_error("main_init", f"FATAL: Could not initialize StateStore due to persistence/corrupt state error: {redact_secrets(str(spe))}")
            sys.exit(1)

        if Config.RUN_MODE == "once":
            if Config.STATE_BACKEND == "firestore":
                if not state_store.acquire_global_cycle_lease():
                    log_ok("Another one-shot polling cycle is active; exiting without work")
                    return
                telegram_offset = state_store.get_telegram_offset()
            log_start("Starting one-shot polling cycle")
            original_error = None
            try:
                run_polling_cycle(state_store, telegram_offset)
                log_ok("One-shot polling cycle completed")
                return
            except BaseException as exc:
                original_error = exc
                raise
            finally:
                if Config.STATE_BACKEND == "firestore":
                    try:
                        state_store.release_global_cycle_lease()
                    except Exception as release_error:
                        if original_error is None:
                            raise
                        log_warn("global_cycle_release", "Could not release global cycle lease during fatal cleanup; expiry will recover it")

        send_telegram_message("🤖 <b>Bot Transcriptor iniciado</b>", parse_mode="HTML")
        log_start(f"Starting generic polling loop. Interval: {Config.POLL_INTERVAL_SECONDS} seconds")
        run_continuous(state_store, telegram_offset)
    finally:
        if lock_required:
            lock.release()

if __name__ == "__main__":
    main()
