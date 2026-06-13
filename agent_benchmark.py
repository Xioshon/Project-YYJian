import json
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from agent_action_verification import verify_action
from agent_hooks import emit_trace
from agent_knowledge import search_knowledge
from agent_self_recovery import diagnose_tool_error, plan_recovery
from agent_task_graph import TaskGraphManager
from core_tools import PROJECT_CACHE_DIR, ToolResult


TASK_BENCHMARK_FILE = os.path.join(PROJECT_CACHE_DIR, "task_benchmark_report.json")


@dataclass
class BenchmarkCase:
    name: str
    description: str
    category: str
    runner: Callable[[], bool | str | dict[str, Any]]
    expected: list[str] = field(default_factory=list)


@dataclass
class BenchmarkResult:
    name: str
    category: str
    status: str
    message: str = ""
    duration_ms: int = 0
    recovery_used: bool = False
    workflow_verified: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskBenchmarkHarness:
    """Small deterministic task benchmark for runtime control-plane behavior."""

    def __init__(self):
        self.cases: list[BenchmarkCase] = []

    def register(self, case: BenchmarkCase) -> None:
        self.cases.append(case)

    def run_detailed(self) -> list[BenchmarkResult]:
        results: list[BenchmarkResult] = []
        for case in self.cases:
            started = time.time()
            status = "pass"
            message = ""
            details: dict[str, Any] = {}
            try:
                outcome = case.runner()
                if isinstance(outcome, dict):
                    ok = bool(outcome.get("ok", True))
                    status = "pass" if ok else "fail"
                    message = str(outcome.get("message") or "")
                    details = {key: value for key, value in outcome.items() if key not in {"ok", "message"}}
                elif isinstance(outcome, str):
                    status = "pass" if outcome else "fail"
                    message = outcome
                else:
                    status = "pass" if outcome else "fail"
                    message = "ok" if outcome else "runner returned false"
            except Exception as exc:
                status = "fail"
                message = str(exc)
                details = {"exception": exc.__class__.__name__}
            duration_ms = int((time.time() - started) * 1000)
            result = BenchmarkResult(
                name=case.name,
                category=case.category,
                status=status,
                message=message,
                duration_ms=duration_ms,
                recovery_used=bool(details.get("recovery_used")),
                workflow_verified=bool(details.get("workflow_verified")),
                details=details,
            )
            emit_trace(
                "benchmark.case",
                name=result.name,
                category=result.category,
                status=result.status,
                duration_ms=result.duration_ms,
                recovery_used=result.recovery_used,
                workflow_verified=result.workflow_verified,
            )
            results.append(result)
        return results

    def report(self, results: list[BenchmarkResult] | None = None) -> dict[str, Any]:
        results = results if results is not None else self.run_detailed()
        total = len(results)
        passed = sum(1 for item in results if item.status == "pass")
        by_category: dict[str, dict[str, int]] = {}
        for category, rows in _group_by_category(results).items():
            by_category[category] = {
                "total": len(rows),
                "passed": sum(1 for item in rows if item.status == "pass"),
                "failed": sum(1 for item in rows if item.status != "pass"),
            }
        report = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "success_rate": round(1.0 if total == 0 else passed / total, 4),
            "recovery_count": sum(1 for item in results if item.recovery_used),
            "workflow_verified_count": sum(1 for item in results if item.workflow_verified),
            "by_category": by_category,
            "results": [item.to_dict() for item in results],
        }
        emit_trace("benchmark.report", total=total, passed=passed, failed=total - passed, success_rate=report["success_rate"])
        return report

    def write_report(self, path: str = TASK_BENCHMARK_FILE) -> dict[str, Any]:
        report = self.report()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(report, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
        return report


def build_default_benchmark() -> TaskBenchmarkHarness:
    harness = TaskBenchmarkHarness()
    harness.register(
        BenchmarkCase(
            name="recovery_plan_cwd_retry",
            description="execute_command path mismatch produces a safe project cwd retry plan",
            category="recovery",
            runner=_case_recovery_plan_cwd_retry,
        )
    )
    harness.register(
        BenchmarkCase(
            name="recovery_plan_mss_fallback",
            description="missing mss during screenshot code produces a deterministic screenshot fallback",
            category="recovery",
            runner=_case_recovery_plan_mss_fallback,
        )
    )
    harness.register(
        BenchmarkCase(
            name="task_graph_records_recovery_evidence",
            description="recovered tool results are attached to verification and task graph evidence",
            category="workflow",
            runner=_case_task_graph_records_recovery_evidence,
        )
    )
    harness.register(
        BenchmarkCase(
            name="workflow_failure_generates_blocked_state",
            description="failed verification blocks the active workflow instead of pretending success",
            category="workflow",
            runner=_case_workflow_failure_generates_blocked_state,
        )
    )
    harness.register(
        BenchmarkCase(
            name="knowledge_search_permission_replay",
            description="engineering knowledge index can answer permission replay questions",
            category="knowledge",
            runner=_case_knowledge_search_permission_replay,
        )
    )
    return harness


def load_latest_benchmark_report(path: str = TASK_BENCHMARK_FILE) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {
        "status": "missing",
        "total": 0,
        "passed": 0,
        "failed": 0,
        "success_rate": 1.0,
        "by_category": {},
        "results": [],
    }


def _case_recovery_plan_cwd_retry() -> dict[str, Any]:
    result = ToolResult(
        "error",
        "Command failed.",
        data={"cwd": "workspace", "retry_hint": "Try cwd='project' for project root files."},
        error="[Errno 2] No such file or directory: 'core_tools.py'",
    )
    args = {"command": "python -m py_compile core_tools.py", "cwd": "workspace"}
    diagnosis = diagnose_tool_error("execute_command", args, result)
    plan = plan_recovery("execute_command", args, result, diagnosis)
    ok = bool(plan and plan.strategy == "cwd_retry" and plan.retry_args and plan.retry_args.get("cwd") == "project")
    return {"ok": ok, "message": diagnosis.category, "recovery_used": bool(plan), "strategy": getattr(plan, "strategy", "")}


def _case_recovery_plan_mss_fallback() -> dict[str, Any]:
    code = "import mss\nwith mss.mss() as sct:\n    sct.shot(output='screenshot.png')"
    result = ToolResult("error", "Python failed.", error="ModuleNotFoundError: No module named 'mss'")
    args = {"code": code, "timeout": 10}
    diagnosis = diagnose_tool_error("execute_python", args, result)
    plan = plan_recovery("execute_python", args, result, diagnosis)
    retry_code = str((plan.retry_args or {}).get("code") if plan else "")
    ok = bool(plan and plan.strategy == "missing_mss_screenshot_fallback" and "ImageGrab.grab" in retry_code)
    return {"ok": ok, "message": diagnosis.category, "recovery_used": bool(plan), "strategy": getattr(plan, "strategy", "")}


def _case_task_graph_records_recovery_evidence() -> dict[str, Any]:
    path = os.path.join(PROJECT_CACHE_DIR, "benchmark_task_graph.json")
    _remove_file(path)
    manager = TaskGraphManager(path)
    recovery = {
        "reason": "cwd_retry",
        "original_status": "error",
        "original_message": "Command failed.",
        "attempts": 1,
        "retry_status": "ok",
        "retry_message": "Command completed.",
        "details": {"diagnosis": "cwd_or_path_mismatch"},
    }
    result = ToolResult("ok", "Command completed.", data={"returncode": 0, "recovered_from": recovery})
    verification = verify_action("execute_command", {"command": "python -m py_compile core_tools.py"}, result, session_id="benchmark", turn_id=1)
    graph = manager.record_tool_result("execute_command", {"command": "python -m py_compile core_tools.py"}, result, verification, session_id="benchmark", turn_id=1, objective="benchmark")
    step = graph.current_step()
    evidence = step.evidence if step else []
    ok = bool(step and step.status == "verified" and any("recovery:cwd_retry" == item for item in evidence) and verification.details.get("recovered"))
    return {"ok": ok, "message": step.status if step else "missing step", "workflow_verified": ok, "recovery_used": True, "evidence": evidence}


def _case_workflow_failure_generates_blocked_state() -> dict[str, Any]:
    path = os.path.join(PROJECT_CACHE_DIR, "benchmark_blocked_graph.json")
    _remove_file(path)
    manager = TaskGraphManager(path)
    result = ToolResult("error", "Command failed.", data={"returncode": 1}, error="boom")
    verification = verify_action("execute_command", {"command": "bad"}, result, session_id="benchmark", turn_id=2)
    graph = manager.record_tool_result("execute_command", {"command": "bad"}, result, verification, session_id="benchmark", turn_id=2, objective="blocked benchmark")
    ok = graph.status == "blocked" and bool(graph.current_step() and graph.current_step().status == "fail")
    return {"ok": ok, "message": graph.status, "workflow_verified": False, "step_status": graph.current_step().status if graph.current_step() else ""}


def _case_knowledge_search_permission_replay() -> dict[str, Any]:
    hits = search_knowledge("permission replay execute_command cwd recovery", limit=3)
    ok = bool(hits)
    return {"ok": ok, "message": f"{len(hits)} hits", "hit_count": len(hits), "top_hit": hits[0]["title"] if hits else ""}


def _group_by_category(results: list[BenchmarkResult]) -> dict[str, list[BenchmarkResult]]:
    grouped: dict[str, list[BenchmarkResult]] = {}
    for item in results:
        grouped.setdefault(item.category, []).append(item)
    return grouped


def _remove_file(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def main() -> int:
    report = build_default_benchmark().write_report()
    print("YueYue Task Benchmark")
    print(f"Passed: {report['passed']}/{report['total']} ({report['success_rate']:.1%})")
    for category, counts in sorted(report["by_category"].items()):
        print(f"- {category}: {counts['passed']}/{counts['total']}")
    if report["failed"]:
        print("Failures:")
        for result in report["results"]:
            if result.get("status") != "pass":
                print(f"- {result.get('name')}: {result.get('message')}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
