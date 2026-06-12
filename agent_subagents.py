import subprocess
import time
import json
import os
import uuid
from dataclasses import asdict, dataclass

from agent_hooks import emit_trace
from agent_worker import ALLOWED_VERIFIER_COMMANDS, WorkerJob, WorkerQueue
from core_tools import PROJECT_CACHE_DIR


SUBAGENT_RUNS_FILE = os.path.join(PROJECT_CACHE_DIR, "subagent_runs.jsonl")


@dataclass
class SubagentSpec:
    name: str
    purpose: str
    allowed_tools: list[str]
    context_policy: str
    can_write_state: bool = False


@dataclass
class SubagentResult:
    name: str
    summary: str
    evidence: list[str]
    status: str = "ok"
    duration_ms: int = 0
    run_id: str = ""


class SubagentLite:
    def __init__(self, spec: SubagentSpec):
        self.spec = spec

    def run(self, task: str, evidence: list[str] | None = None) -> SubagentResult:
        evidence = evidence or []
        result = SubagentResult(
            name=self.spec.name,
            summary=f"{self.spec.name} prepared task: {task}",
            evidence=evidence,
            run_id=_new_run_id(self.spec.name),
        )
        _record_subagent_run(self.spec, result, task)
        return result

    def assert_tool_allowed(self, tool_name: str) -> None:
        if tool_name not in self.spec.allowed_tools:
            emit_trace("subagent.tool_blocked", subagent=self.spec.name, tool=tool_name)
            raise PermissionError(f"{self.spec.name} cannot use tool: {tool_name}")

    def verify_command(self, command: list[str], cwd: str, timeout: int = 120) -> SubagentResult:
        if self.spec.name != "Verifier":
            return SubagentResult(self.spec.name, "verify_command is only available to Verifier", [], status="error")
        self.assert_tool_allowed("execute_command")
        if list(command) not in [list(item) for item in ALLOWED_VERIFIER_COMMANDS.values()]:
            result = SubagentResult(self.spec.name, "Verifier command is outside allowlist.", [], status="error", run_id=_new_run_id(self.spec.name))
            _record_subagent_run(self.spec, result, "verify_command blocked")
            return result
        started = time.time()
        try:
            result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
            status = "ok" if result.returncode == 0 else "error"
            evidence = [
                f"command: {' '.join(command)}",
                f"returncode: {result.returncode}",
                _truncate("stdout: " + (result.stdout or "").strip()),
                _truncate("stderr: " + (result.stderr or "").strip()),
            ]
            summary = "Verifier command passed." if status == "ok" else "Verifier command failed."
        except subprocess.TimeoutExpired:
            status = "error"
            evidence = [f"command: {' '.join(command)}", f"timeout: {timeout}s"]
            summary = "Verifier command timed out."
        duration_ms = int((time.time() - started) * 1000)
        emit_trace("subagent.verifier", status=status, command=command, cwd=cwd, duration_ms=duration_ms, summary=summary)
        result = SubagentResult(self.spec.name, summary, evidence, status=status, duration_ms=duration_ms, run_id=_new_run_id(self.spec.name))
        _record_subagent_run(self.spec, result, "verify_command")
        return result

    def submit_verifier_job(self, command_name: str, timeout: int = 120, queue: WorkerQueue | None = None) -> WorkerJob:
        if self.spec.name != "Verifier":
            raise ValueError("submit_verifier_job is only available to Verifier")
        self.assert_tool_allowed("execute_command")
        queue = queue or WorkerQueue()
        job = queue.start_verifier(command_name, timeout=timeout, metadata={"subagent": self.spec.name, "run_id": _new_run_id(self.spec.name)})
        emit_trace("subagent.worker_submitted", subagent=self.spec.name, job_id=job.job_id, kind=command_name)
        return job


BUILTIN_SUBAGENTS = {
    "Explorer": SubagentSpec("Explorer", "Read-only codebase and trace exploration.", ["read_file", "search_in_files", "list_files", "search_knowledge"], "summary-only"),
    "Verifier": SubagentSpec("Verifier", "Run deterministic tests and report evidence.", ["execute_command"], "test-output-only"),
    "Reviewer": SubagentSpec("Reviewer", "Inspect results for risks and missing tests.", ["read_file", "search_in_files", "search_knowledge"], "findings-only"),
}


def get_subagent(name: str) -> SubagentLite:
    return SubagentLite(BUILTIN_SUBAGENTS[name])


def _truncate(text: str, limit: int = 1200) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _new_run_id(name: str) -> str:
    return f"{name.lower()}_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def _record_subagent_run(spec: SubagentSpec, result: SubagentResult, task: str) -> None:
    payload = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "task": task[:300],
        "spec": asdict(spec),
        "result": asdict(result),
    }
    try:
        os.makedirs(os.path.dirname(SUBAGENT_RUNS_FILE), exist_ok=True)
        with open(SUBAGENT_RUNS_FILE, "a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        emit_trace("subagent.run", subagent=spec.name, status=result.status, run_id=result.run_id)
    except Exception as exc:
        emit_trace("subagent.run_failed", subagent=spec.name, error=str(exc))
