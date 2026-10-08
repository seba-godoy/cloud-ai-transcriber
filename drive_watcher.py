import os
import io
from googleapiclient.http import MediaIoBaseDownload
from drive_client import get_drive_service
from config import Config
from utils import log_error, sanitize_filename, retry, redact_secrets


@retry(attempts=3, delay=5)
def list_pending_audio_files(state_store):
    """
    Return valid pending/retry-due audio files from every configured Drive input.

    Each discovered file is associated with a logical owner (currently Primary or
    Secondary) and a matching output folder. Firestore receives that routing metadata
    immediately so the dashboard can filter jobs without inspecting Drive later.
    Full nextPageToken pagination is preserved independently per input folder.
    """
    service = get_drive_service()
    valid_extensions = ('.m4a', '.mp3', '.mp4')
    pending_files = []
    seen_file_ids = set()

    try:
        for route in Config.drive_folder_routes():
            input_folder_id = route.get("input_folder_id")
            output_folder_id = route.get("output_folder_id")
            owner_id = route.get("owner_id")
            if not input_folder_id or not output_folder_id:
                continue

            query = f"'{input_folder_id}' in parents and trashed=false"
            page_token = None

            while True:
                kwargs = {
                    "q": query,
                    "fields": "nextPageToken, files(id, name, mimeType)",
                    "pageSize": 100,
                }
                if page_token:
                    kwargs["pageToken"] = page_token

                results = service.files().list(**kwargs).execute()
                items = results.get('files', [])

                for raw_item in items:
                    item = dict(raw_item)
                    name = item.get('name', '').lower()
                    file_id = item.get('id')

                    if not file_id or file_id in seen_file_ids or not name.endswith(valid_extensions):
                        continue
                    seen_file_ids.add(file_id)

                    Config.register_drive_file_route(
                        file_id,
                        owner_id=owner_id,
                        input_folder_id=input_folder_id,
                        output_folder_id=output_folder_id,
                    )
                    item["owner_id"] = owner_id
                    item["input_folder_id"] = input_folder_id
                    item["output_folder_id"] = output_folder_id

                    if getattr(state_store, "backend_name", None) == "firestore":
                        state_store.bind_source("drive", file_id, item.get("name", ""))
                        source_key = state_store._key(file_id)
                        state_store.ensure_job(
                            source_key,
                            owner_id=owner_id,
                            input_folder_id=input_folder_id,
                            output_folder_id=output_folder_id,
                        )

                    if state_store.is_processed(file_id) or state_store.is_permanently_failed(file_id):
                        continue

                    # Check if retryable and not yet due.
                    if (getattr(state_store, "backend_name", None) != "firestore"
                            and not state_store.is_retry_due(file_id)
                            and state_store._load().get(file_id, {}).get("status") == "retryable"):
                        continue
                    if (getattr(state_store, "backend_name", None) == "firestore"
                            and not state_store.is_retry_due(file_id)
                            and (state_store.get_job(state_store._key(file_id)) or {}).get("status") == "retryable"):
                        continue

                    pending_files.append(item)

                page_token = results.get('nextPageToken')
                if not page_token:
                    break

        return pending_files
    except Exception as e:
        log_error("list_pending_audio_files", f"Failed to list files: {redact_secrets(str(e))}")
        raise


@retry(attempts=3, delay=10)
def download_file(file_id: str, file_name: str) -> str:
    """
    Download a Drive file to TEMP_DIR and activate its owner-specific output route.
    """
    service = get_drive_service()
    os.makedirs(Config.TEMP_DIR, exist_ok=True)

    # The one-shot worker is serial under the existing global cycle lease. This
    # makes the active output folder deterministic for the pipeline that follows.
    Config.activate_drive_file_route(file_id)

    suffix = file_id[:8] if file_id else ""
    local_filename = sanitize_filename(file_name, fallback="audio_drive", suffix=suffix)
    local_path = os.path.join(Config.TEMP_DIR, local_filename)

    try:
        request = service.files().get_media(fileId=file_id)
        with io.FileIO(local_path, 'wb') as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
        return local_path
    except Exception as e:
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
            except Exception:
                pass
        log_error("download_file", f"Failed to download {redact_secrets(file_name)}: {redact_secrets(str(e))}")
        raise
