import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from agent_hooks import emit_trace
from core_tools import PROJECT_CACHE_DIR, ToolResult, resolve_path


TASK_TRANSACTIONS_FILE = os.path.join(PROJECT_CACHE_DIR, "task_transactions.json")


@dataclass
class TransactionStep:
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    status: str = ""
    message: str = ""
    verification_status: str = ""
    verification_message: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class TaskTransaction:
    task_id: str
    objective: str = ""
    status: str = "active"
    current_step: str = ""
    created_files: list[str] = field(default_factory=list)
    cleanup: bool = True
    steps: list[TransactionStep] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class TaskTransactionManager:
    def __init__(self, path: str = TASK_TRANSACTIONS_FILE):
        self.path = path
        self.transactions: list[TaskTransaction] = []
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                raw_items = json.load(file)
            self.transactions = [_transaction_from_dict(item) for item in raw_items if isinstance(item, dict)]
        except Exception as exc:
            emit_trace("task_transaction.load_failed", error=str(exc), path=self.path)
            self.transactions = []

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as file:
                json.dump([asdict(item) for item in self.transactions[-50:]], file, ensure_ascii=False, indent=2)
        except Exception as exc:
            emit_trace("task_transaction.save_failed", error=str(exc), path=self.path)

    def active(self) -> TaskTransaction | None:
        for item in reversed(self.transactions):
            if item.status in {"active", "awaiting_permission", "awaiting_validation"}:
                return item
        return None

    def start_or_resume(self, objective: str, session_id: str = "", turn_id: int = 0) -> TaskTransaction:
        current = self.active()
        if current:
            current.updated_at = time.time()
            if objective and not current.objective:
                current.objective = objective[:500]
            self.save()
            return current
        task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        transaction = TaskTransaction(task_id=task_id, objective=(objective or "")[:500])
        self.transactions.append(transaction)
        self.save()
        emit_trace("task_transaction.started", session_id=session_id, turn_id=turn_id, task_id=task_id, objective=transaction.objective[:160])
        return transaction

    def record_tool_result(self, tool_name: str, arguments: dict[str, Any], result: ToolResult, verification: Any = None, session_id: str = "", turn_id: int = 0) -> TaskTransaction:
        transaction = self.start_or_resume("", session_id=session_id, turn_id=turn_id)
        transaction.current_step = tool_name
        transaction.status = "awaiting_permission" if result.status == "blocked" and result.requires_permission else "active"
        verification_status = getattr(verification, "status", "") if verification else ""
        verification_message = getattr(verification, "message", "") if verification else ""
        transaction.steps.append(
            TransactionStep(
                tool_name=tool_name,
                arguments=_safe_arguments(arguments),
                status=result.status,
                message=result.message[:800],
                verification_status=verification_status,
                verification_message=verification_message[:800],
            )
        )
        for path in _created_file_candidates(tool_name, arguments, result):
            if path not in transaction.created_files:
                transaction.created_files.append(path)
        if verification_status == "pass":
            transaction.status = "awaiting_validation"
        elif verification_status == "fail":
            transaction.status = "blocked"
        transaction.updated_at = time.time()
        self.save()
        emit_trace(
            "task_transaction.step_recorded",
            session_id=session_id,
            turn_id=turn_id,
            task_id=transaction.task_id,
            tool=tool_name,
            status=result.status,
            verification_status=verification_status,
            created_files=transaction.created_files[-5:],
        )
        return transaction

    def mark_blocked(self, reason: str, session_id: str = "", turn_id: int = 0) -> None:
        transaction = self.active()
        if not transaction:
            return
        transaction.status = "blocked"
        transaction.updated_at = time.time()
        self.save()
        emit_trace("task_transaction.blocked", session_id=session_id, turn_id=turn_id, task_id=transaction.task_id, reason=reason)


def _transaction_from_dict(item: dict[str, Any]) -> TaskTransaction:
    steps = [TransactionStep(**step) for step in item.get("steps", []) if isinstance(step, dict)]
    fields = {key: value for key, value in item.items() if key in TaskTransaction.__dataclass_fields__ and key != "steps"}
    return TaskTransaction(**fields, steps=steps)


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str) and len(value) > 500:
            clean[key] = value[:500] + "...[truncated]"
        else:
            clean[key] = value
    return clean


def _created_file_candidates(tool_name: str, arguments: dict[str, Any], result: ToolResult) -> list[str]:
    candidates: list[str] = []
    data = result.data if isinstance(result.data, dict) else {}
    for key in ("path", "file_path", "filename", "saved_to"):
        value = data.get(key) or (arguments or {}).get(key)
        if isinstance(value, str) and value:
            try:
                candidates.append(resolve_path(value))
            except Exception:
                candidates.append(value)
    if tool_name not in {"write_file", "download_file", "analyze_media"}:
        return []
    return sorted(set(candidates))
