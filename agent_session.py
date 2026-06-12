import json
import os
import time
from dataclasses import asdict, dataclass, field

from agent_hooks import emit_trace
from agent_latency import InteractionMode, classify_interaction
from agent_verification import DEFAULT_VERIFICATION_PLANNER


ROOT_DIR = os.path.abspath(os.getenv("YUEYUE_ROOT_DIR") or os.path.dirname(__file__))
PROJECT_CACHE_DIR = os.path.join(ROOT_DIR, "workspace", "project_cache")
SESSION_BRAIN_FILE = os.path.join(PROJECT_CACHE_DIR, "session_brain.json")
TASK_PLAN_FILE = os.path.join(ROOT_DIR, "workspace", "tasks", "task_plan.md")

SESSION_STATES = {"idle", "active_task", "awaiting_permission", "awaiting_validation", "blocked"}
STOP_MARKERS = ["\u7b97\u4e86", "\u505c\u6b62", "\u53d6\u6d88", "\u4e0d\u7528\u505a\u4e86", "stop", "cancel"]
VALIDATION_MARKERS = ["\u6e2c\u8a66", "\u9a57\u8b49", "\u78ba\u8a8d", "test", "verify", "check"]


@dataclass
class SessionBrainState:
    state: str = "idle"
    current_objective: str = ""
    recent_steps: list[str] = field(default_factory=list)
    failed_tools: list[str] = field(default_factory=list)
    pending_validation: list[str] = field(default_factory=list)
    verification_plan: list[str] = field(default_factory=list)
    last_turn_was_chat: bool = True
    consecutive_failures: int = 0
    updated_at: float = field(default_factory=time.time)


@dataclass
class TurnClassification:
    state: str
    intent: str
    reason: str
    is_chat: bool = True


