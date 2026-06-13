import json
import os
import subprocess
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from agent_hooks import TRACE_LOG_FILE
from agent_observability import load_trace_events
from core_tools import PROJECT_CACHE_DIR, ROOT_DIR


EVAL_REPORT_FILE = os.path.join(PROJECT_CACHE_DIR, "eval_report.json")
PERMISSION_HEALTH_FILE = os.path.join(PROJECT_CACHE_DIR, "permission_health.json")
PRIVATE_GIT_PREFIXES = (
    "workspace/chat_history/",
    "workspace/logs/",
    "workspace/project_cache/",
    "workspace/tg_chat_id.txt",
)
PRIVATE_GIT_EXACT = {".env"}


@dataclass
class LiveEvalReport:
    generated_at: str
    trace_path: str
    total_events: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    tool_success_rate: float = 1.0
    most_failed_tools: list[dict[str, Any]] = field(default_factory=list)
    permission_replay: dict[str, Any] = field(default_factory=dict)
    repeated_failure_count: int = 0
    latency_buckets: dict[str, dict[str, int]] = field(default_factory=dict)
    knowledge: dict[str, Any] = field(default_factory=dict)
    workflow: dict[str, Any] = field(default_factory=dict)
    self_repair: dict[str, Any] = field(default_factory=dict)
    worker: dict[str, Any] = field(default_factory=dict)
    planner: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    subagents: dict[str, Any] = field(default_factory=dict)
    persona: dict[str, Any] = field(default_factory=dict)
    source_health: dict[str, Any] = field(default_factory=dict)
    permission_policy: dict[str, Any] = field(default_factory=dict)
    render: dict[str, Any] = field(default_factory=dict)
    telegram: dict[str, Any] = field(default_factory=dict)
    recent_errors: list[dict[str, Any]] = field(default_factory=list)
    repo_hygiene: dict[str, Any] = field(default_factory=dict)
    next_stage_gate: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_text(self) -> str:
        lines = [
            "YueYue Live Evaluation",
            f"Trace events: {self.total_events}",
            f"Tool success rate: {self.tool_success_rate:.1%} ({self.tool_calls - self.tool_errors}/{self.tool_calls})",
            f"Permission replay: {self.permission_replay.get('success_rate', 1.0):.1%} success",
            f"Knowledge hit rate: {self.knowledge.get('hit_rate', 1.0):.1%} ({self.knowledge.get('hit_count', 0)}/{self.knowledge.get('search_count', 0)})",
            f"Workflow success rate: {self.workflow.get('success_rate', 1.0):.1%} ({self.workflow.get('completed_count', 0)}/{self.workflow.get('started_count', 0)})",
            f"Self repair: {self.self_repair.get('trigger_count', 0)} triggers, deterministic {self.self_repair.get('deterministic_success_rate', 1.0):.1%} success",
            f"Worker success rate: {self.worker.get('success_rate', 1.0):.1%} ({self.worker.get('done_count', 0)}/{self.worker.get('total_results', 0)})",
            f"Planner coverage: {self.planner.get('plan_count', 0)} plans, {self.planner.get('planned_step_count', 0)} planned steps",
            f"Worker assimilation: {self.worker.get('assimilated_count', 0)} results assimilated",
            f"Subagent health: {self.subagents.get('ok_count', 0)}/{self.subagents.get('run_count', 0)} ok",
            f"Context budget: {self.context.get('last_total_after', 0)}/{self.context.get('last_max_chars', 0)} chars",
            f"Persona health: {self.persona.get('status', 'unknown')}",
            f"Source health: {self.source_health.get('status', 'unknown')}",
            f"Permission policy: {self.permission_policy.get('status', 'unknown')}",
            f"Render dedupe: {self.render.get('deduped_count', 0)} artifacts deduped",
            f"Repeated failure cases: {self.repeated_failure_count}",
            f"Repo hygiene: {self.repo_hygiene.get('status', 'unknown')}",
            f"Next-stage gate: {self.next_stage_gate.get('status', 'unknown')}",
        ]
        if self.most_failed_tools:
            lines.append("Most failed tools: " + ", ".join(f"{item['tool']}={item['count']}" for item in self.most_failed_tools))
        if self.latency_buckets:
            parts = []
            for group, buckets in sorted(self.latency_buckets.items()):
                parts.append(group + "[" + ", ".join(f"{name}={count}" for name, count in sorted(buckets.items())) + "]")
            lines.append("Latency: " + "; ".join(parts))
        blockers = self.next_stage_gate.get("blockers") or []
        if blockers:
            lines.append("Blockers:")
            lines.extend(f"- {item}" for item in blockers)
        if self.recent_errors:
            lines.append("Recent errors:")
            for item in self.recent_errors[-5:]:
                lines.append(f"- {item.get('event', 'error')}: {item.get('tool', '')} {item.get('error') or item.get('result') or ''}".strip())
        return "\n".join(lines)


