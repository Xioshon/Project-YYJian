import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from agent_hooks import emit_trace
from core_tools import PROJECT_CACHE_DIR, ToolResult, resolve_path


TASK_GRAPHS_FILE = os.path.join(PROJECT_CACHE_DIR, "task_graphs.json")
WORKFLOW_REPLAY_FILE = os.path.join(PROJECT_CACHE_DIR, "workflow_replay_cases.jsonl")


@dataclass
class StepVerification:
    status: str = ""
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskStep:
    step_id: str
    name: str
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    result_status: str = ""
    result_message: str = ""
    verification: StepVerification = field(default_factory=StepVerification)
    created_files: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    worker_jobs: list[str] = field(default_factory=list)
    observe_policy: str = ""
    planned: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class TaskGraph:
    task_id: str
    objective: str = ""
    status: str = "active"
    current_step_index: int = 0
    steps: list[TaskStep] = field(default_factory=list)
    created_files: list[str] = field(default_factory=list)
    cleanup: bool = True
    planner_version: str = ""
    planned_at: float = 0.0
    assimilated_worker_jobs: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def current_step(self) -> TaskStep | None:
        if not self.steps:
            return None
        index = max(0, min(self.current_step_index, len(self.steps) - 1))
        return self.steps[index]

    def next_pending_step(self) -> TaskStep | None:
        for step in self.steps:
            if step.status in {"pending", "planned", "observe_needed", "awaiting_verification"}:
                return step
        return None


