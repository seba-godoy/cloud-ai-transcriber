"""Register Phase 12 Telegram command metadata using the Bot API.

The bot token must be supplied through TELEGRAM_BOT_TOKEN and the authorized
operator chat through TELEGRAM_CHAT_ID. The tool scopes the visible command
menu to that one chat, never prints the token, and is intended for a one-time
controlled deployment step rather than every scheduled execution.
"""
from __future__ import annotations

import json
import os
import sys

import requests

COMMANDS = [
    {"command": "ayuda", "description": "Ver comandos disponibles"},
    {"command": "estado", "description": "Ver salud y estado durable"},
    {"command": "cola", "description": "Ver trabajos activos o fallidos"},
    {"command": "trabajo", "description": "Ver detalle de un trabajo por ID"},
    {"command": "reintentar", "description": "Preparar reintento seguro de Drive"},
]


def _parse_chat_id(chat_id: str):
    value = str(chat_id or "").strip()
    if not value:
        raise ValueError("TELEGRAM_CHAT_ID is required")
    try:
        return int(value)
    except ValueError:
        if value.startswith("@") and len(value) > 1:
            return value
        raise ValueError("TELEGRAM_CHAT_ID must be a numeric chat ID or @username")


def register(token: str, chat_id: str, post_fn=requests.post) -> bool:
    if not token or not token.strip():
        raise ValueError("TELEGRAM_BOT_TOKEN is required")
    scoped_chat = _parse_chat_id(chat_id)
    url = f"https://api.telegram.org/bot{token.strip()}/setMyCommands"
    response = post_fn(
        url,
        json={
            "commands": COMMANDS,
            "scope": {"type": "chat", "chat_id": scoped_chat},
        },
        timeout=10,
    )
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"Telegram setMyCommands returned non-JSON HTTP {response.status_code}") from exc
    if not isinstance(payload, dict) or not payload.get("ok"):
        code = payload.get("error_code", response.status_code) if isinstance(payload, dict) else response.status_code
        desc = payload.get("description", "unknown error") if isinstance(payload, dict) else "unknown error"
        raise RuntimeError(f"Telegram setMyCommands failed ({code}): {desc}")
    return True


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    try:
        register(token, chat_id)
    except Exception as exc:
        print(f"FAILED: {exc}")
        return 1
    print(json.dumps({
        "ok": True,
        "scope": "authorized_chat",
        "commands": [item["command"] for item in COMMANDS],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