def build_live_eval_report(trace_path: str = TRACE_LOG_FILE, limit: int | None = 2000, include_repo: bool = True) -> LiveEvalReport:
    events = _current_session_events(load_trace_events(trace_path, limit=limit))
    tool_calls: Counter[str] = Counter()
    tool_errors: Counter[str] = Counter()
    permission_replay: Counter[str] = Counter()
    latency: dict[str, Counter[str]] = {}
    knowledge_searches = 0
    knowledge_hits = 0
    knowledge_empty = 0
    repeated_failure_count = 0
    telegram_events: Counter[str] = Counter()
    workflow_started: set[str] = set()
    workflow_completed: set[str] = set()
    workflow_blocked: set[str] = set()
    workflow_steps: Counter[str] = Counter()
    workflow_failures: Counter[str] = Counter()
    recovery_count = 0
    self_repair_triggers = 0
    self_repair_results = 0
    self_repair_success = 0
    self_repair_failures = 0
    self_repair_tools: Counter[str] = Counter()
    self_repair_reasons: Counter[str] = Counter()
    worker_results: Counter[str] = Counter()
    worker_timeouts = 0
    worker_durations: list[int] = []
    worker_assimilated = 0
    planner_plans = 0
    planner_steps = 0
    observe_needed = 0
    context_events = 0
    last_context_after = 0
    last_context_max = 0
    subagent_runs: Counter[str] = Counter()
    recent_errors: list[dict[str, Any]] = []

    for event in events:
        name = str(event.get("event") or "unknown")
        tool = str(event.get("tool") or "")
        if name == "PostToolUse":
            tool_name = tool or "unknown"
            tool_calls[tool_name] += 1
            if str(event.get("status") or "").casefold() == "error":
                tool_errors[tool_name] += 1
                recent_errors.append(_error_snapshot(event))
            if tool_name == "search_knowledge":
                hit_count = _knowledge_hit_count_from_result(event.get("result"))
                knowledge_searches += 1
                if hit_count > 0:
                    knowledge_hits += 1
                else:
                    knowledge_empty += 1
        elif name == "ToolError":
            tool_name = tool or "unknown"
            tool_errors[tool_name] += 1
            recent_errors.append(_error_snapshot(event))
        elif name == "PermissionReplayResult":
            permission_replay[str(event.get("status") or "unknown")] += 1
        elif name == "FailureReplayCreated":
            repeated_failure_count += 1
        elif name == "WorkflowReplayCreated":
            repeated_failure_count += 1
            task_id = str(event.get("task_id") or "")
            if task_id:
                workflow_blocked.add(task_id)
            tool_name = tool or str(event.get("tool") or "unknown")
            workflow_failures[tool_name] += 1
        elif name == "workflow.started":
            task_id = str(event.get("task_id") or "")
            if task_id:
                workflow_started.add(task_id)
        elif name == "workflow.resumed":
            task_id = str(event.get("task_id") or "")
            if task_id:
                workflow_started.add(task_id)
        elif name == "workflow.completed":
            task_id = str(event.get("task_id") or "")
            if task_id:
                workflow_completed.add(task_id)
                workflow_started.add(task_id)
        elif name == "workflow.blocked":
            task_id = str(event.get("task_id") or "")
            if task_id:
                workflow_blocked.add(task_id)
                workflow_started.add(task_id)
            workflow_failures[tool or str(event.get("tool") or "unknown")] += 1
        elif name == "workflow.step_recorded":
            task_id = str(event.get("task_id") or "")
            if task_id:
                workflow_started.add(task_id)
                workflow_steps[task_id] += 1
            if str(event.get("status") or "") in {"fail", "blocked"}:
                workflow_failures[tool or str(event.get("tool") or "unknown")] += 1
        elif name in {"SelfRecoveryAttempt", "SelfRepairPrompt", "PermissionReplaySelfRepair"}:
            self_repair_triggers += 1
            if tool:
                self_repair_tools[tool] += 1
            reason = str(event.get("reason") or name)
            self_repair_reasons[reason] += 1
        elif name in {"SelfRecoveryResult", "ToolRecoveryResult"}:
            recovery_count += 1
            self_repair_results += 1
            if tool:
                self_repair_tools[tool] += 1
            status = str(event.get("retry_status") or "").casefold()
            if status == "ok":
                self_repair_success += 1
            else:
                self_repair_failures += 1
            reason = str(event.get("reason") or name)
            self_repair_reasons[reason] += 1
        elif name == "worker.result":
            status = str(event.get("status") or "unknown")
            worker_results[status] += 1
            if str(event.get("error") or "") == "timeout":
                worker_timeouts += 1
            try:
                worker_durations.append(int(event.get("duration_ms") or 0))
            except Exception:
                pass
        elif name == "worker.result_assimilated":
            worker_assimilated += 1
        elif name == "planner.plan_created":
            planner_plans += 1
            try:
                planner_steps += int(event.get("step_count") or 0)
            except Exception:
                pass
        elif name == "ActionVerification" and str(event.get("status") or "") == "observe_needed":
            observe_needed += 1
        elif name == "workflow.step_recorded" and str(event.get("status") or "") == "observe_needed":
            observe_needed += 1
        elif name == "context.budget":
            context_events += 1
            try:
                last_context_after = int(event.get("total_after") or 0)
                last_context_max = int(event.get("max_chars") or 0)
            except Exception:
                pass
        elif name == "subagent.run":
            subagent_runs[str(event.get("status") or "unknown")] += 1
        elif name == "KnowledgeSearch":
            knowledge_searches += 1
            try:
                hit_count = int(event.get("hit_count") or 0)
            except Exception:
                hit_count = 0
            if hit_count > 0:
                knowledge_hits += 1
            else:
                knowledge_empty += 1
        elif name.startswith("telegram.") or tool in {"send_telegram_media", "react_to_message"}:
            telegram_events[name] += 1

        bucket = _latency_bucket(event.get("duration_ms") or event.get("latency_ms"))
        if bucket:
            group = _latency_group(event)
            latency.setdefault(group, Counter())[bucket] += 1
        if name.endswith("failed") or name == "trace.decode_error" or name == "HookError":
            recent_errors.append(_error_snapshot(event))

    total_tool_calls = sum(tool_calls.values())
    total_tool_errors = sum(tool_errors.values())
    tool_success_rate = 1.0 if total_tool_calls == 0 else max(0.0, min(1.0, (total_tool_calls - total_tool_errors) / total_tool_calls))
    replay_total = sum(permission_replay.values())
    replay_ok = permission_replay.get("ok", 0)
    replay_success_rate = 1.0 if replay_total == 0 else replay_ok / replay_total
    knowledge_hit_rate = 1.0 if knowledge_searches == 0 else knowledge_hits / knowledge_searches
    repo_hygiene = check_repo_hygiene() if include_repo else {"status": "skipped", "tracked_private_files": []}
    persona = _load_persona_health()
    source_health = check_user_facing_source_health()
    permission_policy = _permission_policy_health()
    render = _load_render_dedupe()
    started_count = len(workflow_started)
    completed_count = len(workflow_completed)
    workflow_success_rate = 1.0 if started_count == 0 else completed_count / started_count
    average_steps = 0.0 if not workflow_steps else sum(workflow_steps.values()) / len(workflow_steps)
    worker_total = sum(worker_results.values())
    worker_done = worker_results.get("done", 0)
    worker_success_rate = 1.0 if worker_total == 0 else worker_done / worker_total
    worker_average_ms = 0 if not worker_durations else int(sum(worker_durations) / len(worker_durations))
    deterministic_repair_success_rate = 1.0 if self_repair_results == 0 else self_repair_success / self_repair_results

    gate = _gate_status(
        tool_success_rate=tool_success_rate,
        replay_success_rate=replay_success_rate,
        repo_hygiene=repo_hygiene,
        repeated_failure_count=repeated_failure_count,
        workflow_success_rate=workflow_success_rate,
        worker_success_rate=worker_success_rate,
        source_health=source_health,
    )

    return LiveEvalReport(
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        trace_path=trace_path,
        total_events=len(events),
        tool_calls=total_tool_calls,
        tool_errors=total_tool_errors,
        tool_success_rate=round(tool_success_rate, 4),
        most_failed_tools=[{"tool": name, "count": count} for name, count in tool_errors.most_common(5)],
        permission_replay={"total": replay_total, "ok": replay_ok, "by_status": dict(sorted(permission_replay.items())), "success_rate": round(replay_success_rate, 4)},
        repeated_failure_count=repeated_failure_count,
        latency_buckets={group: dict(sorted(counter.items())) for group, counter in sorted(latency.items())},
        knowledge={"search_count": knowledge_searches, "hit_count": knowledge_hits, "empty_count": knowledge_empty, "hit_rate": round(knowledge_hit_rate, 4)},
        workflow={
            "started_count": started_count,
            "completed_count": completed_count,
            "blocked_count": len(workflow_blocked),
            "success_rate": round(workflow_success_rate, 4),
            "recovery_count": recovery_count,
            "average_steps_per_task": round(average_steps, 2),
            "top_failure_steps": [{"tool": name, "count": count} for name, count in workflow_failures.most_common(5)],
        },
        self_repair={
            "trigger_count": self_repair_triggers,
            "result_count": self_repair_results,
            "success_count": self_repair_success,
            "failure_count": self_repair_failures,
            "deterministic_success_rate": round(deterministic_repair_success_rate, 4),
            "top_tools": [{"tool": name, "count": count} for name, count in self_repair_tools.most_common(5)],
            "by_reason": dict(sorted(self_repair_reasons.items())),
        },
        worker={
            "total_results": worker_total,
            "done_count": worker_done,
            "failed_count": worker_results.get("failed", 0),
            "timeout_count": worker_timeouts,
            "assimilated_count": worker_assimilated,
            "success_rate": round(worker_success_rate, 4),
            "average_duration_ms": worker_average_ms,
            "by_status": dict(sorted(worker_results.items())),
        },
        planner={"plan_count": planner_plans, "planned_step_count": planner_steps, "observe_needed_count": observe_needed},
        context={"budget_events": context_events, "last_total_after": last_context_after, "last_max_chars": last_context_max},
        subagents={"run_count": sum(subagent_runs.values()), "ok_count": subagent_runs.get("ok", 0), "by_status": dict(sorted(subagent_runs.items()))},
        persona=persona,
        source_health=source_health,
        permission_policy=permission_policy,
        render=render,
        telegram={"events": dict(sorted(telegram_events.items()))},
        recent_errors=recent_errors[-10:],
        repo_hygiene=repo_hygiene,
        next_stage_gate=gate,
    )