class TaskGraphManager:
    def __init__(self, path: str = TASK_GRAPHS_FILE):
        self.path = path
        self.graphs: list[TaskGraph] = []
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                raw_items = json.load(file)
            self.graphs = [_graph_from_dict(item) for item in raw_items if isinstance(item, dict)]
        except Exception as exc:
            emit_trace("workflow.load_failed", error=str(exc), path=self.path)
            self.graphs = []

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as file:
                json.dump([asdict(item) for item in self.graphs[-50:]], file, ensure_ascii=False, indent=2)
                file.write("\n")
        except Exception as exc:
            emit_trace("workflow.save_failed", error=str(exc), path=self.path)

    def active(self) -> TaskGraph | None:
        for item in reversed(self.graphs):
            if item.status in {"active", "awaiting_permission", "awaiting_validation", "blocked"}:
                return item
        return None

    def start_or_resume(self, objective: str = "", session_id: str = "", turn_id: int = 0) -> TaskGraph:
        current = self.active()
        if current and current.status != "blocked":
            if objective and not current.objective:
                current.objective = objective[:500]
            current.updated_at = time.time()
            self.save()
            emit_trace("workflow.resumed", session_id=session_id, turn_id=turn_id, task_id=current.task_id, status=current.status)
            return current
        task_id = f"workflow_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        graph = TaskGraph(task_id=task_id, objective=(objective or "")[:500])
        self.graphs.append(graph)
        self.save()
        emit_trace("workflow.started", session_id=session_id, turn_id=turn_id, task_id=task_id, objective=graph.objective[:160])
        return graph

    def record_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        verification: Any = None,
        session_id: str = "",
        turn_id: int = 0,
        objective: str = "",
    ) -> TaskGraph:
        graph = self.start_or_resume(objective, session_id=session_id, turn_id=turn_id)
        verification_status = getattr(verification, "status", "") if verification else ""
        step_status = _step_status(result.status, verification_status)
        step = self._step_for_tool_result(graph, tool_name)
        step.tool_name = tool_name
        step.arguments = _safe_arguments(arguments)
        step.status = step_status
        step.result_status = result.status
        step.result_message = (result.message or "")[:800]
        step.verification = StepVerification(
            status=verification_status,
            message=(getattr(verification, "message", "") or "")[:800],
            details=getattr(verification, "details", {}) if verification else {},
        )
        step.created_files = _created_file_candidates(tool_name, arguments, result)
        step.evidence.extend([item for item in [tool_name, result.status, verification_status] if item])
        step.updated_at = time.time()
        graph.current_step_index = max(0, graph.steps.index(step))
        for path in step.created_files:
            if path not in graph.created_files:
                graph.created_files.append(path)
        graph.status = _graph_status(result.status, verification_status)
        graph.updated_at = time.time()
        self.save()
        emit_trace(
            "workflow.step_recorded",
            session_id=session_id,
            turn_id=turn_id,
            task_id=graph.task_id,
            step_id=step.step_id,
            tool=tool_name,
            status=step.status,
            result_status=result.status,
            verification_status=verification_status,
        )
        return graph

    def plan_steps(self, objective: str, step_names: list[str], session_id: str = "", turn_id: int = 0, planner_version: str = "planner_v1") -> TaskGraph:
        graph = self.start_or_resume(objective, session_id=session_id, turn_id=turn_id)
        if graph.steps:
            emit_trace("planner.plan_reused", session_id=session_id, turn_id=turn_id, task_id=graph.task_id, step_count=len(graph.steps))
            return graph
        for index, name in enumerate(step_names[:12], start=1):
            graph.steps.append(
                TaskStep(
                    step_id=f"step_{index}",
                    name=name[:160],
                    status="planned",
                    planned=True,
                    observe_policy=_observe_policy_for_name(name),
                    evidence=["planned"],
                )
            )
        graph.planner_version = planner_version
        graph.planned_at = time.time()
        graph.updated_at = time.time()
        self.save()
        emit_trace("planner.plan_created", session_id=session_id, turn_id=turn_id, task_id=graph.task_id, objective=graph.objective[:160], step_count=len(graph.steps), planner_version=planner_version)
        return graph

    def append_plan_step(self, name: str, session_id: str = "", turn_id: int = 0) -> TaskGraph:
        graph = self.start_or_resume(name, session_id=session_id, turn_id=turn_id)
        step = TaskStep(
            step_id=f"step_{len(graph.steps) + 1}",
            name=name[:160],
            status="planned",
            planned=True,
            observe_policy=_observe_policy_for_name(name),
            evidence=["planned_append"],
        )
        graph.steps.append(step)
        graph.status = "active"
        graph.updated_at = time.time()
        self.save()
        emit_trace("planner.step_appended", session_id=session_id, turn_id=turn_id, task_id=graph.task_id, step_id=step.step_id, name=step.name)
        return graph

    def cancel_active(self, reason: str = "owner_cancelled", session_id: str = "", turn_id: int = 0) -> bool:
        graph = self.active()
        if not graph:
            return False
        graph.status = "cancelled"
        graph.updated_at = time.time()
        self.save()
        emit_trace("workflow.cancelled", session_id=session_id, turn_id=turn_id, task_id=graph.task_id, reason=reason)
        return True

    def assimilate_worker_results(self, results: list[dict[str, Any]], session_id: str = "", turn_id: int = 0) -> list[dict[str, Any]]:
        graph = self.active()
        if not graph:
            return []
        assimilated: list[dict[str, Any]] = []
        for result in results:
            job_id = str(result.get("job_id") or "")
            if not job_id or job_id in graph.assimilated_worker_jobs:
                continue
            step = self._step_for_worker_result(graph, result)
            status = str(result.get("status") or "")
            step.worker_jobs.append(job_id)
            step.evidence.extend(_worker_evidence(result))
            if status == "done":
                step.status = "verified"
                step.verification = StepVerification("pass", f"worker {result.get('kind', 'verifier')} passed", {"job_id": job_id})
            else:
                step.status = "fail"
                step.verification = StepVerification("fail", f"worker {result.get('kind', 'verifier')} failed", {"job_id": job_id, "error": result.get("error", "")})
                graph.status = "blocked"
            step.updated_at = time.time()
            graph.assimilated_worker_jobs.append(job_id)
            assimilated.append({"job_id": job_id, "step_id": step.step_id, "status": status})
            emit_trace("worker.result_assimilated", session_id=session_id, turn_id=turn_id, task_id=graph.task_id, step_id=step.step_id, job_id=job_id, status=status)
        if assimilated:
            if graph.status != "blocked":
                graph.status = "awaiting_validation"
            graph.updated_at = time.time()
            self.save()
        return assimilated

    def mark_blocked(self, reason: str, session_id: str = "", turn_id: int = 0, tool_name: str = "", arguments: dict[str, Any] | None = None, result: ToolResult | None = None) -> dict[str, Any] | None:
        graph = self.active()
        if not graph:
            return None
        graph.status = "blocked"
        graph.updated_at = time.time()
        self.save()
        emit_trace("workflow.blocked", session_id=session_id, turn_id=turn_id, task_id=graph.task_id, reason=reason, tool=tool_name)
        return record_workflow_replay(graph, reason, tool_name=tool_name, arguments=arguments or {}, result=result, session_id=session_id, turn_id=turn_id)

    def mark_completed(self, session_id: str = "", turn_id: int = 0) -> None:
        graph = self.active()
        if not graph:
            return
        graph.status = "completed"
        graph.updated_at = time.time()
        self.save()
        emit_trace("workflow.completed", session_id=session_id, turn_id=turn_id, task_id=graph.task_id, step_count=len(graph.steps))

    def summary(self) -> str:
        graph = self.active()
        if not graph:
            return "workflow: none"
        current = graph.current_step()
        lines = [
            f"workflow: {graph.status}",
            f"task_id: {graph.task_id}",
            f"objective: {graph.objective or '[none]'}",
            f"steps: {len(graph.steps)}",
        ]
        if current:
            lines.append(f"current_step: {current.step_id} {current.tool_name} {current.status}")
        if graph.created_files:
            lines.append("created_files: " + ", ".join(graph.created_files[-3:]))
        return "\n".join(lines)

    def _step_for_tool_result(self, graph: TaskGraph, tool_name: str) -> TaskStep:
        pending = graph.next_pending_step()
        if pending and (not pending.tool_name or pending.tool_name == tool_name or pending.planned):
            return pending
        step = TaskStep(step_id=f"step_{len(graph.steps) + 1}", name=tool_name, tool_name=tool_name)
        graph.steps.append(step)
        return step

    def _step_for_worker_result(self, graph: TaskGraph, result: dict[str, Any]) -> TaskStep:
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        wanted_step = str(metadata.get("step_id") or "")
        if wanted_step:
            for step in graph.steps:
                if step.step_id == wanted_step:
                    return step
        pending = graph.next_pending_step()
        if pending:
            return pending
        step = TaskStep(step_id=f"step_{len(graph.steps) + 1}", name=f"verify {result.get('kind', 'worker')}", status="awaiting_verification", planned=True)
        graph.steps.append(step)
        return step


