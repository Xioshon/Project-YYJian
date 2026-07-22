
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
OUTBOX_FILE = ROOT / "workspace" / "project_cache" / "telegram_outbox.jsonl"


def _safe_text(value: Any, limit: int = 500) -> str:
    text = str(value or "")
    text = text.replace("\r", " ").replace("\n", " ")
    if len(text) > limit:
        return text[:limit] + "..."
    return text


_ROTATE_BYTES = 10 * 1024 * 1024


def _append(event: dict[str, Any]) -> None:
    OUTBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        if OUTBOX_FILE.exists() and OUTBOX_FILE.stat().st_size > _ROTATE_BYTES:
            OUTBOX_FILE.replace(OUTBOX_FILE.with_suffix(OUTBOX_FILE.suffix + ".1"))
    except OSError:
        pass
    event = dict(event)
    event.setdefault("ts", time.time())
    with OUTBOX_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def start_send_job(kind: str, chat_id: int | str, identity: str = "", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    job = {
        "job_id": uuid.uuid4().hex[:12],
        "kind": str(kind or "unknown"),
        "chat_id": str(chat_id),
        "identity": _safe_text(identity, 220),
        "status": "pending",
        "attempts": 0,
        "metadata": dict(metadata or {}),
        "created_at": time.time(),
    }
    _append({"event": "send.pending", **job})
    return job


def mark_send_job_sent(job: dict[str, Any], result: Any = None) -> dict[str, Any]:
    job = dict(job or {})
    job["status"] = "sent"
    job["sent_at"] = time.time()
    job["attempts"] = int(job.get("attempts") or 0) + 1

    message_id = getattr(result, "message_id", None)
    if message_id is not None:
        job["telegram_message_id"] = message_id

    _append({"event": "send.sent", **job})
    return job


def mark_send_job_failed(job: dict[str, Any], exc: BaseException | str, retryable: bool = False) -> dict[str, Any]:
    job = dict(job or {})
    job["status"] = "failed"
    job["failed_at"] = time.time()
    job["attempts"] = int(job.get("attempts") or 0) + 1
    job["retryable"] = bool(retryable)
    job["error_type"] = type(exc).__name__ if isinstance(exc, BaseException) else "Error"
    job["error"] = _safe_text(exc, 800)

    _append({"event": "send.failed", **job})
    return job


def _load_recent_events(limit: int = 200) -> list[dict[str, Any]]:
    if not OUTBOX_FILE.exists():
        return []

    try:
        lines = OUTBOX_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []

    events = []
    for line in lines[-max(1, limit):]:
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                events.append(item)
        except Exception:
            continue
    return events


def outbox_operational_summary(limit: int = 200) -> dict[str, Any]:
    events = _load_recent_events(limit)
    jobs: dict[str, dict[str, Any]] = {}

    for event in events:
        job_id = str(event.get("job_id") or "")
        if not job_id:
            continue
        jobs[job_id] = event

    final = list(jobs.values())
    failed = [j for j in final if j.get("status") == "failed"]
    pending = [j for j in final if j.get("status") == "pending"]
    sent = [j for j in final if j.get("status") == "sent"]

    last_error = ""
    if failed:
        last = failed[-1]
        last_error = f"{last.get('kind')}: {last.get('error_type')}: {last.get('error')}"

    return {
        "jobs": len(final),
        "sent": len(sent),
        "failed": len(failed),
        "pending": len(pending),
        "last_error": last_error,
        "file": str(OUTBOX_FILE),
    }
