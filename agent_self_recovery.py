from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from agent_hooks import HookManager
from agent_tool_runtime import is_safe_verifier_command
from core_tools import ToolResult


TRANSIENT_ERROR_MARKERS = [
    "connectionreseterror",
    "connection aborted",
    "remote end closed",
    "remotedisconnected",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "10054",
    "502",
    "503",
    "504",
]

IDEMPOTENT_RETRY_TOOLS = {
    "send_telegram_media",
    "react_to_message",
    "analyze_media",
    "search_knowledge",
    "read_knowledge",
    "search_sticker",
    "get_screen_ui",
    "read_file",
    "list_files",
    "search_in_files",
}


@dataclass
class RecoveryEvidence:
    reason: str
    original_status: str
    original_message: str
    original_error: str = ""
    attempts: int = 0
    retry_status: str = ""
    retry_message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "original_status": self.original_status,
            "original_message": self.original_message,
            "original_error": self.original_error,
            "attempts": self.attempts,
            "retry_status": self.retry_status,
            "retry_message": self.retry_message,
            "details": self.details,
        }


class SelfRecoveryController:
    """Deterministic, bounded tool recovery before asking the owner for help."""

    def __init__(self, *, executor: Any, hooks: HookManager, session_id: str = "", max_transient_retries: int = 2):
        self.executor = executor
        self.hooks = hooks
        self.session_id = session_id
        self.max_transient_retries = max(0, min(int(max_transient_retries), 3))
        self._attempted: set[str] = set()

    def recover(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        tool_callback: Callable | None = None,
        response_policy: Any = None,
        turn_id: int = 0,
    ) -> tuple[ToolResult, dict[str, Any] | None]:
        if result.status != "error":
            return result, None
        arguments = arguments or {}

        cwd_retry = self._recover_command_cwd(tool_name, arguments, result, tool_callback, response_policy, turn_id)
        if cwd_retry[1] is not None:
            return cwd_retry

        transient_retry = self._recover_transient(tool_name, arguments, result, tool_callback, response_policy, turn_id)
        if transient_retry[1] is not None:
            return transient_retry

        return result, None

    def _recover_command_cwd(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        tool_callback: Callable | None,
        response_policy: Any,
        turn_id: int,
    ) -> tuple[ToolResult, dict[str, Any] | None]:
        if tool_name != "execute_command":
            return result, None
        data = result.data if isinstance(result.data, dict) else {}
        retry_hint = str(data.get("retry_hint") or "")
        original_cwd = str(arguments.get("cwd") or data.get("cwd") or "project")
        if not retry_hint or original_cwd == "project":
            return result, None
        retry_args = dict(arguments)
        retry_args["cwd"] = "project"
        key = self._key(tool_name, retry_args, "cwd_retry")
        if key in self._attempted:
            return result, None
        self._attempted.add(key)
        evidence = RecoveryEvidence(
            reason="cwd_retry",
            original_status=result.status,
            original_message=result.message,
            original_error=result.error,
            attempts=1,
            details={"original_cwd": original_cwd, "retry_cwd": "project", "retry_hint": retry_hint},
        )
        self._emit_attempt(tool_name, turn_id, evidence)
        recovered = self.executor.execute(tool_name, retry_args, tool_callback, response_policy)
        evidence.retry_status = recovered.status
        evidence.retry_message = recovered.message
        if isinstance(recovered.data, dict):
            recovered.data["recovered_from"] = evidence.to_dict()
        self._emit_result(tool_name, turn_id, evidence)
        if recovered.status == "ok":
            return recovered, evidence.to_dict()
        return result, evidence.to_dict()

    def _recover_transient(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        tool_callback: Callable | None,
        response_policy: Any,
        turn_id: int,
    ) -> tuple[ToolResult, dict[str, Any] | None]:
        if not self._is_transient(result) or not self._can_retry_exactly(tool_name, arguments):
            return result, None
        key = self._key(tool_name, arguments, "transient_retry")
        if key in self._attempted:
            return result, None
        self._attempted.add(key)
        evidence = RecoveryEvidence(
            reason="transient_retry",
            original_status=result.status,
            original_message=result.message,
            original_error=result.error,
            attempts=0,
        )
        self._emit_attempt(tool_name, turn_id, evidence)
        recovered = result
        for attempt in range(1, self.max_transient_retries + 1):
            evidence.attempts = attempt
            time.sleep(min(0.2 * attempt, 0.6))
            recovered = self.executor.execute(tool_name, arguments, tool_callback, response_policy)
            evidence.retry_status = recovered.status
            evidence.retry_message = recovered.message
            if recovered.status == "ok":
                if isinstance(recovered.data, dict):
                    recovered.data["recovered_from"] = evidence.to_dict()
                self._emit_result(tool_name, turn_id, evidence)
                return recovered, evidence.to_dict()
            if not self._is_transient(recovered):
                break
        self._emit_result(tool_name, turn_id, evidence)
        return result, evidence.to_dict()

    def _can_retry_exactly(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        if tool_name in IDEMPOTENT_RETRY_TOOLS:
            return True
        if tool_name == "execute_command":
            return is_safe_verifier_command(str(arguments.get("command") or ""))
        return False

    def _is_transient(self, result: ToolResult) -> bool:
        text = " ".join(str(part or "") for part in [result.message, result.error, result.data]).casefold()
        return any(marker in text for marker in TRANSIENT_ERROR_MARKERS)

    def _emit_attempt(self, tool_name: str, turn_id: int, evidence: RecoveryEvidence) -> None:
        self.hooks.emit("SelfRecoveryAttempt", session_id=self.session_id, turn_id=turn_id, tool=tool_name, **evidence.to_dict())

    def _emit_result(self, tool_name: str, turn_id: int, evidence: RecoveryEvidence) -> None:
        self.hooks.emit("SelfRecoveryResult", session_id=self.session_id, turn_id=turn_id, tool=tool_name, **evidence.to_dict())

    def _key(self, tool_name: str, arguments: dict[str, Any], reason: str) -> str:
        return f"{reason}:{tool_name}:{repr(sorted((arguments or {}).items()))[:1000]}"
