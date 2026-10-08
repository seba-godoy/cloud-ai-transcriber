import os
import re
import sys
import time
import math
import random
import socket
import hashlib
from functools import wraps

class StatePersistenceError(Exception):
    """Raised when atomic save or load of local state fails."""
    pass

class ProgressPersistenceError(Exception):
    """Raised when checkpoint progress saving or loading fails after retries."""
    pass

class TranscriptionUnavailableError(Exception):
    """Raised when Gemini default and fallback models fail to return a valid transcript."""
    pass

class DriveIntegrityError(Exception):
    """Raised when multiple files match search or Drive data integrity is violated."""
    pass

class NetworkUnavailableError(Exception):
    """Raised or wrapped when network or DNS is temporarily unavailable."""
    pass

class TelegramDeliveryError(Exception):
    """Raised when mandatory Telegram delivery (required=True) fails after retries."""
    def __init__(self, message: str, transient: bool | None = None):
        super().__init__(message)
        self.transient = transient

class DriveUploadUnconfirmedError(Exception):
    """Raised when a Drive upload creation occurred or was interrupted but cannot be confirmed immediately."""
    pass

class TelegramAPIError(Exception):
    """Raised when Telegram API returns ok=False in JSON response."""
    def __init__(self, error_code: int, description: str, retry_after: int | None = None):
        self.error_code = error_code
        self.description = redact_secrets(description)
        self.retry_after = retry_after
        super().__init__(f"Telegram API Error {error_code}: {self.description}")

def calculate_backoff(attempt: int, base_delay: float = 2.0, max_delay: float = 60.0, jitter_fn=None) -> float:
    """
    Calculates exponential backoff delay with jitter: min(max_delay, base_delay * 2 ** (attempt - 1)) + jitter
    """
    _jitter = jitter_fn or random.uniform
    jitter_val = _jitter(0.1, 1.0)
    backoff = min(float(max_delay), float(base_delay) * (2 ** (attempt - 1)))
    return backoff + jitter_val

TRANSIENT_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}
PERMANENT_GOOGLE_REASONS = {
    "storageQuotaExceeded",
    "insufficientFilePermissions",
    "appNotAuthorizedToFile",
    "dailyLimitExceeded",
}
TRANSIENT_GOOGLE_REASONS = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
}
TRANSIENT_WINERRNOS = {11001, 10050, 10051, 10053, 10054, 10060, 10061, 10065}

EXACT_DNS_SIGNATURES = [
    "getaddrinfo failed",
    "name resolution",
    "temporary failure in name resolution",
    "nodename nor servname provided",
    "failed to establish a new connection",
    "wsahost_not_found"
]

def iter_exception_chain(error: BaseException):
    """
    Yields error and all cause/context ancestors without infinite cycles.
    """
    curr = error
    seen = set()
    while curr is not None and id(curr) not in seen:
        seen.add(id(curr))
        yield curr
        cause = getattr(curr, '__cause__', None)
        if cause is not None:
            curr = cause
        else:
            curr = getattr(curr, '__context__', None)

