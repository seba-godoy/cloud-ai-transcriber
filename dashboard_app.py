"""Read-only two-user web dashboard for the cloud transcriber."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qs

from google.cloud import firestore

USERS = {"primary": "Primary", "secondary": "Secondary"}
MAX_JOBS = 50
MAX_NAME_CHARS = 100
MAX_ERROR_CHARS = 180
ALLOWED_JOB_KEYS = {
    "id", "name", "source", "status", "owner_id", "owner_name",
    "completed_chunks", "total_chunks", "progress_pct", "updated_at", "error",
}


def _text(value, limit: int | None = None) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if limit and len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def _iso_time(value) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return _text(value, 40) or None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return _text(value, 40) or None


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


def _owner_id(job: dict) -> str:
    candidate = _text(job.get("owner_id")).casefold()
    return candidate if candidate in USERS else "primary"


def _progress(job: dict, status: str) -> tuple[int, int | None, int]:
    completed = job.get("completed_chunks")
    total = job.get("total_chunks")
    if not isinstance(completed, int) or completed < 0:
        completed = 0
    if not isinstance(total, int) or total <= 0:
        total = None
    if total:
        pct = max(0, min(100, round((completed / total) * 100)))
    elif status == "completed":
        pct = 100
    else:
        pct = 0
    return completed, total, pct


def _short_id(job: dict) -> str:
    source_key = _text(job.get("source_key"))
    if source_key:
        return source_key[:8]
    source_id = _text(job.get("file_id") or job.get("video_id"))
    return source_id[:8] if source_id else "????????"


def sanitize_job(job: dict) -> dict:
    status = _text(job.get("status")) or "unknown"
    owner_id = _owner_id(job)
    completed, total, pct = _progress(job, status)
    error = None
    if status in {"retryable", "failed_permanent"}:
        error = _text(job.get("last_error") or job.get("error"), MAX_ERROR_CHARS) or None
    result = {
        "id": _short_id(job),
        "name": _text(job.get("name"), MAX_NAME_CHARS) or "(sin nombre)",
        "source": _text(job.get("source"), 30) or "—",
        "status": status,
        "owner_id": owner_id,
        "owner_name": USERS[owner_id],
        "completed_chunks": completed,
        "total_chunks": total,
        "progress_pct": pct,
        "updated_at": _iso_time(job.get("updated_at") or job.get("last_attempt_at") or job.get("created_at")),
        "error": error,
    }
    assert set(result) == ALLOWED_JOB_KEYS
    return result


def build_payload(raw_jobs: Iterable[dict], selected_user: str) -> dict:
    if selected_user not in USERS:
        raise ValueError("unknown user")
    ordered = sorted((dict(job) for job in raw_jobs), key=_sort_time, reverse=True)
    jobs = [sanitize_job(job) for job in ordered]
    jobs = [job for job in jobs if job["owner_id"] == selected_user][:MAX_JOBS]
    counts = Counter(job["status"] for job in jobs)
    return {
        "user": {"id": selected_user, "name": USERS[selected_user]},
        "summary": {
            "total": len(jobs),
            "completed": counts.get("completed", 0),
            "in_progress": counts.get("in_progress", 0),
            "retryable": counts.get("retryable", 0),
            "failed_permanent": counts.get("failed_permanent", 0),
        },
        "jobs": jobs,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


class FirestoreJobReader:
    def __init__(self) -> None:
        project_id = os.getenv("FIRESTORE_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
        database_id = os.getenv("FIRESTORE_DATABASE_ID", "(default)")
        self.client = firestore.Client(project=project_id, database=database_id)

    def list_jobs(self) -> list[dict]:
        jobs = []
        for snapshot in self.client.collection("transcription_jobs").stream():
            data = snapshot.to_dict() or {}
            if not data.get("source_key"):
                data["source_key"] = snapshot.id
            jobs.append(data)
        return jobs


def _authenticated_email(environ: dict) -> str | None:
    value = _text(environ.get("HTTP_X_GOOG_AUTHENTICATED_USER_EMAIL"))
    if not value:
        return None
    if ":" in value:
        _, value = value.split(":", 1)
    return value.casefold()


def _allowed_emails() -> set[str]:
    raw = os.getenv("DASHBOARD_ALLOWED_EMAILS", "")
    return {item.strip().casefold() for item in raw.split(",") if item.strip()}


def _respond_json(start_response, status: str, payload: dict):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    start_response(status, [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
    ])
    return [body]


def _respond_html(start_response):
    path = Path(__file__).with_name("dashboard") / "index.html"
    body = path.read_bytes()
    start_response("200 OK", [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'"),
    ])
    return [body]


class DashboardApplication:
    def __init__(
        self,
        job_loader: Callable[[], list[dict]] | None = None,
        require_iap: bool | None = None,
        allowed_emails: set[str] | None = None,
    ) -> None:
        self._job_loader = job_loader
        self._reader = None
        self.require_iap = require_iap if require_iap is not None else (
            os.getenv("DASHBOARD_REQUIRE_IAP", "true").casefold() in {"1", "true", "yes"}
        )
        self.allowed_emails = allowed_emails if allowed_emails is not None else _allowed_emails()

    def _jobs(self) -> list[dict]:
        if self._job_loader is not None:
            return self._job_loader()
        if self._reader is None:
            self._reader = FirestoreJobReader()
        return self._reader.list_jobs()

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "/")
        email = _authenticated_email(environ)

        if path == "/healthz":
            return _respond_json(start_response, "200 OK", {"ok": True})
        if self.require_iap and not email:
            return _respond_json(start_response, "401 Unauthorized", {"error": "authentication required"})
        if self.allowed_emails and email not in self.allowed_emails:
            return _respond_json(start_response, "403 Forbidden", {"error": "not authorized"})
        if path == "/":
            return _respond_html(start_response)
        if path == "/api/jobs":
            params = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            selected_user = (params.get("user") or ["primary"])[0].casefold()
            if selected_user not in USERS:
                return _respond_json(start_response, "400 Bad Request", {"error": "unknown user"})
            try:
                payload = build_payload(self._jobs(), selected_user)
            except Exception:
                return _respond_json(start_response, "503 Service Unavailable", {"error": "dashboard data unavailable"})
            payload["viewer"] = email
            return _respond_json(start_response, "200 OK", payload)
        return _respond_json(start_response, "404 Not Found", {"error": "not found"})


app = DashboardApplication()