class SessionBrain:
    def __init__(self, path: str = SESSION_BRAIN_FILE):
        self.path = path
        self.state = SessionBrainState()
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
            self.state = SessionBrainState(**{key: value for key, value in data.items() if key in SessionBrainState.__dataclass_fields__})
            if self.state.state not in SESSION_STATES:
                self.state.state = "idle"
        except Exception as exc:
            emit_trace("session_brain.load_failed", error=str(exc))
            self.state = SessionBrainState()

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.state.updated_at = time.time()
            with open(self.path, "w", encoding="utf-8") as file:
                json.dump(asdict(self.state), file, ensure_ascii=False, indent=2)
        except Exception as exc:
            emit_trace("session_brain.save_failed", error=str(exc))

    def classify_turn(self, text: str, grant: str = "none", pending_permission: bool = False, turn_id: int = 0, session_id: str = "") -> TurnClassification:
        normalized = (text or "").strip().casefold()
        old_state = self.state.state
        if _contains_any(normalized, STOP_MARKERS) or grant == "deny":
            self._set_state("idle", turn_id, session_id, "owner_cancelled")
            self.state.current_objective = ""
            self.state.pending_validation = []
            self.state.verification_plan = []
            self.state.last_turn_was_chat = True
            self.save()
            return self._emit_classified("idle", "cancel", "owner_cancelled", True, turn_id, session_id, old_state)

        if pending_permission:
            self._set_state("awaiting_permission", turn_id, session_id, "pending_permission")
            self.state.last_turn_was_chat = False
            self.save()
            return self._emit_classified("awaiting_permission", "permission_reply", "pending_permission", False, turn_id, session_id, old_state)

        if grant in {"single", "turn"}:
            self._set_state("active_task", turn_id, session_id, f"permission_{grant}")
            self.state.last_turn_was_chat = False
            self.save()
            return self._emit_classified("active_task", "permission_granted", f"permission_{grant}", False, turn_id, session_id, old_state)

        interaction = classify_interaction(text, has_media=False)
        if interaction == InteractionMode.TOOL_TASK or _looks_like_task(normalized):
            self._set_state("active_task", turn_id, session_id, "task_intent")
            self.state.current_objective = _shorten(text)
            self.state.last_turn_was_chat = False
            self.save()
            return self._emit_classified("active_task", "task", "task_intent", False, turn_id, session_id, old_state)

        if self.state.state in {"active_task", "awaiting_validation"} and normalized and not _looks_casual(normalized):
            self._set_state("active_task", turn_id, session_id, "task_followup")
            self.state.last_turn_was_chat = False
            self.save()
            return self._emit_classified("active_task", "task_followup", "task_followup", False, turn_id, session_id, old_state)

        self.state.last_turn_was_chat = True
        if not self.state.current_objective and not self.state.pending_validation:
            self._set_state("idle", turn_id, session_id, "chat")
        self.save()
        return self._emit_classified(self.state.state, "chat", "chat", True, turn_id, session_id, old_state)

    def mark_permission_needed(self, tool_name: str, turn_id: int = 0, session_id: str = "") -> None:
        self._set_state("awaiting_permission", turn_id, session_id, f"tool_requires_permission:{tool_name}")
        self._append_recent_step(f"permission needed for {tool_name}")
        self.state.last_turn_was_chat = False
        self.save()

    def mark_tool_result(self, tool_name: str, status: str, turn_id: int = 0, session_id: str = "") -> None:
        if status == "ok":
            self.state.consecutive_failures = 0
            self._append_recent_step(f"{tool_name} ok")
            return
        self.state.consecutive_failures += 1
        self.state.failed_tools.append(tool_name)
        self.state.failed_tools = self.state.failed_tools[-10:]
        self._append_recent_step(f"{tool_name} {status}")
        if self.state.consecutive_failures >= 3:
            self._set_state("blocked", turn_id, session_id, "consecutive_tool_failures")
            emit_trace("session_brain.blocked", session_id=session_id, turn_id=turn_id, failed_tools=self.state.failed_tools[-3:])
        self.save()

    def mark_validation_needed(self, note: str, turn_id: int = 0, session_id: str = "", changed_files: list[str] | None = None, evidence: list[str] | None = None) -> None:
        if note and note not in self.state.pending_validation:
            self.state.pending_validation.append(note)
            self.state.pending_validation = self.state.pending_validation[-5:]
        plan = DEFAULT_VERIFICATION_PLANNER.plan(note, changed_files=changed_files, evidence=evidence)
        self.state.verification_plan = plan.summary().splitlines()[-8:]
        if self.state.state != "blocked":
            self._set_state("awaiting_validation", turn_id, session_id, "tool_completed")
        emit_trace("session_brain.validation_needed", session_id=session_id, turn_id=turn_id, pending_validation=self.state.pending_validation, verification_plan=self.state.verification_plan)
        self.save()

    def mark_verification_result(self, status: str, evidence: list[str] | None = None, turn_id: int = 0, session_id: str = "") -> None:
        evidence = evidence or []
        if status == "ok":
            self.state.pending_validation = []
            self.state.verification_plan = []
            self.state.consecutive_failures = 0
            if self.state.state != "idle":
                self._set_state("idle", turn_id, session_id, "verification_passed")
            self._append_recent_step("verification ok")
        else:
            self.state.consecutive_failures += 1
            self._append_recent_step("verification failed")
            if self.state.consecutive_failures >= 3:
                self._set_state("blocked", turn_id, session_id, "verification_failed_repeatedly")
                emit_trace("session_brain.blocked", session_id=session_id, turn_id=turn_id, failed_tools=self.state.failed_tools[-3:], reason="verification_failed")
            else:
                self._set_state("awaiting_validation", turn_id, session_id, "verification_failed")
        emit_trace("session_brain.verification_result", session_id=session_id, turn_id=turn_id, status=status, evidence=evidence[-5:])
        self.save()

    def summary(self, max_chars: int = 1200) -> str:
        lines = [
            f"state: {self.state.state}",
            f"current_objective: {self.state.current_objective or '[none]'}",
            f"last_turn_was_chat: {self.state.last_turn_was_chat}",
        ]
        if self.state.recent_steps:
            lines.append("recent_steps: " + " | ".join(self.state.recent_steps[-5:]))
        if self.state.failed_tools:
            lines.append("failed_tools: " + ", ".join(self.state.failed_tools[-5:]))
        if self.state.pending_validation:
            lines.append("pending_validation: " + " | ".join(self.state.pending_validation[-5:]))
        if self.state.verification_plan:
            lines.append("verification_plan: " + " | ".join(self.state.verification_plan[-5:]))
        return "\n".join(lines)[-max_chars:]

    def _append_recent_step(self, step: str) -> None:
        self.state.recent_steps.append(step)
        self.state.recent_steps = self.state.recent_steps[-10:]

    def _set_state(self, new_state: str, turn_id: int, session_id: str, reason: str) -> None:
        if new_state not in SESSION_STATES:
            new_state = "idle"
        old_state = self.state.state
        self.state.state = new_state
        if old_state != new_state:
            emit_trace("session_brain.state_changed", session_id=session_id, turn_id=turn_id, old_state=old_state, new_state=new_state, reason=reason)

    def _emit_classified(self, state: str, intent: str, reason: str, is_chat: bool, turn_id: int, session_id: str, old_state: str) -> TurnClassification:
        emit_trace("session_brain.classified", session_id=session_id, turn_id=turn_id, old_state=old_state, state=state, intent=intent, reason=reason, is_chat=is_chat)
        return TurnClassification(state=state, intent=intent, reason=reason, is_chat=is_chat)


def load_session_brain_summary(path: str = SESSION_BRAIN_FILE) -> str:
    brain = SessionBrain(path)
    return brain.summary()


def _contains_any(text: str, markers: list[str]) -> bool:
    return any(marker.casefold() in text for marker in markers)


def _looks_like_task(text: str) -> bool:
    markers = [
        "\u5e6b\u6211",
        "\u4fee",
        "\u505a",
        "\u6e2c\u8a66",
        "\u6aa2\u67e5",
        "\u627e bug",
        "\u5be6\u73fe",
        "\u512a\u5316",
        "please implement",
        "implement",
        "debug",
        "fix",
        "run",
        "test",
        "optimize",
    ]
    return _contains_any(text, markers)


def _looks_casual(text: str) -> bool:
    casual = ["\u54c8", "\u65e9", "\u665a\u5b89", "\u55ef", "\u597d\u8036", "\u53ef\u611b", "lol", "haha"]
    return len(text) < 80 and _contains_any(text, casual)


def _shorten(text: str, limit: int = 180) -> str:
    text = " ".join((text or "").split())
    return text[:limit]