def write_eval_report(report: LiveEvalReport, path: str = EVAL_REPORT_FILE) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(report.to_dict(), file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")
    return path


def _current_session_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for index in range(len(events) - 1, -1, -1):
        if str(events[index].get("event") or "") == "SessionStart":
            return events[index:]
    return events


def check_repo_hygiene(root_dir: str = ROOT_DIR) -> dict[str, Any]:
    if not os.path.isdir(os.path.join(root_dir, ".git")):
        return {"status": "skipped", "tracked_private_files": []}
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root_dir, capture_output=True, timeout=30)
    if result.returncode != 0:
        return {"status": "error", "error": result.stderr.decode("utf-8", errors="replace"), "tracked_private_files": []}
    files = [item.decode("utf-8", errors="replace") for item in result.stdout.split(b"\0") if item]
    leaked = [
        path
        for path in files
        if path in PRIVATE_GIT_EXACT or path.endswith(".pyc") or any(path == prefix or path.startswith(prefix) for prefix in PRIVATE_GIT_PREFIXES)
    ]
    return {"status": "pass" if not leaked else "fail", "tracked_private_files": leaked[:50]}


USER_FACING_SOURCE_FILES = (
    "agent_user_voice.py",
    "agent_outcome.py",
    "agent_latency.py",
    "agent_tool_runtime.py",
    "agent_tool_loop.py",
    "main.py",
)

