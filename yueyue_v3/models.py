from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum, StrEnum
from typing import Any

SCHEMA_VERSION = 4


class TurnMode(StrEnum):
    CHAT = "chat"
    SOCIAL = "social"
    TASK = "task"
    VISION = "vision"
    PRESENCE = "presence"


class WorkflowStatus(StrEnum):
    IDLE = "idle"
    PLANNED = "planned"
    AWAITING_PERMISSION = "awaiting_permission"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    AWAITING_PERMISSION = "awaiting_permission"
    AWAITING_OBSERVATION = "awaiting_observation"
    VERIFIED = "verified"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class TurnEnvelope:
    chat_id: str
    text: str
    mode: TurnMode = TurnMode.CHAT
    message_id: str = ""
    reply_to: str = ""
    media: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class RuntimeEvent:
    kind: str
    session_id: str
    turn_id: str = ""
    workflow_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class RequestedOutput:
    name: str
    description: str
    required: bool = True
    evidence_kind: str = "value"
    required_facts: list[str] = field(default_factory=list)


@dataclass
class GoalContract:
    objective: str
    requested_outputs: list[RequestedOutput]
    success_criteria: list[str]
    risk_level: str = "low"


@dataclass
class StepContract:
    step_id: str
    name: str
    kind: str
    done_condition: str
    allowed_tools: list[str]
    required_facts: list[str] = field(default_factory=list)
    risk_level: str = "low"
    required_sources: list[str] = field(default_factory=list)
    completion_mode: str = "auto"
    status: StepStatus = StepStatus.PLANNED
    attempts: int = 0
    last_action_event_id: str = ""
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class ActionEnvelope:
    tool_name: str
    arguments: dict[str, Any]
    step_id: str
    risk_level: str
    action_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)


@dataclass
class UiElement:
    element_id: str
    name: str
    control_type: str
    bounds: dict[str, int]
    enabled: bool = True
    visible: bool = True

    @property
    def center(self) -> tuple[int, int]:
        return (
            (int(self.bounds.get("left", 0)) + int(self.bounds.get("right", 0))) // 2,
            (int(self.bounds.get("top", 0)) + int(self.bounds.get("bottom", 0))) // 2,
        )


@dataclass
class UiSnapshot:
    snapshot_id: str
    window_id: str
    window_title: str
    revision: str
    elements: list[UiElement]
    created_at: float = field(default_factory=time.time)

    def element(self, element_id: str) -> UiElement | None:
        return next((item for item in self.elements if item.element_id == str(element_id)), None)


@dataclass
class ExecutionEvidence:
    step_id: str
    source: str
    status: str
    summary: str
    facts: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    observation_revision: str = ""
    evidence_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)


@dataclass
class VerificationDecision:
    action_succeeded: bool
    step_satisfied: bool
    goal_satisfied: bool
    reason: str
    missing_outputs: list[str] = field(default_factory=list)
    extracted_outputs: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0


@dataclass
class PermissionState:
    scope: str = "none"
    bundle: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    pending_action: ActionEnvelope | None = None
    granted_at: float = 0.0
    expires_at: float = 0.0


@dataclass
class WorkflowState:
    workflow_id: str
    goal: GoalContract
    steps: list[StepContract]
    status: WorkflowStatus = WorkflowStatus.PLANNED
    current_step_index: int = 0
    evidence: list[ExecutionEvidence] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    verification: VerificationDecision | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def current_step(self) -> StepContract | None:
        if not self.steps:
            return None
        index = max(0, min(self.current_step_index, len(self.steps) - 1))
        return self.steps[index]


@dataclass
class RuntimeState:
    schema_version: int = SCHEMA_VERSION
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    workflow: WorkflowState | None = None
    permission: PermissionState = field(default_factory=PermissionState)
    # Owner concept 2026-07-20: tasks arriving while one is running QUEUE instead of being lost;
    # the queue auto-drains after completion and is queryable via the list_tasks skill.
    task_queue: list[str] = field(default_factory=list)
    processed_event_ids: list[str] = field(default_factory=list)
    render_keys: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)


@dataclass
class V3ToolResult:
    status: str
    message: str
    data: Any = None
    facts: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    error: str = ""
    error_category: str = ""
    retryable: bool = False
    requires_permission: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def runtime_state_from_dict(raw: dict[str, Any] | None) -> RuntimeState:
    raw = raw if isinstance(raw, dict) else {}
    workflow_raw = raw.get("workflow") if isinstance(raw.get("workflow"), dict) else None
    workflow = workflow_from_dict(workflow_raw) if workflow_raw else None
    permission_raw = raw.get("permission") if isinstance(raw.get("permission"), dict) else {}
    pending_raw = (
        permission_raw.get("pending_action") if isinstance(permission_raw.get("pending_action"), dict) else None
    )
    pending = ActionEnvelope(**pending_raw) if pending_raw else None
    permission = PermissionState(
        scope=str(permission_raw.get("scope") or "none"),
        bundle=str(permission_raw.get("bundle") or ""),
        allowed_tools=[str(item) for item in permission_raw.get("allowed_tools") or []],
        pending_action=pending,
        granted_at=float(permission_raw.get("granted_at") or 0.0),
        expires_at=float(permission_raw.get("expires_at") or 0.0),
    )
    return RuntimeState(
        schema_version=SCHEMA_VERSION,
        session_id=str(raw.get("session_id") or uuid.uuid4().hex),
        workflow=workflow,
        permission=permission,
        task_queue=[str(item) for item in raw.get("task_queue") or []][:20],
        processed_event_ids=[str(item) for item in raw.get("processed_event_ids") or []][-2000:],
        render_keys=[str(item) for item in raw.get("render_keys") or []][-1000:],
        updated_at=float(raw.get("updated_at") or time.time()),
    )


def workflow_from_dict(raw: dict[str, Any]) -> WorkflowState:
    goal_raw = raw.get("goal") or {}
    goal = GoalContract(
        objective=str(goal_raw.get("objective") or ""),
        requested_outputs=[RequestedOutput(**item) for item in goal_raw.get("requested_outputs") or []],
        success_criteria=[str(item) for item in goal_raw.get("success_criteria") or []],
        risk_level=str(goal_raw.get("risk_level") or "low"),
    )
    steps = []
    for item in raw.get("steps") or []:
        payload = dict(item)
        payload["status"] = _enum_value(StepStatus, payload.get("status"), StepStatus.PLANNED)
        steps.append(StepContract(**payload))
    evidence = [ExecutionEvidence(**item) for item in raw.get("evidence") or []]
    verification_raw = raw.get("verification") if isinstance(raw.get("verification"), dict) else None
    verification = VerificationDecision(**verification_raw) if verification_raw else None
    return WorkflowState(
        workflow_id=str(raw.get("workflow_id") or uuid.uuid4().hex),
        goal=goal,
        steps=steps,
        status=_enum_value(WorkflowStatus, raw.get("status"), WorkflowStatus.PLANNED),
        current_step_index=int(raw.get("current_step_index") or 0),
        evidence=evidence,
        outputs=dict(raw.get("outputs") or {}),
        verification=verification,
        created_at=float(raw.get("created_at") or time.time()),
        updated_at=float(raw.get("updated_at") or time.time()),
    )


def _enum_value(enum_type: type[Enum], raw: Any, default: Enum) -> Any:
    if isinstance(raw, enum_type):
        return raw
    value = str(raw or default.value)
    prefix = enum_type.__name__ + "."
    if value.startswith(prefix):
        value = value[len(prefix) :].casefold()
    try:
        return enum_type(value)
    except ValueError:
        return default
