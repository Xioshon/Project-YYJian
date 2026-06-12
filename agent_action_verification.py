import os
from dataclasses import asdict, dataclass, field
from typing import Any

from agent_hooks import emit_trace
from core_tools import ToolResult, resolve_path


@dataclass
class ActionVerificationResult:
    tool_name: str
    status: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_action(tool_name: str, arguments: dict[str, Any], result: ToolResult, session_id: str = "", turn_id: int = 0) -> ActionVerificationResult:
    arguments = arguments or {}
    if result.status != "ok":
        verification = ActionVerificationResult(tool_name, "fail", f"tool status is {result.status}", {"tool_message": result.message})
        _emit(verification, session_id, turn_id)
        return verification

    if tool_name in {"write_file", "download_file"}:
        filename = _path_from_result_or_args(result, arguments, "filename")
        exists = bool(filename and os.path.exists(filename))
        verification = ActionVerificationResult(tool_name, "pass" if exists else "fail", "file exists after action" if exists else "expected file is missing", {"path": filename})
    elif tool_name == "delete_file":
        filename = _path_from_result_or_args(result, arguments, "filename")
        absent = bool(filename and not os.path.exists(filename))
        verification = ActionVerificationResult(tool_name, "pass" if absent else "fail", "file absent after delete" if absent else "file still exists after delete", {"path": filename})
    elif tool_name in {"execute_command", "execute_python", "execute_async_command"}:
        data = result.data if isinstance(result.data, dict) else {}
        code = data.get("returncode")
        ok = code in (None, 0)
        verification = ActionVerificationResult(tool_name, "pass" if ok else "fail", "process result accepted" if ok else f"process returned {code}", {"returncode": code})
    elif tool_name == "send_telegram_media":
        verification = ActionVerificationResult(tool_name, "pass", "telegram media tool returned ok")
    elif tool_name in {"click_ui_element", "type_keyboard", "press_hotkey"}:
        verification = ActionVerificationResult(tool_name, "observe_needed", "UI action completed; follow-up observation is required")
    else:
        verification = ActionVerificationResult(tool_name, "pass", "tool returned ok")
    _emit(verification, session_id, turn_id)
    return verification


def _path_from_result_or_args(result: ToolResult, arguments: dict[str, Any], preferred_key: str) -> str:
    data = result.data if isinstance(result.data, dict) else {}
    value = data.get("path") or data.get("file_path") or data.get(preferred_key) or arguments.get(preferred_key)
    if not isinstance(value, str) or not value:
        return ""
    try:
        return resolve_path(value)
    except Exception:
        return value


def _emit(verification: ActionVerificationResult, session_id: str, turn_id: int) -> None:
    emit_trace("ActionVerification", session_id=session_id, turn_id=turn_id, **verification.to_dict())
