import requests
import os
import math
import html
import time
import random
from config import Config
from utils import (
    log_error,
    log_warn,
    redact_secrets,
    TelegramAPIError,
    TelegramDeliveryError,
    calculate_backoff,
    is_network_or_dns_error,
)

def is_authorized_telegram_message(message: dict) -> bool:
    """
    Zero-trust Telegram message authorization check comparing chat.id with TELEGRAM_CHAT_ID.
    """
    expected_chat_id = str(Config.TELEGRAM_CHAT_ID or "").strip()
    if not expected_chat_id:
        return False
        
    if not message or not isinstance(message, dict):
        return False
        
    chat = message.get("chat")
    if not chat or not isinstance(chat, dict):
        return False
        
    actual_chat_id = chat.get("id")
    if actual_chat_id is None:
        return False
        
    return str(actual_chat_id).strip() == expected_chat_id

def send_telegram_message(message_text: str, parse_mode: str = None) -> bool:
    """
    Sends a text message to Telegram without propagating exceptions (required=False).
    Returns True if sent successfully, False otherwise.
    """
    return send_telegram_message_with_retry(message_text, parse_mode=parse_mode, attempts=3, required=False)

AUTO_RETRY_TELEGRAM_CODES = {408, 429, 500, 502, 503, 504}

def is_permanent_telegram_status(status_code: int | None) -> bool:
    """
    Returns True for any HTTP 4xx error code except 408 and 429.
    """
    if status_code is None:
        return False
    return 400 <= status_code < 500 and status_code not in AUTO_RETRY_TELEGRAM_CODES

def send_telegram_message_with_retry(
    message_text: str,
    parse_mode: str = None,
    attempts: int = 3,
    required: bool = False,
    sleep_fn = None,
    jitter_fn = None
) -> bool:
    """
    Sends a message to Telegram with up to `attempts` retries.
    If required=True, raises TelegramDeliveryError after exhausting retries or on permanent error.
    If required=False, logs warning and returns False after exhausting retries.
    """
    _sleep = sleep_fn or time.sleep
    _jitter = jitter_fn or random.uniform
    
    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        log_warn("send_telegram_message_with_retry", "Telegram credentials missing. Skipping notification.")
        if required:
            raise TelegramDeliveryError("Telegram credentials missing.", transient=False)
        return False

    url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": Config.TELEGRAM_CHAT_ID,
        "text": message_text
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    last_exception = None

    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(url, json=payload, timeout=10)
            data = None
            json_error = None
            try:
                data = response.json()
            except Exception as ex:
                json_error = ex

            if json_error is not None:
                status = response.status_code
                if status in AUTO_RETRY_TELEGRAM_CODES:
                    api_err = TelegramAPIError(error_code=status or 500, description=f"Non-JSON HTTP {status} response")
                    raise api_err
                elif is_permanent_telegram_status(status):
                    api_err = TelegramAPIError(error_code=status, description=f"Non-JSON HTTP {status} response")
                    log_warn("send_telegram_message_with_retry", f"Permanent Telegram error {status}: Non-JSON HTTP {status} response")
                    if required:
                        raise TelegramDeliveryError(f"Permanent Telegram error {status}: Non-JSON HTTP {status} response", transient=False) from api_err
                    return False
                elif status and 200 <= status < 300:
                    raise ValueError("Telegram API returned non-JSON response on 2xx HTTP status.") from json_error
                else:
                    api_err = TelegramAPIError(error_code=status or 500, description=f"Non-JSON HTTP {status} response")
                    raise api_err

            if not data.get("ok"):
                err_code = data.get("error_code") or response.status_code or 500
                desc = data.get("description", "Unknown Telegram error")
                retry_after = data.get("parameters", {}).get("retry_after")
                api_err = TelegramAPIError(error_code=err_code, description=desc, retry_after=retry_after)
                
                if is_permanent_telegram_status(err_code):
                    log_warn("send_telegram_message_with_retry", f"Permanent Telegram error {err_code}: {redact_secrets(desc)}")
                    if required:
                        raise TelegramDeliveryError(f"Permanent Telegram error {err_code}: {desc}", transient=False) from api_err
                    return False
                raise api_err

            return True

        except TelegramDeliveryError:
            raise
        except Exception as e:
            last_exception = e
            is_perm = False
            if isinstance(e, TelegramAPIError) and is_permanent_telegram_status(e.error_code):
                is_perm = True
            elif hasattr(e, 'response') and is_permanent_telegram_status(getattr(e.response, 'status_code', None)):
                is_perm = True

            if is_perm:
                log_warn("send_telegram_message_with_retry", f"Permanent Telegram error: {redact_secrets(str(e))}")
                if required:
                    raise TelegramDeliveryError(f"Permanent Telegram error: {str(e)}", transient=False) from e
                return False

            if attempt < attempts:
                retry_after = getattr(e, 'retry_after', None)
                if retry_after is not None and isinstance(retry_after, (int, float)) and retry_after > 0:
                    sleep_dur = float(retry_after)
                else:
                    sleep_dur = calculate_backoff(attempt, base_delay=2.0, max_delay=30.0, jitter_fn=_jitter)

                log_warn("send_telegram_message_with_retry", f"Telegram error detected (Attempt {attempt}/{attempts}). Retrying in {sleep_dur:.1f}s...")
                _sleep(sleep_dur)

    log_warn("send_telegram_message_with_retry", f"Failed to send Telegram message after {attempts} attempts: {redact_secrets(str(last_exception))}")
    if required:
        raise TelegramDeliveryError(f"Failed to deliver Telegram message after {attempts} attempts.", transient=True) from last_exception
    return False

