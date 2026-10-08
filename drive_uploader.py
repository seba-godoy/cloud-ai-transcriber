import os
import time
import random
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from drive_client import get_drive_service
from config import Config
from utils import (
    log_start, 
    log_ok, 
    log_warn, 
    log_error, 
    redact_secrets, 
    is_transient_error, 
    DriveIntegrityError,
    DriveUploadUnconfirmedError,
    calculate_backoff
)

def generate_drive_file_id() -> str:
    """
    Pre-allocates an ID from Google Drive using files.generateIds().
    Response format: {"ids": ["generated_id_str"], "space": "drive", "kind": "drive#generatedIds"}
    Retries up to 3 attempts with calculate_backoff for transient errors.
    """
    service = get_drive_service()
    last_err = None
    for attempt in range(1, 4):
        try:
            res = service.files().generateIds(count=1, space='drive').execute()
            ids = res.get("ids", [])
            if not isinstance(ids, list) or len(ids) != 1 or not str(ids[0]).strip():
                raise ValueError(f"Drive files.generateIds failed to return a single valid ID. Response: {res}")
            return str(ids[0]).strip()
        except Exception as e:
            last_err = e
            if not is_transient_error(e):
                raise
            if attempt < 3:
                sleep_dur = calculate_backoff(attempt, base_delay=2.0, max_delay=15.0)
                log_warn("generate_drive_file_id", f"Transient error generating Drive ID (Attempt {attempt}/3). Retrying in {sleep_dur:.1f}s...")
                time.sleep(sleep_dur)
    raise last_err or ValueError("Failed to generate Drive file ID after 3 attempts.")

def upload_file_to_drive(
    file_path: str, 
    mime_type: str, 
    source_key: str, 
    doc_type: str, 
    preallocated_id: str
) -> str:
    """
    Uploads a file to Google Drive using a pre-allocated ID.
    Idempotent upload guarantee with zero except Exception: pass statements.
    """
    if not preallocated_id or not isinstance(preallocated_id, str) or not preallocated_id.strip():
        raise ValueError("preallocated_id is required and cannot be empty.")
    
    clean_name = os.path.basename(file_path)
    log_start(f"Uploading {clean_name} to Drive (Pre-allocated ID: {preallocated_id})")

    service = get_drive_service()
    
    file_metadata = {
        'id': preallocated_id,
        'name': clean_name,
        'parents': [Config.DRIVE_OUTPUT_FOLDER_ID],
        'appProperties': {
            'source_key': str(source_key),
            'document_type': str(doc_type)
        }
    }
    
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)
    
    last_err = None
    for attempt in range(1, 4):
        try:
            # Check if preallocated_id already exists on Drive before creating
            try:
                existing = service.files().get(
                    fileId=preallocated_id, 
                    fields='id, trashed, appProperties'
                ).execute()
                if existing and not existing.get('trashed', False):
                    props = existing.get('appProperties', {})
                    if props.get('source_key') == source_key and props.get('document_type') == doc_type:
                        log_ok(f"File with ID {preallocated_id} already exists on Drive. Reusing existing upload.")
                        return preallocated_id
                    else:
                        raise DriveIntegrityError(f"File {preallocated_id} exists on Drive but has incompatible appProperties.")
            except HttpError as e:
                status_code = getattr(e.resp, 'status', None) or getattr(e, 'status_code', None)
                if status_code != 404:
                    raise

            # Perform creation
            file_res = service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, appProperties'
            ).execute()
            
            created_id = file_res.get('id') if file_res else None
            if created_id == preallocated_id:
                log_ok(f"Uploaded {clean_name} successfully (Drive ID: {created_id})")
                return created_id

            # If response missing ID or ID mismatch, check preallocated_id explicitly
            try:
                chk = service.files().get(fileId=preallocated_id, fields='id, trashed, appProperties').execute()
                if chk and not chk.get('trashed', False):
                    props = chk.get('appProperties', {})
                    if props.get('source_key') == source_key and props.get('document_type') == doc_type:
                        log_ok(f"Uploaded {clean_name} successfully (Drive ID confirmed: {preallocated_id})")
                        return preallocated_id
                    raise DriveIntegrityError("File exists with incompatible appProperties.")
                raise DriveUploadUnconfirmedError("Creation unconfirmed (file trashed or missing).")
            except HttpError as get_err:
                status_code = getattr(get_err.resp, 'status', None) or getattr(get_err, 'status_code', None)
                if status_code == 404:
                    raise DriveUploadUnconfirmedError("Creation unconfirmed (404 Not Found).") from get_err
                raise get_err

        except Exception as create_err:
            last_err = create_err
            # Confirm if file was created despite creation error
            try:
                chk = service.files().get(
                    fileId=preallocated_id, 
                    fields='id, trashed, appProperties'
                ).execute()
                if chk and not chk.get('trashed', False):
                    props = chk.get('appProperties', {})
                    if props.get('source_key') == source_key and props.get('document_type') == doc_type:
                        log_ok(f"File with ID {preallocated_id} confirmed on Drive after ambiguous error. Reusing.")
                        return preallocated_id
                    raise DriveIntegrityError("File exists with incompatible appProperties.")
            except HttpError as get_err:
                status_code = getattr(get_err.resp, 'status', None) or getattr(get_err, 'status_code', None)
                if status_code != 404:
                    raise get_err from create_err
            except Exception as get_err:
                raise get_err from create_err

            if not is_transient_error(create_err):
                log_error("upload_file_to_drive", f"Permanent failure uploading {clean_name}: {redact_secrets(str(create_err))}")
                raise create_err

            if attempt < 3:
                sleep_dur = calculate_backoff(attempt, base_delay=2.0, max_delay=30.0)
                log_warn("upload_file_to_drive", f"Transient upload error (Attempt {attempt}/3). Retrying in {sleep_dur:.1f}s...")
                time.sleep(sleep_dur)

    raise last_err or DriveUploadUnconfirmedError("Upload failed after 3 attempts.")
