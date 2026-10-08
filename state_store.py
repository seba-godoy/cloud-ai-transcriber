import json
import os
from datetime import datetime, timezone, timedelta
from config import Config
from utils import (
    redact_secrets, 
    log_warn,
    StatePersistenceError, 
    make_source_key,
    save_cached_transcript,
    get_cached_transcript
)

class StateStore:
    def __init__(self, file_path: str = None):
        self.file_path = file_path or Config.LOCAL_STATE_FILE
        base, _ = os.path.splitext(self.file_path)
        self.initialized_path = f"{base}.initialized"
        self.recovery_required_path = f"{base}.recovery_required"
        self._is_runtime_initialized = False
        self._initialize()

    def _get_corrupt_backups(self) -> list:
        base, _ = os.path.splitext(self.file_path)
        directory = os.path.dirname(self.file_path) or "."
        prefix = f"{os.path.basename(base)}.corrupt_"
        if not os.path.exists(directory):
            return []
        return [os.path.join(directory, f) for f in os.listdir(directory) if f.startswith(prefix) and f.endswith(".json")]

    def _create_initialized_marker(self):
        temp_init = f"{self.initialized_path}.tmp"
        with open(temp_init, "w", encoding="utf-8") as f:
            f.write(datetime.now(timezone.utc).isoformat())
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_init, self.initialized_path)

    def _initialize(self):
        if os.path.exists(self.recovery_required_path):
            raise StatePersistenceError("Se requiere recuperación manual de estado debido a una corrupción previa. Bot detenido.")

        if not os.path.exists(self.file_path):
            if os.path.exists(self.initialized_path):
                raise StatePersistenceError("El archivo de estado desapareció después de haber sido inicializado. Se requiere recuperación manual para evitar reprocesamientos y duplicados.")
            
            corrupt_backups = self._get_corrupt_backups()
            if corrupt_backups:
                raise StatePersistenceError("Existen copias de seguridad corruptas previas. Se requiere recuperación manual antes de reiniciar.")

            # First legitimate run: create state and initialized files atomically
            self._save({})
            self._create_initialized_marker()
        else:
            # Valid existing state file exists: load it to validate
            self._load()
            # Legacy migration check: if valid state exists but initialized marker does not exist yet
            if not os.path.exists(self.initialized_path):
                self._create_initialized_marker()
            self._migrate_historical_dns_errors()

        self._is_runtime_initialized = True

    def _migrate_historical_dns_errors(self):
        data = self._load()
        signatures = [
            "[errno 11001] getaddrinfo failed",
            "getaddrinfo failed",
            "wsahost_not_found",
            "temporary failure in name resolution"
        ]
        modified = False
        now_iso = datetime.now(timezone.utc).isoformat()
        
        for file_id, entry in data.items():
            if not isinstance(entry, dict) or file_id.startswith("__"):
                continue
                
            status = entry.get("status")
            if status not in ("failed", "failed_permanent"):
                continue
                
            if entry.get("migrated_from") == "failed_permanent_network_error":
                continue
                
            err_msg = (entry.get("error_message") or entry.get("last_error") or "").lower()
            if any(sig in err_msg for sig in signatures):
                entry["status"] = "retryable"
                entry["retry_count"] = entry.get("retry_count", 1)
                entry["next_retry_at"] = now_iso
                entry["migrated_from"] = "failed_permanent_network_error"
                modified = True
                from utils import log_warn
                log_warn("state_store_migration", f"Migrated historical network failure for file '{entry.get('name', file_id)}' ({file_id}) to retryable.")
                
        if modified:
            self._save(data)

    def _load(self) -> dict:
        if os.path.exists(self.recovery_required_path):
            raise StatePersistenceError("Se requiere recuperación manual de estado debido a una corrupción previa. Bot detenido.")

        if not os.path.exists(self.file_path):
            if os.path.exists(self.initialized_path) or self._is_runtime_initialized:
                raise StatePersistenceError("El archivo de estado desapareció después de haber sido inicializado. Se requiere recuperación manual para evitar reprocesamientos y duplicados.")
            return {}

        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            base, ext = os.path.splitext(self.file_path)
            corrupt_path = f"{base}.corrupt_{timestamp_str}{ext or '.json'}"
            try:
                if os.path.exists(self.file_path):
                    os.replace(self.file_path, corrupt_path)
                with open(self.recovery_required_path, "w", encoding="utf-8") as rf:
                    rf.write(datetime.now(timezone.utc).isoformat())
            except Exception as ex:
                raise StatePersistenceError(f"Corrupt state JSON backup failed: {str(ex)}") from ex
            raise StatePersistenceError(f"Corrupt state JSON file detected and backed up to {corrupt_path}. Bot execution stopped for manual recovery.") from e
        except (PermissionError, OSError) as e:
            raise StatePersistenceError(f"Failed to read state file: {str(e)}") from e

    def _save(self, data: dict):
        temp_path = f"{self.file_path}.tmp"
        try:
            directory = os.path.dirname(self.file_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
                
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.file_path)
        except Exception as e:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            raise StatePersistenceError(f"Failed to persist state to disk: {str(e)}") from e

    def is_processed(self, file_id: str) -> bool:
        data = self._load()
        if file_id in data:
            return data[file_id].get("status") == "completed"
        return False

    def is_permanently_failed(self, file_id: str) -> bool:
        data = self._load()
        if file_id in data:
            return data[file_id].get("status") in ("failed_permanent", "failed")
        return False

    def is_failed(self, file_id: str) -> bool:
        return self.is_permanently_failed(file_id)

    def is_retry_due(self, file_id: str) -> bool:
        data = self._load()
        if file_id not in data:
            return False
        entry = data[file_id]
        if entry.get("status") != "retryable":
            return False
            
        next_retry_str = entry.get("next_retry_at")
        if not next_retry_str:
            return True
            
        try:
            next_retry_dt = datetime.fromisoformat(next_retry_str)
            now_dt = datetime.now(timezone.utc)
            return now_dt >= next_retry_dt
        except Exception:
            return True

    def get_partial_uploads(self, file_id: str) -> dict:
        data = self._load()
        return data.get(file_id, {}).get("partial_uploads", {})

    def save_partial_upload(self, file_id: str, doc_type: str, drive_id: str):
        if doc_type not in ("docx", "pdf"):
            raise ValueError(f"Invalid doc_type '{doc_type}'. Must be 'docx' or 'pdf'.")
        if not drive_id or not isinstance(drive_id, str) or not drive_id.strip():
            raise ValueError("drive_id must be a non-empty string.")
            
        data = self._load()
        entry = data.setdefault(file_id, {
            "file_id": file_id,
            "status": "in_progress"
        })
        partials = entry.setdefault("partial_uploads", {})
        partials[doc_type] = drive_id.strip()
        self._save(data)

    def remove_partial_upload(self, file_id: str, doc_type: str):
        data = self._load()
        if file_id in data and "partial_uploads" in data[file_id]:
            data[file_id]["partial_uploads"].pop(doc_type, None)
            self._save(data)

    def mark_processed(self, file_id: str, name: str, model_used: str, final_ids: dict = None):
        data = self._load()
        entry = {
            "file_id": file_id,
            "name": name,
            "processed_date": datetime.now(timezone.utc).isoformat(),
            "model_used": model_used,
            "status": "completed",
            "final_ids": final_ids or {}
        }
        data[file_id] = entry
        self._save(data)

    def mark_retryable(self, file_id: str, name: str, source: str, error_message: str, is_network_error: bool = False):
        data = self._load()
        prev_entry = data.get(file_id, {})
        prev_retry_count = prev_entry.get("retry_count", 0)
        new_retry_count = prev_retry_count + 1
        partials = prev_entry.get("partial_uploads", {})
        migrated_from = prev_entry.get("migrated_from")
        now_dt = datetime.now(timezone.utc)
        first_error_at = prev_entry.get("first_error_at", now_dt.isoformat())
        
        if is_network_error:
            prev_net_count = prev_entry.get("network_retry_count", 0)
            new_net_count = prev_net_count + 1
            max_net_retries = getattr(Config, "MAX_NETWORK_FILE_RETRIES", 0)
            
            if max_net_retries > 0 and new_net_count >= max_net_retries:
                self.mark_permanent_failed(file_id, name, f"Exceeded max network retries ({max_net_retries}): {error_message}", source)
                return
                
            base_sec = getattr(Config, "NETWORK_FILE_RETRY_BASE_SECONDS", 30)
            max_sec = getattr(Config, "NETWORK_FILE_RETRY_MAX_SECONDS", 300)
            delay_sec = min(base_sec * (2 ** (new_net_count - 1)), max_sec)

            if new_net_count > 0 and new_net_count % 12 == 0:
                log_warn("persistent_network_failure", f"Network/DNS has failed for {new_net_count} consecutive file retries. Check internet/DNS configuration. Progress remains preserved and retryable.")
            
            entry = {
                "file_id": file_id,
                "name": name,
                "status": "retryable",
                "source": source,
                "retry_count": new_retry_count,
                "network_retry_count": new_net_count,
                "last_error": redact_secrets(error_message),
                "first_error_at": first_error_at,
                "last_attempt_at": now_dt.isoformat(),
                "next_retry_at": (now_dt + timedelta(seconds=delay_sec)).isoformat()
            }
        else:
            if new_retry_count >= Config.MAX_FILE_RETRIES:
                self.mark_permanent_failed(file_id, name, f"Exceeded max retries ({Config.MAX_FILE_RETRIES}): {error_message}", source)
                return
                
            delay_sec = min(Config.FILE_RETRY_BASE_SECONDS * (2 ** (new_retry_count - 1)), Config.FILE_RETRY_MAX_SECONDS)
            
            entry = {
                "file_id": file_id,
                "name": name,
                "status": "retryable",
                "source": source,
                "retry_count": new_retry_count,
                "last_error": redact_secrets(error_message),
                "first_error_at": first_error_at,
                "last_attempt_at": now_dt.isoformat(),
                "next_retry_at": (now_dt + timedelta(seconds=delay_sec)).isoformat()
            }
            if "network_retry_count" in prev_entry:
                entry["network_retry_count"] = prev_entry["network_retry_count"]

        if partials:
            entry["partial_uploads"] = partials
        if migrated_from:
            entry["migrated_from"] = migrated_from

        data[file_id] = entry
        self._save(data)

    def mark_permanent_failed(self, file_id: str, name: str, error_message: str, source: str = "drive"):
        data = self._load()
        prev_entry = data.get(file_id, {})
        partials = prev_entry.get("partial_uploads", {})
        
        entry = {
            "file_id": file_id,
            "name": name,
            "processed_date": datetime.now(timezone.utc).isoformat(),
            "model_used": "N/A",
            "status": "failed_permanent",
            "source": source,
            "error_message": redact_secrets(error_message)
        }
        if partials:
            entry["partial_uploads"] = partials
        data[file_id] = entry
        self._save(data)

    def get_youtube_state(self, video_id: str) -> dict:
        data = self._load()
        entry = data.get(video_id, {})
        return entry.get("youtube_state", {})

    def save_youtube_transcript(self, video_id: str, title: str, transcript_text: str, docx_id: str = None, pdf_id: str = None):
        source_key = make_source_key("youtube", video_id)
        cache_path, sha256_hash = save_cached_transcript(source_key, transcript_text)

        data = self._load()
        entry = data.setdefault(video_id, {
            "file_id": video_id,
            "name": title,
            "source": "youtube",
            "processed_date": datetime.now(timezone.utc).isoformat(),
            "status": "in_progress"
        })
        yt_state = entry.setdefault("youtube_state", {})
        yt_state["transcript_status"] = "completed"
        yt_state["cache_path"] = cache_path
        yt_state["sha256"] = sha256_hash
        if docx_id: yt_state["docx_id"] = docx_id
        if pdf_id: yt_state["pdf_id"] = pdf_id
        if "summary_status" not in yt_state:
            yt_state["summary_status"] = "pending"
        self._save(data)

    def get_youtube_transcript(self, video_id: str) -> str | None:
        yt_state = self.get_youtube_state(video_id)
        cache_path = yt_state.get("cache_path")
        sha256_hash = yt_state.get("sha256")
        if cache_path and sha256_hash:
            cached = get_cached_transcript(cache_path, sha256_hash)
            if cached:
                return cached
        # Fallback check for inline transcript_text (legacy compatibility)
        inline = yt_state.get("transcript_text")
        if inline:
            return inline
        return None

    def mark_youtube_summary_completed(self, video_id: str, title: str, summary_text: str = None):
        data = self._load()
        entry = data.get(video_id, {})
        yt_state = entry.setdefault("youtube_state", {})
        yt_state["summary_status"] = "completed"
        if summary_text:
            source_key = make_source_key("youtube_summary", video_id)
            cache_path, sha256_hash = save_cached_transcript(source_key, summary_text)
            yt_state["summary_cache_path"] = cache_path
            yt_state["summary_sha256"] = sha256_hash
        entry["status"] = "completed"
        entry["processed_date"] = datetime.now(timezone.utc).isoformat()
        self._save(data)

    def get_youtube_summary(self, video_id: str) -> str | None:
        yt_state = self.get_youtube_state(video_id)
        cache_path = yt_state.get("summary_cache_path")
        sha256_hash = yt_state.get("summary_sha256")
        if cache_path and sha256_hash:
            cached = get_cached_transcript(cache_path, sha256_hash)
            if cached:
                return cached
        return yt_state.get("summary_text")

    def save_youtube_summary_pending_delivery(self, video_id: str, title: str, summary_text: str):
        source_key = make_source_key("youtube_summary", video_id)
        cache_path, sha256_hash = save_cached_transcript(source_key, summary_text)

        data = self._load()
        entry = data.setdefault(video_id, {
            "file_id": video_id,
            "name": title,
            "source": "youtube",
            "processed_date": datetime.now(timezone.utc).isoformat()
        })
        yt_state = entry.setdefault("youtube_state", {})
        yt_state["summary_status"] = "pending_delivery"
        yt_state["summary_cache_path"] = cache_path
        yt_state["summary_sha256"] = sha256_hash
        self._save(data)

    def mark_youtube_summary_failed(self, video_id: str, title: str, error_msg: str, permanent: bool = False):
        data = self._load()
        entry = data.get(video_id, {})
        yt_state = entry.setdefault("youtube_state", {})
        yt_state["summary_status"] = "failed_permanent" if permanent else "failed_retryable"
        yt_state["summary_error"] = redact_secrets(error_msg)
        self._save(data)

    def get_telegram_offset(self) -> int:
        data = self._load()
        return data.get("__telegram_offset__", 0)

    def set_telegram_offset(self, offset: int):
        data = self._load()
        data["__telegram_offset__"] = offset
        self._save(data)