def get_telegram_updates(
    offset: int = None, 
    timeout: int = 30, 
    attempts: int = 3, 
    sleep_fn = None, 
    jitter_fn = None
) -> list:
    """
    Polls getUpdates from Telegram API with up to `attempts` retries for transient errors.
    Returns list of update dicts ONLY when ok=True.
    Raises TelegramAPIError on ok=False or permanent/exhausted API errors.
    Raises ValueError on non-JSON response on 2xx or when ok=True but 'result' is not a list.
    """
    _sleep = sleep_fn or time.sleep
    _jitter = jitter_fn or random.uniform

    if not Config.TELEGRAM_BOT_TOKEN:
        log_warn("get_telegram_updates", "Telegram bot token missing.")
        return []

    url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": timeout}
    if offset:
        params["offset"] = offset

    last_exception = None

    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, params=params, timeout=timeout + 5)
            
            data = None
            json_error = None
            try:
                data = response.json()
            except Exception as ex:
                json_error = ex

            if json_error is not None:
                status = response.status_code
                if status in AUTO_RETRY_TELEGRAM_CODES:
                    api_err = TelegramAPIError(error_code=status, description=f"Non-JSON HTTP {status} response")
                    log_warn("get_telegram_updates", f"Transient Telegram HTTP {status} (non-JSON) (Attempt {attempt}/{attempts})")
                    raise api_err
                elif is_permanent_telegram_status(status):
                    api_err = TelegramAPIError(error_code=status, description=f"Non-JSON HTTP {status} response")
                    if status == 409:
                        log_warn("get_telegram_updates", "409 Conflict: Another active bot instance is likely running with this token!")
                    else:
                        log_warn("get_telegram_updates", f"Permanent Telegram HTTP {status} (non-JSON)")
                    raise api_err
                elif status and 200 <= status < 300:
                    raise ValueError("Telegram API returned non-JSON response on 2xx HTTP status.") from json_error
                else:
                    api_err = TelegramAPIError(error_code=status or 500, description=f"Non-JSON HTTP {status} response")
                    raise api_err

            if not isinstance(data, dict):
                raise ValueError("Telegram API response is not a JSON object.")

            if not data.get("ok"):
                err_code = data.get("error_code") or response.status_code or 500
                desc = data.get("description", "Unknown Telegram API Error")
                retry_after = data.get("parameters", {}).get("retry_after")
                api_err = TelegramAPIError(error_code=err_code, description=desc, retry_after=retry_after)

                if err_code == 409:
                    log_warn("get_telegram_updates", f"409 Conflict: Another active bot instance is likely running with this token! ({redact_secrets(desc)})")
                    raise api_err
                elif is_permanent_telegram_status(err_code):
                    log_warn("get_telegram_updates", f"Permanent Telegram error {err_code}: {redact_secrets(desc)}")
                    raise api_err
                elif err_code in AUTO_RETRY_TELEGRAM_CODES:
                    log_warn("get_telegram_updates", f"Transient Telegram error {err_code} (Attempt {attempt}/{attempts}): {redact_secrets(desc)}")
                    raise api_err
                else:
                    log_warn("get_telegram_updates", f"Telegram error {err_code}: {redact_secrets(desc)}")
                    raise api_err

            if "result" not in data or not isinstance(data["result"], list):
                raise ValueError(f"Telegram API response ok=True but 'result' is not a list (got {type(data.get('result')).__name__}).")

            return data["result"]

        except (ValueError, TelegramAPIError) as err:
            last_exception = err
            if isinstance(err, ValueError):
                raise err
            if isinstance(err, TelegramAPIError):
                if is_permanent_telegram_status(err.error_code) or err.error_code not in AUTO_RETRY_TELEGRAM_CODES:
                    raise err

            if attempt >= attempts:
                raise err

            retry_after = getattr(err, "retry_after", None)
            if retry_after is not None and isinstance(retry_after, (int, float)) and retry_after > 0:
                sleep_dur = float(retry_after)
            else:
                sleep_dur = calculate_backoff(attempt, base_delay=2.0, max_delay=30.0, jitter_fn=_jitter)

            log_warn("get_telegram_updates", f"Retrying in {sleep_dur:.1f}s...")
            _sleep(sleep_dur)

        except Exception as net_err:
            last_exception = net_err
            if not is_network_or_dns_error(net_err) and not isinstance(net_err, (requests.ConnectionError, requests.Timeout)):
                raise net_err
            if attempt >= attempts:
                log_warn("get_telegram_updates", f"Polling failed after {attempts} attempts: {redact_secrets(str(net_err))}")
                raise net_err
            sleep_dur = calculate_backoff(attempt, base_delay=2.0, max_delay=30.0, jitter_fn=_jitter)
            log_warn("get_telegram_updates", f"Connection error on polling (Attempt {attempt}/{attempts}). Retrying in {sleep_dur:.1f}s...")
            _sleep(sleep_dur)

    raise last_exception