def record_workflow_replay(
    graph: TaskGraph,
    reason: str,
    tool_name: str = "",
    arguments: dict[str, Any] | None = None,
    result: ToolResult | None = None,
    session_id: str = "",
    turn_id: int = 0,
) -> dict[str, Any]:
    case = {
        "name": f"workflow_{graph.task_id}_{int(time.time())}",
        "task_id": graph.task_id,
        "objective": graph.objective,
        "status": graph.status,
        "failed_step": asdict(graph.current_step()) if graph.current_step() else None,
        "tool": tool_name,
        "arguments": _safe_arguments(arguments or {}),
        "result": result.to_text() if result else "",
        "reason": reason,
        "session_id": session_id,
        "turn_id": turn_id,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        os.makedirs(os.path.dirname(WORKFLOW_REPLAY_FILE), exist_ok=True)
        with open(WORKFLOW_REPLAY_FILE, "a", encoding="utf-8") as file:
            file.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")
        emit_trace("WorkflowReplayCreated", session_id=session_id, turn_id=turn_id, task_id=graph.task_id, tool=tool_name, reason=reason)
    except Exception as exc:
        emit_trace("workflow_replay.save_failed", session_id=session_id, turn_id=turn_id, error=str(exc))
    return case


def _graph_from_dict(item: dict[str, Any]) -> TaskGraph:
    steps = []
    for raw_step in item.get("steps", []):
        if not isinstance(raw_step, dict):
            continue
        verification = raw_step.get("verification", {})
        if isinstance(verification, dict):
            raw_step["verification"] = StepVerification(**{key: value for key, value in verification.items() if key in StepVerification.__dataclass_fields__})
        steps.append(TaskStep(**{key: value for key, value in raw_step.items() if key in TaskStep.__dataclass_fields__}))
    fields = {key: value for key, value in item.items() if key in TaskGraph.__dataclass_fields__ and key != "steps"}
    return TaskGraph(**fields, steps=steps)


def _observe_policy_for_name(name: str) -> str:
    lowered = (name or "").casefold()
    if any(marker in lowered for marker in ["ui", "screen", "window", "browser", "telegram", "media", "screenshot", "observe"]):
        return "observe_required"
    if any(marker in lowered for marker in ["test", "verify", "compile", "eval"]):
        return "deterministic"
    return "standard"


def _worker_evidence(result: dict[str, Any]) -> list[str]:
    evidence = result.get("evidence")
    if isinstance(evidence, list):
        return [str(item)[:800] for item in evidence[-6:]]
    items = [
        f"worker: {result.get('kind', '')}",
        f"status: {result.get('status', '')}",
    ]
    if result.get("error"):
        items.append(f"error: {result.get('error')}")
    return items


def _step_status(result_status: str, verification_status: str) -> str:
    if result_status == "blocked":
        return "awaiting_permission"
    if result_status != "ok":
        return "fail"
    if verification_status == "pass":
        return "verified"
    if verification_status == "observe_needed":
        return "observe_needed"
    if verification_status == "fail":
        return "fail"
    return "done"


def _graph_status(result_status: str, verification_status: str) -> str:
    if result_status == "blocked":
        return "awaiting_permission"
    if result_status != "ok" or verification_status == "fail":
        return "blocked"
    if verification_status in {"pass", "observe_needed"}:
        return "awaiting_validation"
    return "active"


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str) and len(value) > 500:
            clean[key] = value[:500] + "...[truncated]"
        else:
            clean[key] = value
    return clean


def _created_file_candidates(tool_name: str, arguments: dict[str, Any], result: ToolResult) -> list[str]:
    if tool_name not in {"write_file", "download_file", "analyze_media"}:
        return []
    data = result.data if isinstance(result.data, dict) else {}
    candidates: list[str] = []
    for key in ("path", "file_path", "filename", "saved_to"):
        value = data.get(key) or (arguments or {}).get(key)
        if isinstance(value, str) and value:
            try:
                candidates.append(resolve_path(value))
            except Exception:
                candidates.append(value)
    return sorted(set(candidates))