SOURCE_MOJIBAKE_MARKERS = ("鍓", "鐪", "绲", "鎴", "锛", "灞", "闆", "铻", "妯", "楹", "�")

SOURCE_REQUIRED_PHRASES = {
    "agent_user_voice.py": ("我先不直接跑", "可以", "繼續"),
    "agent_outcome.py": ("有結果", "發給我", "分析一下", "繼續"),
    "agent_latency.py": ("我先看一下", "我先處理一下", "收到"),
    "agent_tool_runtime.py": ("你可以說「繼續」接回原任務"),
    "agent_tool_loop.py": ("系統截圖",),
}


def check_user_facing_source_health(root_dir: str = ROOT_DIR) -> dict[str, Any]:
    checked: list[str] = []
    issues: list[dict[str, Any]] = []
    for filename in USER_FACING_SOURCE_FILES:
        path = os.path.join(root_dir, filename)
        checked.append(filename)
        if not os.path.exists(path):
            issues.append({"file": filename, "kind": "missing"})
            continue
        try:
            with open(path, "rb") as file:
                raw = file.read()
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            issues.append({"file": filename, "kind": "invalid_utf8", "message": str(exc)})
            continue
        if "????" in text:
            issues.append({"file": filename, "kind": "question_mark_mojibake"})
        markers = [marker for marker in SOURCE_MOJIBAKE_MARKERS if marker in text]
        if markers:
            issues.append({"file": filename, "kind": "mojibake_markers", "markers": markers[:8]})
        for phrase in SOURCE_REQUIRED_PHRASES.get(filename, ()):
            if phrase not in text:
                issues.append({"file": filename, "kind": "missing_phrase", "phrase": phrase})
    return {"status": "pass" if not issues else "fail", "checked_files": checked, "issues": issues[:20]}


