import json
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from agent_outcome import _is_safe_retry_step, _latest_retryable_graph_and_step, detect_outcome_action, is_result_followup
from agent_action_verification import verify_action
from agent_hooks import DEFAULT_HOOK_MANAGER, emit_trace
from agent_knowledge import search_knowledge
from agent_latency import InteractionMode, response_policy_for
from agent_permission_replay import PermissionReplayController
from agent_planner import DEFAULT_PLANNER
from agent_presence import PresenceConfig, PresenceEngine
from agent_self_recovery import SelfRecoveryController, diagnose_tool_error, plan_recovery
from agent_url_context import URLContextCache, classify_url_platform, parse_html_metadata
from agent_session import SessionBrain
from agent_task_graph import TaskGraphManager
from agent_tool_runtime import PermissionManager, ToolExecutor, ToolRegistry
from agent_user_voice import friendly_tool_block, permission_request_reply, repeated_tool_stop_reply
from core_tools import ALL_TOOLS, PROJECT_CACHE_DIR, AgentTool, ToolResult


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
                session_id="benchmark",
                turn_id=0,
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
        emit_trace("benchmark.report", session_id="benchmark", turn_id=0, total=total, passed=passed, failed=total - passed, success_rate=report["success_rate"])
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
            name="recovery_command_file_probe_after_cwd_retry",
            description="execute_command path mismatch falls through to a safe file probe when cwd retry fails",
            category="recovery",
            runner=_case_recovery_command_file_probe_after_cwd_retry,
        )
    )
    harness.register(
        BenchmarkCase(
            name="recovery_plan_screenshot_fallback",
            description="screenshot capture failures produce a deterministic screenshot fallback",
            category="recovery",
            runner=_case_recovery_plan_mss_fallback,
        )
    )
    harness.register(
        BenchmarkCase(
            name="recovery_plan_screenshot_runtime_error",
            description="broken screenshot capture code can be recovered without owner follow-up",
            category="recovery",
            runner=_case_recovery_plan_screenshot_runtime_error,
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
            name="planner_code_task_structured_steps",
            description="code tasks produce inspect/change/verify/report structured steps",
            category="planner",
            runner=_case_planner_code_task_structured_steps,
        )
    )
    harness.register(
        BenchmarkCase(
            name="planner_screen_task_short_observe_flow",
            description="screen observation tasks produce a bounded observe/summarize flow",
            category="planner",
            runner=_case_planner_screen_task_short_observe_flow,
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
    harness.register(
        BenchmarkCase(
            name="url_context_metadata_and_douyin_classification",
            description="URL context classifies Douyin separately from TikTok and parses stable metadata",
            category="url_context",
            runner=_case_url_context_metadata_and_douyin_classification,
        )
    )
    harness.register(
        BenchmarkCase(
            name="permission_single_replay_exact_action",
            description="single approval preserves and exposes the original pending action",
            category="permission",
            runner=_case_permission_single_replay_exact_action,
        )
    )
    harness.register(
        BenchmarkCase(
            name="permission_replay_delivers_safe_artifact",
            description="approved replay delivers safe generated media instead of stopping at tool completion",
            category="permission",
            runner=_case_permission_replay_delivers_safe_artifact,
        )
    )
    harness.register(
        BenchmarkCase(
            name="screen_route_allows_safe_verifier",
            description="screen_observe route allows safe verifier commands instead of blocking recovery",
            category="route",
            runner=_case_screen_route_allows_safe_verifier,
        )
    )
    harness.register(
        BenchmarkCase(
            name="transient_error_plans_safe_retry",
            description="transient Telegram/network style errors produce bounded exact retry plans",
            category="recovery",
            runner=_case_transient_error_plans_safe_retry,
        )
    )
    harness.register(
        BenchmarkCase(
            name="outcome_followup_intent_stays_available",
            description="terse result/continue followups remain classified without replanning",
            category="conversation",
            runner=_case_outcome_followup_intent_stays_available,
        )
    )
    harness.register(
        BenchmarkCase(
            name="outcome_retry_finds_recent_failed_step",
            description="outcome retry finds the latest safe failed step even if a new active graph exists",
            category="workflow",
            runner=_case_outcome_retry_finds_recent_failed_step,
        )
    )
    harness.register(
        BenchmarkCase(
            name="user_voice_hides_internal_control_plane",
            description="owner-facing block/retry text stays warm and does not expose route/policy internals",
            category="voice",
            runner=_case_user_voice_hides_internal_control_plane,
        )
    )
    harness.register(
        BenchmarkCase(
            name="presence_shadow_candidate_and_cooldown",
            description="presence v1 records a shadow candidate and suppresses immediate repeats",
            category="presence",
            runner=_case_presence_shadow_candidate_and_cooldown,
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


def _case_recovery_command_file_probe_after_cwd_retry() -> dict[str, Any]:
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeExecutor:
        def execute(self, tool_name: str, arguments: dict[str, Any], tool_callback=None, response_policy=None):
            calls.append((tool_name, dict(arguments or {})))
            if tool_name == "execute_command":
                return ToolResult(
                    "error",
                    "Command failed.",
                    data={"cwd": "project", "retry_hint": "File was still not found from project root."},
                    error="[Errno 2] No such file or directory: 'missing_core_file.py'",
                )
            if tool_name == "execute_python":
                code = str((arguments or {}).get("code") or "")
                ok = "missing_core_file.py" in code and "rglob" in code
                return ToolResult(
                    "ok" if ok else "error",
                    "Python completed." if ok else "Python failed.",
                    data={"stdout": "candidate: C:\\Agent\\core_tools.py", "returncode": 0},
                    error="" if ok else "probe did not include target filename",
                )
            return ToolResult("error", "unexpected tool", error=tool_name)

    original = ToolResult(
        "error",
        "Command failed.",
        data={"cwd": "workspace", "retry_hint": "Command could not find a file. Verify cwd and paths."},
        error="[Errno 2] No such file or directory: 'missing_core_file.py'",
    )
    args = {"command": "python -m py_compile missing_core_file.py", "cwd": "workspace", "timeout": 30}
    controller = SelfRecoveryController(executor=FakeExecutor(), hooks=DEFAULT_HOOK_MANAGER, session_id="benchmark")
    recovered, evidence = controller.recover("execute_command", args, original, None, response_policy_for(InteractionMode.TOOL_TASK), turn_id=3)
    prior = recovered.data.get("prior_recovery_attempts", []) if isinstance(recovered.data, dict) else []
    ok = (
        recovered.status == "ok"
        and bool(evidence)
        and evidence.get("reason") == "command_file_probe"
        and [name for name, _ in calls] == ["execute_command", "execute_python"]
        and bool(prior)
        and prior[0].get("reason") == "cwd_retry"
    )
    return {
        "ok": ok,
        "message": evidence.get("reason") if evidence else recovered.message,
        "recovery_used": True,
        "strategy": evidence.get("details", {}).get("strategy") if evidence else "",
        "candidate_count": len(calls),
        "fallback_tool": calls[-1][0] if calls else "",
    }


def _case_recovery_plan_mss_fallback() -> dict[str, Any]:
    code = "import mss\nwith mss.mss() as sct:\n    sct.shot(output='screenshot.png')"
    result = ToolResult("error", "Python failed.", error="ModuleNotFoundError: No module named 'mss'")
    args = {"code": code, "timeout": 10}
    diagnosis = diagnose_tool_error("execute_python", args, result)
    plan = plan_recovery("execute_python", args, result, diagnosis)
    retry_code = str((plan.retry_args or {}).get("code") if plan else "")
    ok = bool(plan and plan.strategy == "screenshot_capture_fallback" and "System.Windows.Forms" in retry_code and "CopyFromScreen" in retry_code)
    return {"ok": ok, "message": diagnosis.category, "recovery_used": bool(plan), "strategy": getattr(plan, "strategy", "")}


def _case_recovery_plan_screenshot_runtime_error() -> dict[str, Any]:
    code = "import mss\nwith mss.mss() as sct:\n    img = sct.grab(sct.monitors[0])\n    img.save('screen.png')"
    result = ToolResult("error", "Python failed.", error="AttributeError: 'ScreenShot' object has no attribute 'save'")
    args = {"code": code, "timeout": 10}
    diagnosis = diagnose_tool_error("execute_python", args, result)
    plan = plan_recovery("execute_python", args, result, diagnosis)
    retry_code = str((plan.retry_args or {}).get("code") if plan else "")
    ok = bool(plan and plan.strategy == "screenshot_capture_fallback" and "System.Windows.Forms" in retry_code and "CopyFromScreen" in retry_code)
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


def _case_planner_code_task_structured_steps() -> dict[str, Any]:
    path = os.path.join(PROJECT_CACHE_DIR, "benchmark_planner_code_graph.json")
    _remove_file(path)
    plan = DEFAULT_PLANNER.plan("請幫我修 bug，然後跑 self_test", session_id="benchmark", turn_id=11)
    manager = TaskGraphManager(path)
    graph = manager.plan_steps(plan.objective, plan.step_names(), "benchmark", 11, plan.planner_version, step_specs=plan.step_specs())
    selected = manager.select_next_step("benchmark", 12)
    kinds = [step.kind for step in graph.steps]
    verification_steps = [step for step in graph.steps if step.verification_policy == "deterministic"]
    ok = bool(
        selected
        and selected.status == "running"
        and {"plan", "act", "verify", "reply"}.issubset(set(kinds))
        and verification_steps
        and graph.steps[0].allowed_tools
        and graph.steps[-1].done_condition
    )
    return {
        "ok": ok,
        "message": f"{len(graph.steps)} steps",
        "workflow_verified": ok,
        "step_count": len(graph.steps),
        "first_step": selected.name if selected else "",
        "planner_version": graph.planner_version,
    }


def _case_planner_screen_task_short_observe_flow() -> dict[str, Any]:
    path = os.path.join(PROJECT_CACHE_DIR, "benchmark_planner_screen_graph.json")
    _remove_file(path)
    plan = DEFAULT_PLANNER.plan("可以幫我截圖看一下現在畫面嗎", session_id="benchmark", turn_id=13)
    manager = TaskGraphManager(path)
    graph = manager.plan_steps(plan.objective, plan.step_names(), "benchmark", 13, plan.planner_version, step_specs=plan.step_specs())
    selected = manager.select_next_step("benchmark", 14)
    ok = bool(
        len(graph.steps) == 2
        and selected
        and selected.kind == "observe"
        and selected.observe_policy == "observe_required"
        and all(step.kind in {"observe", "reply"} for step in graph.steps)
    )
    return {"ok": ok, "message": selected.name if selected else "missing", "workflow_verified": ok, "step_count": len(graph.steps)}


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


def _case_url_context_metadata_and_douyin_classification() -> dict[str, Any]:
    metadata = parse_html_metadata(
        "<html><head><title>Fallback</title><meta property='og:title' content='URL Benchmark'>"
        "<meta name='description' content='metadata ok'><meta property='og:image' content='https://example.com/a.jpg'></head></html>"
    )
    cache = URLContextCache(os.path.join(PROJECT_CACHE_DIR, "benchmark_url_context_cache.json"))
    classification_ok = classify_url_platform("https://www.douyin.com/video/123") == "douyin" and classify_url_platform("https://www.douyin.com/video/123") != "tiktok"
    ok = classification_ok and metadata.get("title") == "URL Benchmark" and metadata.get("description") == "metadata ok" and cache.reindex().get("status") == "ok"
    return {"ok": ok, "message": "ok" if ok else "url context failed", "platform": classify_url_platform("https://www.douyin.com/video/123"), "title": metadata.get("title", "")}


def _case_permission_single_replay_exact_action() -> dict[str, Any]:
    manager = PermissionManager(session_id="benchmark")
    original_args = {"code": "print('benchmark')", "timeout": 5}
    manager.record_blocked("execute_python", original_args, turn_id=1)
    grant = manager.classify_user_reply("可以", turn_id=2)
    action = manager.pop_approved_action()
    exact = bool(action and action.tool_name == "execute_python" and action.arguments == original_args)
    no_second_action = manager.pop_approved_action() is None
    return {"ok": grant == "single" and exact and no_second_action, "message": grant, "tool": getattr(action, "tool_name", ""), "exact": exact}


def _case_permission_replay_delivers_safe_artifact() -> dict[str, Any]:
    artifact = os.path.join(PROJECT_CACHE_DIR, "benchmark_permission_artifact.png")
    with open(artifact, "wb") as file:
        file.write(b"\x89PNG\r\n\x1a\n")
    sent: list[str] = []

    def fake_execute_python(code: str, timeout: int = 30):
        return ToolResult("ok", "Python completed.", data={"returncode": 0, "stdout": artifact, "stderr": ""})

    def fake_send_telegram_media(file_path: str, caption: str = ""):
        sent.append(os.path.abspath(file_path))
        return ToolResult("ok", "fake media sent", data={"file_path": file_path, "caption": caption})

    registry = ToolRegistry()
    for tool in ALL_TOOLS:
        registry.add(tool)
    registry.add(AgentTool("execute_python", "fake python", fake_execute_python, {"type": "object", "properties": {}}, True))
    registry.add(AgentTool("send_telegram_media", "fake send", fake_send_telegram_media, {"type": "object", "properties": {}}))

    permissions = PermissionManager(session_id="benchmark")
    permissions.record_blocked("execute_python", {"code": "make screenshot"}, turn_id=1)
    grant = permissions.classify_user_reply("可以", turn_id=2)
    executor = ToolExecutor(registry, permissions, interactive_mode=False, session_id="benchmark")
    brain = SessionBrain()
    replies: list[str] = []

    def after_tool_result(tool_name: str, arguments: dict, result: ToolResult):
        return verify_action(tool_name, arguments, result, "benchmark", 2), None

    controller = PermissionReplayController(
        permission_manager=permissions,
        session_brain=brain,
        executor=executor,
        hooks=type("Hooks", (), {"emit": lambda self, *args, **kwargs: type("Decision", (), {"annotate": ""})()})(),
        after_tool_result=after_tool_result,
        append_user_context=lambda text: text,
        append_assistant_reply=replies.append,
        reset_turn_state=lambda: None,
        session_id="benchmark",
        turn_id_getter=lambda: 2,
    )
    handled = controller.maybe_replay(grant, "可以", None)
    content = handled.content if handled else ""
    ok = bool(sent and sent[-1] == os.path.abspath(artifact) and "順手" in content)
    return {"ok": ok, "message": "delivered" if ok else content, "sent": sent[-1] if sent else "", "workflow_verified": ok}


def _case_screen_route_allows_safe_verifier() -> dict[str, Any]:
    registry = ToolRegistry()
    for tool in ALL_TOOLS:
        registry.add(tool)
    permissions = PermissionManager(session_id="benchmark")
    executor = ToolExecutor(registry, permissions, interactive_mode=False, session_id="benchmark")
    policy = response_policy_for(InteractionMode.SCREEN_OBSERVE)
    result = executor.execute("execute_command", {"command": "python -m py_compile core_tools.py", "timeout": 30}, None, policy)
    return {"ok": result.status == "ok", "message": result.message, "returncode": (result.data or {}).get("returncode") if isinstance(result.data, dict) else None}


def _case_transient_error_plans_safe_retry() -> dict[str, Any]:
    result = ToolResult(
        "error",
        "Telegram send failed.",
        error="('Connection aborted.', ConnectionResetError(10054, 'remote host closed an existing connection', None, 10054, None))",
    )
    args = {"file_path": os.path.join(PROJECT_CACHE_DIR, "benchmark.png"), "caption": "benchmark"}
    diagnosis = diagnose_tool_error("send_telegram_media", args, result)
    plan = plan_recovery("send_telegram_media", args, result, diagnosis, max_transient_retries=2)
    ok = bool(plan and plan.strategy == "transient_retry" and plan.max_attempts == 2)
    return {"ok": ok, "message": diagnosis.category, "recovery_used": bool(plan), "strategy": getattr(plan, "strategy", ""), "max_attempts": getattr(plan, "max_attempts", 0)}


def _case_outcome_followup_intent_stays_available() -> dict[str, Any]:
    checks = {
        "continue_traditional": detect_outcome_action("繼續") == "continue_task",
        "continue_simplified": detect_outcome_action("继续") == "continue_task",
        "retry": detect_outcome_action("再試一次") == "continue_task",
        "result_followup": is_result_followup("有結果嗎"),
        "result_followup_short": is_result_followup("結果呢"),
    }
    ok = all(checks.values())
    return {"ok": ok, "message": "ok" if ok else str({key: value for key, value in checks.items() if not value}), **checks}


def _case_outcome_retry_finds_recent_failed_step() -> dict[str, Any]:
    path = os.path.join(PROJECT_CACHE_DIR, "benchmark_outcome_retry_graph.json")
    _remove_file(path)
    manager = TaskGraphManager(path)
    artifact = os.path.join(PROJECT_CACHE_DIR, "benchmark_outcome_retry.png")
    with open(artifact, "wb") as file:
        file.write(b"\x89PNG\r\n\x1a\n")
    failed = ToolResult("error", "Connection aborted.", error="ConnectionResetError(10054)")
    verification = verify_action("send_telegram_media", {"file_path": artifact}, failed, session_id="benchmark", turn_id=1)
    failed_graph = manager.record_tool_result("send_telegram_media", {"file_path": artifact}, failed, verification, session_id="benchmark", turn_id=1, objective="send artifact")
    manager.start_or_resume("new owner follow-up", session_id="benchmark", turn_id=2)
    graph, step = _latest_retryable_graph_and_step(manager)
    ok = bool(graph and step and graph.task_id == failed_graph.task_id and step.tool_name == "send_telegram_media" and _is_safe_retry_step(step.tool_name, step.arguments))
    return {
        "ok": ok,
        "message": step.tool_name if step else "no retryable step",
        "workflow_verified": ok,
        "failed_graph": failed_graph.task_id,
        "selected_graph": getattr(graph, "task_id", ""),
    }


def _case_user_voice_hides_internal_control_plane() -> dict[str, Any]:
    samples = [
        friendly_tool_block("execute_python"),
        friendly_tool_block("analyze_media"),
        friendly_tool_block("execute_command", ToolResult("blocked", "execute_command skipped by chat route policy.", data={"route": "chat", "retry_hint": "你可以說「繼續」接回原任務。"})),
        repeated_tool_stop_reply("execute_python", "case_1"),
        permission_request_reply("execute_command", {"command": "python self_test.py"}),
    ]
    combined = "\n".join(samples)
    leaks = [
        "route policy",
        "chat route",
        "screen_observe",
        "tool_task",
        "social_sticker",
        "skipped by",
        "response policy",
        "loop controller",
        "tool_not_allowed_for_route",
    ]
    bad_markers = [chr(code) for code in [0x9345, 0x9435, 0x7EF2, 0x93B4, 0x952B, 0x707A, 0x95C6, 0x94FB, 0x59AF, 0x6979, 0xFFFD]]
    leaked = [item for item in leaks if item in combined.casefold()]
    mojibake = [hex(ord(item)) for item in bad_markers if item in combined]
    expected = {"主人", "可以", "繼續"}
    missing = sorted(item for item in expected if item not in combined)
    ok = not leaked and not mojibake and not missing
    return {"ok": ok, "message": "ok" if ok else "voice leak", "leaked": leaked, "mojibake": mojibake, "missing": missing}


def _case_presence_shadow_candidate_and_cooldown() -> dict[str, Any]:
    base = os.path.join(PROJECT_CACHE_DIR, "benchmark_presence")
    os.makedirs(base, exist_ok=True)
    state = os.path.join(base, "state.json")
    candidates = os.path.join(base, "candidates.jsonl")
    health = os.path.join(base, "health.json")
    for path in (state, candidates, health):
        _remove_file(path)
    engine = PresenceEngine(
        state_file=state,
        candidates_file=candidates,
        health_file=health,
        config=PresenceConfig(mode="shadow", min_interval_minutes=120, quiet_hours="00:00-00:00"),
    )
    now = 12 * 60 * 60
    first = engine.evaluate(
        "benchmark-chat",
        short_context={"primary_text": "剛剛分享了一個抖音影片，想知道你覺得呢", "topic": "douyin video"},
        session_summary="idle",
        now=now,
    )
    second = engine.evaluate(
        "benchmark-chat",
        short_context={"primary_text": "再聊一下"},
        session_summary="idle",
        now=now + 60,
    )
    recent = engine.recent_candidates()
    health_payload = engine.write_health()
    ok = bool(
        first.status == "shadow"
        and first.candidate
        and first.candidate.kind in {"followup", "soft_ping", "social", "care"}
        and second.status == "suppressed"
        and second.reason == "cooldown"
        and len(recent) == 1
        and health_payload.get("shadow_count") == 1
    )
    return {
        "ok": ok,
        "message": f"{first.status}/{second.reason}",
        "workflow_verified": ok,
        "candidate_kind": first.candidate.kind if first.candidate else "",
        "suppressed_reason": second.reason,
    }


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
