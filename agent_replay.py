import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from agent_hooks import TRACE_LOG_FILE, emit_trace
from core_tools import PROJECT_CACHE_DIR, ToolResult


FAILURE_REPLAY_FILE = os.path.join(PROJECT_CACHE_DIR, "failure_replay_cases.jsonl")


@dataclass
class ReplayCase:
    name: str
    description: str
    runner: Callable[[], bool | str]
    expected_events: list[str] = field(default_factory=list)


@dataclass
class ReplayResult:
    name: str
    status: str
    message: str = ""
    expected_events: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class ReplayHarness:
    def __init__(self):
        self.cases: list[ReplayCase] = []

    def register(self, case: ReplayCase) -> None:
        self.cases.append(case)

    def run(self) -> dict[str, str]:
        return {result.name: result.message if result.status == "ok" else f"{result.status}: {result.message}" for result in self.run_detailed()}

    def run_detailed(self) -> list[ReplayResult]:
        results: list[ReplayResult] = []
        for case in self.cases:
            started = time.time()
            try:
                outcome = case.runner()
                message = "ok" if outcome is True else str(outcome)
                result = ReplayResult(case.name, "ok", message, case.expected_events, int((time.time() - started) * 1000))
            except Exception as exc:
                result = ReplayResult(case.name, "fail", f"{type(exc).__name__}: {exc}", case.expected_events, int((time.time() - started) * 1000))
            emit_trace("replay.case", name=result.name, status=result.status, message=result.message, duration_ms=result.duration_ms, expected_events=result.expected_events)
            results.append(result)
        return results

    def summary(self) -> dict:
        results = self.run_detailed()
        failed = [result.to_dict() for result in results if result.status != "ok"]
        return {
            "total": len(results),
            "passed": len(results) - len(failed),
            "failed": len(failed),
            "results": [result.to_dict() for result in results],
            "failures": failed,
        }


def record_failure_replay(
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
    session_id: str = "",
    turn_id: int = 0,
    count: int = 3,
    path: str = FAILURE_REPLAY_FILE,
) -> dict[str, Any]:
    case = {
        "name": f"failure_{tool_name}_{int(time.time())}",
        "description": f"{tool_name} failed {count} consecutive times",
        "tool_name": tool_name,
        "arguments": _safe_args(arguments),
        "result": result.to_text()[:2000],
        "session_id": session_id,
        "turn_id": turn_id,
        "consecutive_failures": count,
        "trace_file": TRACE_LOG_FILE,
        "created_at": time.time(),
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as file:
            file.write(json.dumps(case, ensure_ascii=False) + "\n")
        emit_trace("FailureReplayCreated", session_id=session_id, turn_id=turn_id, tool=tool_name, count=count, path=path, case_name=case["name"])
    except Exception as exc:
        emit_trace("failure_replay.save_failed", session_id=session_id, turn_id=turn_id, tool=tool_name, error=str(exc), path=path)
    return case


def _safe_args(arguments: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str) and len(value) > 500:
            safe[key] = value[:500] + "...[truncated]"
        else:
            safe[key] = value
    return safe