def _load_persona_health() -> dict[str, Any]:
    path = os.path.join(PROJECT_CACHE_DIR, "persona_health.json")
    if not os.path.exists(path):
        return {"status": "missing", "warnings": []}
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
        return {"status": data.get("status", "unknown"), "warning_count": len(data.get("warnings", [])), "warnings": data.get("warnings", [])[:5]}
    except Exception as exc:
        return {"status": "error", "error": str(exc), "warnings": []}


def _permission_policy_health() -> dict[str, Any]:
    try:
        from agent_tool_runtime import LOW_RISK_TOOLS, PERMISSION_BUNDLES, SAFE_VERIFIER_COMMAND_PATTERNS

        guarded = sorted({tool for tools in PERMISSION_BUNDLES.values() for tool in tools} - set(LOW_RISK_TOOLS))
        report = {
            "status": "pass",
            "free_tools": sorted(LOW_RISK_TOOLS),
            "guarded_tools": guarded,
            "bundles": {name: sorted(tools) for name, tools in sorted(PERMISSION_BUNDLES.items())},
            "safe_verifier_patterns": list(SAFE_VERIFIER_COMMAND_PATTERNS),
            "principle": "guard real destructive/exfiltration/system-control risk; keep low-risk local tools convenient",
        }
        os.makedirs(PROJECT_CACHE_DIR, exist_ok=True)
        with open(PERMISSION_HEALTH_FILE, "w", encoding="utf-8") as file:
            json.dump(report, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
        return report
    except Exception as exc:
        return {"status": "error", "warnings": [str(exc)]}


def _load_render_dedupe() -> dict[str, Any]:
    path = os.path.join(PROJECT_CACHE_DIR, "render_dedupe.jsonl")
    counts: Counter[str] = Counter()
    deduped = 0
    if not os.path.exists(path):
        return {"deduped_count": 0, "by_kind": {}}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as file:
            for line in file:
                if not line.strip():
                    continue
                row = json.loads(line)
                kind = str(row.get("kind") or "unknown")
                original = int(row.get("original_count") or 0)
                rendered = int(row.get("rendered_count") or 0)
                saved = max(0, original - rendered)
                counts[kind] += saved
                deduped += saved
    except Exception:
        pass
    return {"deduped_count": deduped, "by_kind": dict(sorted(counts.items()))}


def _gate_status(
    tool_success_rate: float,
    replay_success_rate: float,
    repo_hygiene: dict[str, Any],
    repeated_failure_count: int,
    workflow_success_rate: float = 1.0,
    worker_success_rate: float = 1.0,
    source_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    if repo_hygiene.get("status") == "fail":
        blockers.append("private runtime files are tracked by Git")
    if source_health and source_health.get("status") == "fail":
        blockers.append("user-facing source text health failed")
    if tool_success_rate < 0.8:
        blockers.append("tool success rate is below 80%")
    elif tool_success_rate < 0.95:
        warnings.append("tool success rate is below 95%; inspect recent errors before large changes")
    if replay_success_rate < 1.0:
        warnings.append("some permission replay attempts failed")
    if workflow_success_rate < 0.8:
        warnings.append("workflow success rate is below 80%; inspect blocked workflows before large changes")
    elif workflow_success_rate < 1.0:
        warnings.append("some workflows did not complete")
    if repeated_failure_count > 0:
        warnings.append("failure replay cases exist; review them before major workflow expansion")
    if worker_success_rate < 0.8:
        warnings.append("background verifier worker success rate is below 80%")
    return {"status": "pass" if not blockers else "block", "blockers": blockers, "warnings": warnings}


def _knowledge_hit_count_from_result(result: Any) -> int:
    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except Exception:
            return 0
    elif isinstance(result, dict):
        payload = result
    else:
        return 0
    data = payload.get("data") if isinstance(payload, dict) else {}
    hits = data.get("hits") if isinstance(data, dict) else []
    return len(hits or [])


def _latency_group(event: dict[str, Any]) -> str:
    tool = str(event.get("tool") or "")
    mode = str(event.get("mode") or "")
    if tool == "analyze_media" or mode == "vision_task":
        return "vision"
    if tool in {"send_telegram_media", "react_to_message"}:
        return "telegram_media"
    if tool:
        return "tool"
    if mode:
        return mode
    return "other"


def _latency_bucket(value: Any) -> str:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return ""
    if duration < 1000:
        return "<1s"
    if duration < 3000:
        return "1-3s"
    if duration < 6000:
        return "3-6s"
    return ">=6s"


def _error_snapshot(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": event.get("ts", ""),
        "event": event.get("event", ""),
        "tool": event.get("tool", ""),
        "error": event.get("error", ""),
        "result": str(event.get("result", ""))[:300],
    }


def main() -> None:
    report = build_live_eval_report()
    path = write_eval_report(report)
    print(report.to_text())
    print(f"\nReport written: {path}")


if __name__ == "__main__":
    main()
