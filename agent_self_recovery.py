from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from agent_hooks import HookManager
from agent_tool_runtime import is_safe_verifier_command
from core_tools import PROJECT_CACHE_DIR, ToolResult


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

SELF_REPAIR_TOOLS = {
    "execute_command",
    "execute_python",
    "read_file",
    "list_files",
    "search_in_files",
    "analyze_media",
    "send_telegram_media",
    "get_screen_ui",
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

        python_fallback = self._recover_missing_python_dependency(tool_name, arguments, result, tool_callback, response_policy, turn_id)
        if python_fallback[1] is not None:
            return python_fallback

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
        recovered = self.executor.execute(tool_name, retry_args, tool_callback, _recovery_policy(response_policy))
        evidence.retry_status = recovered.status
        evidence.retry_message = recovered.message
        if isinstance(recovered.data, dict):
            recovered.data["recovered_from"] = evidence.to_dict()
        self._emit_result(tool_name, turn_id, evidence)
        if recovered.status == "ok":
            return recovered, evidence.to_dict()
        return result, evidence.to_dict()

    def _recover_missing_python_dependency(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        tool_callback: Callable | None,
        response_policy: Any,
        turn_id: int,
    ) -> tuple[ToolResult, dict[str, Any] | None]:
        if tool_name != "execute_python":
            return result, None
        module_name = _missing_module_name(result)
        if module_name != "mss" or not _looks_like_screenshot_code(str(arguments.get("code") or "")):
            return result, None
        fallback_path = os.path.join(PROJECT_CACHE_DIR, "fullscreen_screenshot.png")
        fallback_code = _mss_screenshot_fallback_code(fallback_path)
        try:
            timeout = min(max(1, int(arguments.get("timeout") or 30)), 30)
        except Exception:
            timeout = 30
        retry_args = {"code": fallback_code, "timeout": timeout}
        key = self._key(tool_name, retry_args, "missing_mss_screenshot_fallback")
        if key in self._attempted:
            return result, None
        self._attempted.add(key)
        evidence = RecoveryEvidence(
            reason="missing_mss_screenshot_fallback",
            original_status=result.status,
            original_message=result.message,
            original_error=result.error,
            attempts=1,
            details={"missing_module": module_name, "fallback_path": fallback_path},
        )
        self._emit_attempt(tool_name, turn_id, evidence)
        recovered = self.executor.execute(tool_name, retry_args, tool_callback, _recovery_policy(response_policy))
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
            recovered = self.executor.execute(tool_name, arguments, tool_callback, _recovery_policy(response_policy))
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


def should_prompt_self_repair(tool_name: str, result: ToolResult, response_policy: Any = None) -> bool:
    if result.status != "error":
        return False
    if tool_name not in SELF_REPAIR_TOOLS:
        return False
    route = str(getattr(response_policy, "route", "") or "").casefold()
    if route in {"chat", "social_sticker"}:
        return False
    return True


def _missing_module_name(result: ToolResult) -> str:
    text = " ".join(str(part or "") for part in [result.message, result.error, result.data])
    marker = "No module named "
    if marker not in text:
        return ""
    tail = text.split(marker, 1)[1].strip()
    if not tail:
        return ""
    quote = tail[0] if tail[0] in {"'", '"'} else ""
    if quote:
        tail = tail[1:].split(quote, 1)[0]
    else:
        tail = tail.split()[0].strip(".,:;")
    return tail.strip().casefold()


def _looks_like_screenshot_code(code: str) -> bool:
    lowered = (code or "").casefold()
    if "import mss" not in lowered and "from mss" not in lowered:
        return False
    return any(marker in lowered for marker in ["screenshot", "screen", "monitor", "grab", ".png", ".jpg", ".jpeg"])


def _mss_screenshot_fallback_code(output_path: str) -> str:
    escaped = output_path.replace("\\", "\\\\")
    return (
        "from pathlib import Path\n"
        f"output = Path(r'{escaped}')\n"
        "output.parent.mkdir(parents=True, exist_ok=True)\n"
        "try:\n"
        "    import pyautogui\n"
        "    image = pyautogui.screenshot()\n"
        "    image.save(output)\n"
        "except Exception:\n"
        "    from PIL import ImageGrab\n"
        "    image = ImageGrab.grab()\n"
        "    image.save(output)\n"
        "print(str(output))\n"
    )


def _recovery_policy(response_policy: Any) -> Any:
    if response_policy is None:
        return None
    try:
        return replace(
            response_policy,
            allowed_tools=None,
            max_tool_iterations=1,
            route=(str(getattr(response_policy, "route", "") or "tool_task") + "_recovery"),
        )
    except Exception:
        return response_policy


def self_repair_instruction(tool_name: str, arguments: dict[str, Any], result: ToolResult) -> str:
    data = result.data if isinstance(result.data, dict) else {}
    retry_hint = str(data.get("retry_hint") or "").strip()
    stderr = str(data.get("stderr") or result.error or "").strip()
    stdout = str(data.get("stdout") or "").strip()
    parts = [
        "[SelfRepair]",
        f"The previous `{tool_name}` call failed. Do not stop at the raw error if a safe recovery is possible.",
        "First inspect the error, then choose one bounded next step: retry with corrected cwd/path, inspect relevant files, run an allowlisted verifier, or explain clearly if permission or external state is required.",
        "Do not repeat the exact same failing tool call. Do not invent success. Do not run destructive commands.",
    ]
    if retry_hint:
        parts.append(f"retry_hint: {retry_hint}")
    if stderr:
        parts.append(f"stderr: {stderr[:900]}")
    elif stdout:
        parts.append(f"stdout: {stdout[:900]}")
    missing_module = _missing_module_name(result)
    if missing_module:
        parts.append(
            f"diagnosis: Python dependency `{missing_module}` is missing. Prefer a safe fallback using already available libraries; do not install packages unless the owner explicitly approves."
        )
    if arguments:
        parts.append(f"failed_arguments: {repr(arguments)[:900]}")
    return "\n".join(parts)
