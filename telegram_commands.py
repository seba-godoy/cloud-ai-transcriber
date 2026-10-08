"""Phase 12 Telegram operator commands for the cloud transcriber.

This module intentionally keeps operational Telegram commands separate from the
transcription pipeline. Commands are available only to the already-authorized
TELEGRAM_CHAT_ID and use the existing Firestore durable state as their source
of truth.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import html
from typing import Iterable

from config import Config
from state_backend import RecoveryRefusedError
from telegram_notifier import is_authorized_telegram_message, send_telegram_message
from utils import StatePersistenceError, log_error, log_warn, redact_secrets

PHASE12_VERSION = "v1.1.0-cloud"
MAX_QUEUE_ITEMS = 8
MAX_NAME_CHARS = 64
MAX_ERROR_CHARS = 220


def _safe_text(value, limit: int | None = None) -> str:
    text = str(value or "").replace("\n", " ").strip()
    text = redact_secrets(text)
    if limit and len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def _format_time(value) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return _safe_text(value, 40)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return _safe_text(value, 40)


def _sort_time(job: dict) -> datetime:
    for key in ("updated_at", "last_attempt_at", "created_at"):
        value = job.get(key)
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def _short_id(job: dict) -> str:
    key = _safe_text(job.get("source_key"))
    return key[:8] if key else "????????"


def _status_icon(status: str) -> str:
    return {
        "completed": "✅",
        "in_progress": "⏳",
        "retryable": "🔁",
        "failed_permanent": "❌",
    }.get(status, "•")


class TelegramCommandRuntime:
    """Processes operator commands while preserving main.py's durable offset flow."""

    def __init__(self):
        self.backend = None

    def bind_backend(self, backend) -> None:
        self.backend = backend

    def process_updates(self, updates: Iterable[dict]) -> list[dict]:
        """Handle authorized commands and return updates with commands neutralized.

        Command updates remain in the returned sequence with an empty text message,
        allowing main.py to advance and persist the Telegram offset exactly once.
        """
        result = []
        for original in updates or []:
            update = original
            message = original.get("message") if isinstance(original, dict) else None
            if isinstance(message, dict) and is_authorized_telegram_message(message):
                text = message.get("text")
                if isinstance(text, str) and text.strip().startswith("/"):
                    try:
                        consumed = self.handle_command(text.strip())
                    except Exception as exc:
                        log_error("telegram_command", redact_secrets(str(exc)))
                        send_telegram_message(
                            "⚠️ No pude ejecutar ese comando. El bot de transcripción seguirá funcionando normalmente."
                        )
                        consumed = True
                    if consumed:
                        update = deepcopy(original)
                        update["message"] = {"chat": deepcopy(message.get("chat", {})), "text": ""}
            result.append(update)
        return result

    def handle_command(self, text: str) -> bool:
        parts = text.split()
        if not parts:
            return False
        command = parts[0].split("@", 1)[0].casefold()
        args = parts[1:]

        if command in ("/start", "/ayuda", "/help"):
            self._send_help()
            return True
        if command in ("/estado", "/status"):
            self._send_status()
            return True
        if command in ("/cola", "/queue"):
            self._send_queue()
            return True
        if command in ("/trabajo", "/job"):
            self._send_job(args)
            return True
        if command in ("/reintentar", "/retry"):
            self._retry_job(args)
            return True
        return False

    def _require_firestore(self):
        if self.backend is None or getattr(self.backend, "backend_name", None) != "firestore":
            raise RuntimeError("Los comandos operativos requieren el backend Firestore del bot cloud.")
        return self.backend

    def _jobs(self) -> list[dict]:
        backend = self._require_firestore()
        try:
            snapshots = backend.client.collection("transcription_jobs").stream()
            jobs = []
            for snap in snapshots:
                data = snap.to_dict() or {}
                if not data.get("source_key"):
                    snap_id = getattr(snap, "id", None)
                    if snap_id:
                        data["source_key"] = snap_id
                jobs.append(data)
            return sorted(jobs, key=_sort_time, reverse=True)
        except StatePersistenceError:
            raise
        except Exception as exc:
            raise StatePersistenceError(
                f"Could not read transcription jobs for Telegram operations: {redact_secrets(str(exc))}"
            ) from exc

    def _resolve_job(self, token: str) -> dict:
        token = _safe_text(token).casefold()
        if not token:
            raise ValueError("Falta el ID del trabajo.")
        matches = []
        for job in self._jobs():
            key = _safe_text(job.get("source_key")).casefold()
            source_id = _safe_text(job.get("file_id") or job.get("video_id")).casefold()
            if key == token or key.startswith(token) or source_id == token or source_id.startswith(token):
                matches.append(job)
        if not matches:
            raise LookupError("No encontré un trabajo con ese ID. Usa /cola para ver IDs cortos.")
        if len(matches) > 1:
            raise LookupError("Ese ID es ambiguo. Usa más caracteres del ID mostrado por /cola.")
        return matches[0]

    def _send_help(self) -> None:
        send_telegram_message(
            "🤖 Comandos del Bot Transcriptor\n\n"
            "/estado — salud y resumen del estado durable\n"
            "/cola — trabajos activos, reintentables o fallidos\n"
            "/trabajo <id> — detalle de un trabajo\n"
            "/reintentar <id> — preparar un reintento seguro\n"
            "/ayuda — mostrar esta ayuda\n\n"
            "Los comandos son procesados por el polling actual, por lo que pueden tardar hasta el próximo ciclo."
        )

    def _send_status(self) -> None:
        jobs = self._jobs()
        counts = Counter(_safe_text(job.get("status")) or "unknown" for job in jobs)
        recent = jobs[0] if jobs else None
        lines = [
            "🟢 Bot Transcriptor operativo",
            f"Versión lógica: {PHASE12_VERSION}",
            "Backend: Firestore",
            f"Trabajos registrados: {len(jobs)}",
            (
                "Estados: "
                f"✅ {counts.get('completed', 0)} | "
                f"⏳ {counts.get('in_progress', 0)} | "
                f"🔁 {counts.get('retryable', 0)} | "
                f"❌ {counts.get('failed_permanent', 0)}"
            ),
        ]
        if recent:
            lines.extend([
                "",
                "Actividad durable más reciente:",
                f"{_status_icon(_safe_text(recent.get('status')))} [{_short_id(recent)}] {_safe_text(recent.get('name'), MAX_NAME_CHARS) or '(sin nombre)'}",
                f"Actualizado: {_format_time(recent.get('updated_at'))}",
            ])
        send_telegram_message("\n".join(lines))

    def _send_queue(self) -> None:
        jobs = self._jobs()
        active_statuses = {"in_progress", "retryable", "failed_permanent"}
        queue = [job for job in jobs if _safe_text(job.get("status")) in active_statuses]
        if not queue:
            send_telegram_message("📭 No hay trabajos activos, reintentables ni fallidos en Firestore.")
            return
        counts = Counter(_safe_text(job.get("status")) for job in queue)
        lines = [
            "📋 Cola operativa",
            f"⏳ {counts.get('in_progress', 0)} | 🔁 {counts.get('retryable', 0)} | ❌ {counts.get('failed_permanent', 0)}",
            "",
        ]
        for job in queue[:MAX_QUEUE_ITEMS]:
            status = _safe_text(job.get("status"))
            name = _safe_text(job.get("name"), MAX_NAME_CHARS) or "(sin nombre)"
            progress = ""
            completed = job.get("completed_chunks")
            total = job.get("total_chunks")
            if isinstance(completed, int) and isinstance(total, int) and total > 0:
                progress = f" · {completed}/{total} bloques"
            lines.append(f"{_status_icon(status)} [{_short_id(job)}] {name}{progress}")
        if len(queue) > MAX_QUEUE_ITEMS:
            lines.append(f"… y {len(queue) - MAX_QUEUE_ITEMS} más")
        lines.extend(["", "Detalle: /trabajo <id>"])
        send_telegram_message("\n".join(lines))

    def _send_job(self, args: list[str]) -> None:
        if not args:
            send_telegram_message("Uso: /trabajo <id>. Obtén el ID corto con /cola.")
            return
        try:
            job = self._resolve_job(args[0])
        except (ValueError, LookupError) as exc:
            send_telegram_message(f"⚠️ {exc}")
            return
        status = _safe_text(job.get("status")) or "unknown"
        completed = job.get("completed_chunks", 0)
        total = job.get("total_chunks")
        progress = f"{completed}/{total}" if isinstance(total, int) and total > 0 else str(completed)
        lines = [
            f"{_status_icon(status)} Trabajo [{_short_id(job)}]",
            f"Nombre: {_safe_text(job.get('name'), 100) or '—'}",
            f"Origen: {_safe_text(job.get('source')) or '—'}",
            f"Estado: {status}",
            f"Bloques completados: {progress}",
            f"Reintentos: {job.get('retry_count', 0)}",
            f"Actualizado: {_format_time(job.get('updated_at'))}",
        ]
        next_retry = job.get("next_retry_at")
        if next_retry:
            lines.append(f"Próximo reintento: {_format_time(next_retry)}")
        error = _safe_text(job.get("error_message") or job.get("last_error"), MAX_ERROR_CHARS)
        if error:
            lines.append(f"Error: {error}")
        if status == "failed_permanent" and _safe_text(job.get("source")) == "drive":
            lines.append(f"Reintento manual: /reintentar {_short_id(job)}")
        send_telegram_message("\n".join(lines))

    def _retry_job(self, args: list[str]) -> None:
        if not args:
            send_telegram_message("Uso: /reintentar <id>. Primero revisa /trabajo <id>.")
            return
        try:
            job = self._resolve_job(args[0])
        except (ValueError, LookupError) as exc:
            send_telegram_message(f"⚠️ {exc}")
            return

        status = _safe_text(job.get("status"))
        source = _safe_text(job.get("source"))
        short = _short_id(job)
        name = _safe_text(job.get("name"), MAX_NAME_CHARS) or "(sin nombre)"
        if status != "failed_permanent":
            send_telegram_message(f"⚠️ [{short}] no está en failed_permanent; no se modificó nada.")
            return
        if source != "drive":
            send_telegram_message("⚠️ El reintento administrativo de Fase 12 está limitado a trabajos de Drive.")
            return
        evidence = _safe_text(job.get("error_message") or job.get("last_error"))
        if not evidence:
            send_telegram_message("⚠️ El trabajo no conserva evidencia de error suficiente; reintento rechazado.")
            return

        confirmed = len(args) >= 2 and args[1].casefold() == "confirmar"
        if not confirmed:
            send_telegram_message(
                f"⚠️ Reintento protegido para [{short}] {name}\n"
                "Esto conserva chunks, IDs de salida y claims de notificación.\n\n"
                f"Para confirmar: /reintentar {short} confirmar"
            )
            return

        backend = self._require_firestore()
        source_key = _safe_text(job.get("source_key"))
        try:
            backend.requeue_permanent_failure(
                source_key,
                evidence,
                reason="telegram_operator_requeue_phase12",
            )
        except RecoveryRefusedError as exc:
            send_telegram_message(f"⚠️ Reintento rechazado de forma segura: {_safe_text(exc, 180)}")
            return
        except StatePersistenceError:
            raise
        send_telegram_message(
            f"✅ [{short}] quedó reencolado de forma segura. Se intentará en un ciclo posterior del Scheduler."
        )


def install_phase12_runtime(app_module):
    """Install a narrow composition layer without changing main.py semantics."""
    runtime = TelegramCommandRuntime()
    original_create_backend = app_module.create_state_backend
    original_get_updates = app_module.get_telegram_updates

    def create_backend_and_capture(*args, **kwargs):
        backend = original_create_backend(*args, **kwargs)
        runtime.bind_backend(backend)
        return backend

    def get_updates_and_commands(*args, **kwargs):
        updates = original_get_updates(*args, **kwargs)
        return runtime.process_updates(updates)

    app_module.create_state_backend = create_backend_and_capture
    app_module.get_telegram_updates = get_updates_and_commands
    return runtime
