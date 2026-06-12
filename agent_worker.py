import json
import os
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from agent_hooks import emit_trace
from agent_verification import DEFAULT_COMPILE_COMMAND
from core_tools import PROJECT_CACHE_DIR, ROOT_DIR


WORKER_JOBS_FILE = os.path.join(PROJECT_CACHE_DIR, "worker_jobs.jsonl")
WORKER_RESULTS_FILE = os.path.join(PROJECT_CACHE_DIR, "worker_results.jsonl")

ALLOWED_VERIFIER_COMMANDS = {
    "py_compile": list(DEFAULT_COMPILE_COMMAND),
    "self_test": ["python", "self_test.py"],
    "agent_eval": ["python", "agent_eval.py"],
    "trace_summary": ["python", "agent_observability.py"],
}


@dataclass
class WorkerJob:
    job_id: str
    kind: str
    command: list[str]
    cwd: str = ROOT_DIR
    timeout: int = 120
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkerResult:
    job_id: str
    kind: str
    status: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    evidence: list[str] = field(default_factory=list)
    duration_ms: int = 0
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


class WorkerQueue:
    def __init__(self, jobs_path: str = WORKER_JOBS_FILE, results_path: str = WORKER_RESULTS_FILE, allowed_commands: dict[str, list[str]] | None = None):
        self.jobs_path = jobs_path
        self.results_path = results_path
        self.allowed_commands = allowed_commands or ALLOWED_VERIFIER_COMMANDS
        self._lock = threading.RLock()

    def submit_verifier(self, command_name: str, timeout: int = 120, metadata: dict[str, Any] | None = None) -> WorkerJob:
        if command_name not in self.allowed_commands:
            raise ValueError(f"Verifier command is not allowed: {command_name}")
        job = WorkerJob(
            job_id=f"worker_{int(time.time())}_{uuid.uuid4().hex[:8]}",
            kind=command_name,
            command=list(self.allowed_commands[command_name]),
            timeout=max(1, min(int(timeout), 600)),
            metadata=metadata or {},
        )
        self._append_jsonl(self.jobs_path, asdict(job))
        emit_trace("worker.job_submitted", job_id=job.job_id, kind=job.kind, command=job.command)
        return job

    def start_verifier(self, command_name: str, timeout: int = 120, metadata: dict[str, Any] | None = None) -> WorkerJob:
        job = self.submit_verifier(command_name, timeout=timeout, metadata=metadata)
        worker = VerifierWorker(self)
        thread = threading.Thread(target=worker.run_job, args=(job,), daemon=True)
        thread.start()
        return job

    def record_job_status(self, job: WorkerJob, status: str) -> WorkerJob:
        job.status = status
        now = time.time()
        if status == "running":
            job.started_at = now
        elif status in {"done", "failed"}:
            job.finished_at = now
        self._append_jsonl(self.jobs_path, asdict(job))
        emit_trace("worker.job_status", job_id=job.job_id, kind=job.kind, status=status)
        return job

    def record_result(self, result: WorkerResult) -> WorkerResult:
        self._append_jsonl(self.results_path, asdict(result))
        emit_trace(
            "worker.result",
            job_id=result.job_id,
            kind=result.kind,
            status=result.status,
            returncode=result.returncode,
            duration_ms=result.duration_ms,
            error=result.error,
        )
        return result

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        return _read_jsonl(self.jobs_path)[-limit:]

    def list_results(self, limit: int = 50) -> list[dict[str, Any]]:
        return _read_jsonl(self.results_path)[-limit:]

    def latest_result(self, job_id: str) -> dict[str, Any] | None:
        for row in reversed(self.list_results(limit=500)):
            if row.get("job_id") == job_id:
                return row
        return None

    def _append_jsonl(self, path: str, payload: dict[str, Any]) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as file:
                file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


class VerifierWorker:
    def __init__(self, queue: WorkerQueue | None = None):
        self.queue = queue or WorkerQueue()

    def run_job(self, job: WorkerJob) -> WorkerResult:
        if job.kind not in self.queue.allowed_commands or job.command != self.queue.allowed_commands[job.kind]:
            result = WorkerResult(job.job_id, job.kind, "failed", error="command is not in verifier allowlist", metadata=dict(job.metadata))
            self.queue.record_result(result)
            self.queue.record_job_status(job, "failed")
            return result

        self.queue.record_job_status(job, "running")
        started = time.time()
        try:
            completed = subprocess.run(
                job.command,
                cwd=job.cwd,
                capture_output=True,
                text=True,
                timeout=job.timeout,
            )
            duration_ms = int((time.time() - started) * 1000)
            status = "done" if completed.returncode == 0 else "failed"
            result = WorkerResult(
                job_id=job.job_id,
                kind=job.kind,
                status=status,
                returncode=completed.returncode,
                stdout=_truncate(completed.stdout or ""),
                stderr=_truncate(completed.stderr or ""),
                evidence=[
                    f"command: {' '.join(job.command)}",
                    f"returncode: {completed.returncode}",
                    _truncate("stdout: " + (completed.stdout or "").strip()),
                    _truncate("stderr: " + (completed.stderr or "").strip()),
                ],
                duration_ms=duration_ms,
                metadata=dict(job.metadata),
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.time() - started) * 1000)
            result = WorkerResult(
                job_id=job.job_id,
                kind=job.kind,
                status="failed",
                stdout=_truncate((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
                stderr=_truncate((exc.stderr or "") if isinstance(exc.stderr, str) else ""),
                evidence=[f"command: {' '.join(job.command)}", f"timeout: {job.timeout}s"],
                duration_ms=duration_ms,
                error="timeout",
                metadata=dict(job.metadata),
            )
        self.queue.record_result(result)
        self.queue.record_job_status(job, result.status)
        return result


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8", errors="replace") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({"status": "decode_error", "raw": line[:300]})
    return rows


def _truncate(text: str, limit: int = 2000) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"