def get_telegram_file_info(file_id: str) -> dict:
    """
    Gets file path info from Telegram API for a file_id.
    Raises TelegramAPIError when ok=False.
    Raises ValueError when ok=True but result/file_path is missing.
    """
    url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/getFile"
    response = requests.get(url, params={"file_id": file_id}, timeout=10)
    
    try:
        data = response.json()
    except Exception:
        response.raise_for_status()
        raise ValueError("Telegram API returned non-JSON response.")

    if not data.get("ok"):
        error_code = data.get("error_code") or response.status_code or 500
        description = data.get("description", "Unknown Telegram API Error")
        retry_after = data.get("parameters", {}).get("retry_after")
        raise TelegramAPIError(error_code=error_code, description=description, retry_after=retry_after)

    result = data.get("result")
    if not result or not isinstance(result, dict) or not result.get("file_path"):
        raise ValueError("Telegram API response ok=True but missing 'result' or 'file_path'.")

    return result

def download_telegram_file(file_path: str, save_path: str, expected_size: int = None):
    """
    Downloads file from Telegram servers in streaming chunks with byte size limit enforcement.
    Interrupts and deletes temp file if TELEGRAM_MAX_DOWNLOAD_BYTES is exceeded.
    """
    if expected_size and expected_size > Config.TELEGRAM_MAX_DOWNLOAD_BYTES:
        raise ValueError(f"File size ({expected_size} bytes) exceeds limit ({Config.TELEGRAM_MAX_DOWNLOAD_BYTES} bytes).")

    url = f"https://api.telegram.org/file/bot{Config.TELEGRAM_BOT_TOKEN}/{file_path}"
    
    response = requests.get(url, stream=True, timeout=30)
    response.raise_for_status()
    
    downloaded_bytes = 0
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    
    try:
        with open(save_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    downloaded_bytes += len(chunk)
                    if downloaded_bytes > Config.TELEGRAM_MAX_DOWNLOAD_BYTES:
                        raise ValueError(f"Downloaded bytes ({downloaded_bytes}) exceeded limit ({Config.TELEGRAM_MAX_DOWNLOAD_BYTES}).")
                    f.write(chunk)
    except Exception:
        if os.path.exists(save_path):
            try:
                os.remove(save_path)
            except Exception:
                pass
        raise

def split_long_text(text: str, max_chars: int = 4000) -> list[str]:
    """
    Splits long text strictly into chunks <= max_chars.
    Priority order for split boundaries:
    1. Double newline (\n\n)
    2. Single newline (\n)
    3. Space (' ')
    4. Forced split (hard cut at max_chars)
    Guarantees "".join(chunks) == text (exact reconstruction, no loss, no duplication).
    """
    if not text:
        return []
        
    chunks = []
    current = text
    
    while len(current) > max_chars:
        pos = current.rfind("\n\n", 0, max_chars)
        if pos != -1:
            split_at = pos + 2
        else:
            pos = current.rfind("\n", 0, max_chars)
            if pos != -1:
                split_at = pos + 1
            else:
                pos = current.rfind(" ", 0, max_chars)
                if pos != -1:
                    split_at = pos + 1
                else:
                    split_at = max_chars

        chunks.append(current[:split_at])
        current = current[split_at:]

    if current:
        chunks.append(current)

    return chunks

def send_long_telegram_message(
    full_text: str,
    max_chars: int = 4000,
    parse_mode: str = None,
    required: bool = False,
    sleep_fn = None,
    jitter_fn = None
) -> bool:
    """
    Sends long messages in chunks <= max_chars to Telegram.
    Returns True ONLY if ALL chunks were delivered successfully.
    Stops immediately if any chunk fails and does not send subsequent chunks.
    """
    chunks = split_long_text(full_text, max_chars=max_chars)
    for chunk in chunks:
        success = send_telegram_message_with_retry(
            chunk,
            parse_mode=parse_mode,
            attempts=3,
            required=required,
            sleep_fn=sleep_fn,
            jitter_fn=jitter_fn
        )
        if not success:
            return False
    return True