def is_network_or_dns_error(error: BaseException) -> bool:
    """
    Checks if an exception or any cause in its chain represents a network or DNS failure.
    """
    if error is None:
        return False

    for node in iter_exception_chain(error):
        if isinstance(node, (NetworkUnavailableError, socket.gaierror, ConnectionError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return True
            
        node_name = type(node).__name__
        if node_name in (
            "NetworkUnavailableError", "gaierror", "ConnectionError", 
            "ConnectTimeout", "ReadTimeout", "TimeoutError", 
            "ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError",
            "MaxRetryError", "NewConnectionError"
        ):
            return True

        if "requests" in type(node).__module__ or "urllib3" in type(node).__module__:
            if any(k in node_name for k in ("ConnectionError", "Timeout", "ConnectTimeoutError", "MaxRetryError", "NameResolutionError", "ProxyError")):
                return True

        if isinstance(node, (OSError, socket.error)):
            err_no = getattr(node, 'errno', None)
            win_err = getattr(node, 'winerror', None)
            if err_no in TRANSIENT_WINERRNOS or win_err in TRANSIENT_WINERRNOS:
                return True
            for arg in getattr(node, 'args', ()):
                if isinstance(arg, int) and arg in TRANSIENT_WINERRNOS:
                    return True

        node_str = str(node).lower()
        if any(sig in node_str for sig in EXACT_DNS_SIGNATURES):
            return True

    return False

def make_source_key(source: str, file_id: str) -> str:
    """Generates a stable sha256 hash key combining source and file_id."""
    return hashlib.sha256(f"{source}\0{file_id}".encode("utf-8")).hexdigest()

def redact_secrets(text: str) -> str:
    """
    Redacts known secrets (API keys, bot tokens, auth headers) from strings.
    """
    if not text:
        return ""
    
    redacted = str(text)
    try:
        from config import Config
        if Config.TELEGRAM_BOT_TOKEN and len(str(Config.TELEGRAM_BOT_TOKEN)) > 5:
            redacted = redacted.replace(str(Config.TELEGRAM_BOT_TOKEN), "[REDACTED_TELEGRAM_TOKEN]")
        if Config.GEMINI_API_KEY and len(str(Config.GEMINI_API_KEY)) > 5:
            redacted = redacted.replace(str(Config.GEMINI_API_KEY), "[REDACTED_GEMINI_KEY]")
    except Exception:
        pass
        
    redacted = re.sub(r'/bot[0-9]+:[A-Za-z0-9_-]+/', '/bot[REDACTED_TOKEN]/', redacted)
    redacted = re.sub(r'([?&](?:api_)?key=)[A-Za-z0-9_-]+', r'\1[REDACTED_KEY]', redacted, flags=re.IGNORECASE)
    redacted = re.sub(r'(Bearer\s+)[A-Za-z0-9._-]+', r'\1[REDACTED_TOKEN]', redacted, flags=re.IGNORECASE)
    redacted = re.sub(r'(Authorization:\s*)[^\s]+', r'\1[REDACTED_AUTH]', redacted, flags=re.IGNORECASE)
    return redacted

def log_start(step_name: str):
    print(f"[START] {redact_secrets(step_name)}", flush=True)

def log_ok(step_name: str):
    print(f"[OK] {redact_secrets(step_name)}", flush=True)

def log_error(step_name: str, message: str):
    clean_msg = redact_secrets(message)
    print(f"[ERROR] {redact_secrets(step_name)}: {clean_msg}", flush=True)

def log_warn(step_name: str, message: str):
    clean_msg = redact_secrets(message)
    print(f"[WARN] {redact_secrets(step_name)}: {clean_msg}", flush=True)

def sanitize_filename(filename: str, fallback: str = "archivo", suffix: str = "") -> str:
    """
    Sanitizes filenames safely for Windows filesystems while preserving extensions and safe Unicode.
    Reserves space for _<suffix> and extension BEFORE truncating the stem so suffix is never lost.
    """
    if not filename:
        filename = fallback

    filename = os.path.basename(filename).strip()
    
    name_part, ext = os.path.splitext(filename)
    ext = ext.strip()
    if ext == ".":
        name_part = f"{name_part}."
        ext = ""
        
    name_part = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name_part).strip(' .')
    if not name_part:
        name_part = fallback

    reserved = {"CON", "PRN", "AUX", "NUL"}
    for i in range(1, 10):
        reserved.add(f"COM{i}")
        reserved.add(f"LPT{i}")
        
    if name_part.upper() in reserved:
        name_part = f"{name_part}_file"

    clean_suffix = re.sub(r'[^a-zA-Z0-9_-]', '', str(suffix)) if suffix else ""
    suffix_str = f"_{clean_suffix}" if clean_suffix else ""
    
    ext = re.sub(r'[<>:"/\\|?*\x00-\x1f\s]', '', ext)
    
    max_stem_len = 180 - len(ext) - len(suffix_str)
    if max_stem_len < 1:
        max_stem_len = 1
        
    if len(name_part) > max_stem_len:
        name_part = name_part[:max_stem_len].strip(' .')
        
    final_name = f"{name_part}{suffix_str}{ext}".strip(' .')
    return final_name or fallback

def get_http_status_code(error: Exception) -> int | None:
    """Returns the HTTP status code from various exception types if present."""
    if error is None:
        return None
        
    if hasattr(error, 'error_code'):
        try:
            return int(error.error_code)
        except (ValueError, TypeError):
            pass

    if hasattr(error, 'resp') and hasattr(error.resp, 'status'):
        try:
            return int(error.resp.status)
        except (ValueError, TypeError):
            pass
            
    if hasattr(error, 'response') and hasattr(error.response, 'status_code'):
        try:
            return int(error.response.status_code)
        except (ValueError, TypeError):
            pass
            
    if hasattr(error, 'code'):
        try:
            return int(error.code)
        except (ValueError, TypeError):
            pass
            
    if hasattr(error, 'status_code'):
        try:
            return int(error.status_code)
        except (ValueError, TypeError):
            pass
            
    return None

DISK_FULL_ERRNOS = {28, 112} # 28: ENOSPC, 112: ERROR_DISK_FULL on Windows

def is_disk_full_error(error: BaseException) -> bool:
    if error is None:
        return False
    for node in iter_exception_chain(error):
        err_no = getattr(node, 'errno', None)
        win_err = getattr(node, 'winerror', None)
        if err_no in DISK_FULL_ERRNOS or win_err in DISK_FULL_ERRNOS:
            return True
        node_str = str(node).lower()
        if any(term in node_str for term in ["no space left on device", "not enough space", "disk full", "there is not enough space"]):
            return True
    return False

def is_transient_error(error: Exception) -> bool:
    """
    Determines whether an exception represents a transient network/API error.
    """
    if error is None:
        return False

    if is_disk_full_error(error):
        return False

    error_type_name = type(error).__name__
    if isinstance(error, (StatePersistenceError, DriveIntegrityError, PermissionError, FileNotFoundError)) or \
       error_type_name in ('StatePersistenceError', 'DriveIntegrityError', 'PermissionError', 'FileNotFoundError'):
        return False

    if isinstance(error, ProgressPersistenceError) or error_type_name == "ProgressPersistenceError":
        cause = getattr(error, '__cause__', None) or getattr(error, '__context__', None)
        if cause and is_disk_full_error(cause):
            return False
        return True

    if isinstance(error, TelegramDeliveryError) or error_type_name == "TelegramDeliveryError":
        transient_attr = getattr(error, 'transient', None)
        if transient_attr is not None:
            return bool(transient_attr)
        cause = getattr(error, '__cause__', None)
        if cause:
            return is_transient_error(cause)
        return True

    if isinstance(error, TelegramAPIError) or error_type_name == "TelegramAPIError":
        code = getattr(error, 'error_code', None)
        if code in (429, 408) or (code and code >= 500):
            return True
        return False

    if isinstance(error, DriveUploadUnconfirmedError) or error_type_name == "DriveUploadUnconfirmedError":
        return True

    if isinstance(error, TranscriptionUnavailableError) or error_type_name == "TranscriptionUnavailableError":
        return True

    if is_network_or_dns_error(error):
        return True

    code = get_http_status_code(error)
    if code is not None:
        content_str = ""
        if hasattr(error, 'content'):
            content_str = error.content.decode('utf-8', errors='ignore') if isinstance(error.content, bytes) else str(error.content)
            
        for reason in TRANSIENT_GOOGLE_REASONS:
            if reason in content_str:
                return True
                
        for reason in PERMANENT_GOOGLE_REASONS:
            if reason in content_str:
                return False
                
        if code in TRANSIENT_HTTP_STATUS_CODES:
            return True
            
        return False

    err_str = str(error).lower()
    if any(term in err_str for term in ['rate limit', 'ratelimit', 'too many requests', '503', '502', '504', 'timeout', 'connection reset', 'temporarily unavailable']):
        if not any(perm in err_str for perm in ['quota exceeded', 'permission denied', 'not found', 'invalid argument']):
            return True
            
    return False

class SingleInstanceLock:
    """
    Guarantees single process execution across lifetime using OS-level mutex / file lock.
    """
    def __init__(self, lock_name: str = "agente_transcriptor_bot_single_instance"):
        self.lock_name = lock_name
        self.mutex_handle = None
        self.file_handle = None
        self.is_acquired = False

    def acquire(self) -> bool:
        if self.is_acquired:
            return True

        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            CreateMutexW = kernel32.CreateMutexW
            CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            CreateMutexW.restype = wintypes.HANDLE
            CloseHandle = kernel32.CloseHandle
            CloseHandle.argtypes = [wintypes.HANDLE]
            CloseHandle.restype = wintypes.BOOL
            GetLastError = kernel32.GetLastError
            ERROR_ALREADY_EXISTS = 183

            handle = CreateMutexW(None, False, f"Local\\{self.lock_name}")
            err = GetLastError()
            
            if not handle or handle == 0:
                raise OSError(err, f"CreateMutexW failed with WinError {err}")

            if err == ERROR_ALREADY_EXISTS:
                CloseHandle(handle)
                self.mutex_handle = None
                self.is_acquired = False
                return False

            self.mutex_handle = handle
            self.is_acquired = True
            return True
        else:
            import fcntl
            import tempfile
            lock_path = os.path.join(tempfile.gettempdir(), f"{self.lock_name}.lock")
            try:
                self.file_handle = open(lock_path, "w")
                fcntl.flock(self.file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.is_acquired = True
                return True
            except (OSError, IOError):
                if self.file_handle:
                    try:
                        self.file_handle.close()
                    except Exception:
                        pass
                    self.file_handle = None
                self.is_acquired = False
                return False

    def release(self):
        if not self.is_acquired:
            return
        if os.name == 'nt' and self.mutex_handle:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            CloseHandle = kernel32.CloseHandle
            CloseHandle.argtypes = [wintypes.HANDLE]
            CloseHandle.restype = wintypes.BOOL
            CloseHandle(self.mutex_handle)
            self.mutex_handle = None
        elif self.file_handle:
            try:
                self.file_handle.close()
            except Exception:
                pass
            self.file_handle = None
        self.is_acquired = False

def retry(attempts=3, delay=5, sleep_fn=None, jitter_fn=None):
    """
    Retry decorator using bounded exponential backoff and jitter via calculate_backoff.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            _sleep = sleep_fn or time.sleep
            _jitter = jitter_fn or random.uniform
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if not is_transient_error(e):
                        raise
                    if attempt >= attempts:
                        raise
                        
                    retry_after = getattr(e, 'retry_after', None)
                    if retry_after is not None and isinstance(retry_after, (int, float)) and retry_after > 0:
                        sleep_dur = float(retry_after)
                    else:
                        sleep_dur = calculate_backoff(attempt, base_delay=delay, max_delay=60.0, jitter_fn=_jitter)
                        
                    log_warn(func.__name__, f"Transient error detected (Attempt {attempt}/{attempts}). Retrying in {sleep_dur:.1f}s...")
                    _sleep(sleep_dur)
        return wrapper
    return decorator

def get_transcript_cache_dir() -> str:
    """
    Resolves the stable absolute path for transcript cache directory:
    - If Config.TRANSCRIPT_CACHE_DIR is absolute, returns it normalized.
    - If relative, resolves it relative to the directory containing Config.LOCAL_STATE_FILE.
    """
    from config import Config
    raw_cache_dir = getattr(Config, "TRANSCRIPT_CACHE_DIR", "transcript_cache")
    if os.path.isabs(raw_cache_dir):
        return os.path.abspath(raw_cache_dir)
    
    state_file = getattr(Config, "LOCAL_STATE_FILE", "processed_files.json")
    state_dir = os.path.dirname(os.path.abspath(state_file))
    return os.path.abspath(os.path.join(state_dir, raw_cache_dir))

def save_cached_transcript(source_key: str, transcript_text: str) -> tuple[str, str]:
    """
    Saves transcript_text to resolved TRANSCRIPT_CACHE_DIR/<source_key>.txt atomically.
    Returns (cache_path, sha256_hash).
    """
    cache_dir = get_transcript_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{source_key}.txt")
    temp_path = f"{cache_path}.tmp"
    
    encoded = transcript_text.encode("utf-8")
    content_hash = hashlib.sha256(encoded).hexdigest()
    
    with open(temp_path, "wb") as f:
        f.write(encoded)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, cache_path)
    
    return cache_path, content_hash

def get_cached_transcript(cache_path: str, expected_hash: str) -> str | None:
    """
    Reads cached transcript from cache_path, verifying its sha256 expected_hash.
    Returns transcript text if valid, or None if file missing or corrupt.
    """
    if not cache_path or not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "rb") as f:
            data = f.read()
        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != expected_hash:
            log_warn("get_cached_transcript", f"Transcript cache checksum mismatch for {cache_path}.")
            return None
        return data.decode("utf-8")
    except Exception as e:
        log_warn("get_cached_transcript", f"Failed reading cached transcript {cache_path}: {redact_secrets(str(e))}")
        return None
