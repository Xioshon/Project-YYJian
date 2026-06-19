from __future__ import annotations

import os
import re
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


@dataclass(frozen=True)
class ErrorDiagnosis:
    category: str
    confidence: float
    detail: str = ""
    retryable: bool = False
    safe_to_auto_repair: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryPlan:
    strategy: str
    reason: str
    retry_args: dict[str, Any] | None = None
    max_attempts: int = 1
    requires_same_tool: bool = True
    tool_name: str = ""
    details: dict[str, Any] = field(default_factory=dict)


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


class RepairPlanner:
    """Produces ordered, safe recovery candidates for a failed tool result."""

    def __init__(self, max_transient_retries: int = 2):
        self.max_transient_retries = max(0, min(int(max_transient_retries), 3))

    def candidates(self, tool_name: str, arguments: dict[str, Any], result: ToolResult, diagnosis: ErrorDiagnosis | None = None) -> list[RecoveryPlan]:
        diagnosis = diagnosis or diagnose_tool_error(tool_name, arguments or {}, result)
        return plan_recovery_candidates(tool_name, arguments or {}, result, diagnosis, self.max_transient_retries)


class SelfRecoveryController:
    """Deterministic, bounded tool recovery before asking the owner for help."""

    def __init__(self, *, executor: Any, hooks: HookManager, session_id: str = "", max_transient_retries: int = 2):
        self.executor = executor
        self.hooks = hooks
        self.session_id = session_id
        self.max_transient_retries = max(0, min(int(max_transient_retries), 3))
        self.planner = RepairPlanner(self.max_transient_retries)
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
        diagnosis = diagnose_tool_error(tool_name, arguments, result)
        if diagnosis.category == "transient_external_error":
            safe_retry = self._can_retry_exactly(tool_name, arguments)
            diagnosis = replace(
                diagnosis,
                retryable=safe_retry,
                safe_to_auto_repair=safe_retry,
                evidence={**diagnosis.evidence, "safe_exact_retry": safe_retry},
            )
        plans = self.planner.candidates(tool_name, arguments, result, diagnosis)
        if not plans:
            return result, None

        last_skipped = False
        failed_attempts: list[dict[str, Any]] = []
        for plan in plans:
            target_tool = plan.tool_name or tool_name
            if self._has_attempted(target_tool, plan.retry_args or arguments, plan.strategy):
                last_skipped = True
                continue

            if plan.strategy in {"cwd_retry", "command_file_probe"}:
                recovered, evidence = self._recover_command_cwd(tool_name, arguments, result, tool_callback, response_policy, turn_id, diagnosis, plan)
            elif plan.strategy in {"screenshot_capture_fallback", "screen_ui_snapshot_fallback"}:
                recovered, evidence = self._recover_missing_python_dependency(tool_name, arguments, result, tool_callback, response_policy, turn_id, diagnosis, plan)
            elif plan.strategy == "transient_retry":
                recovered, evidence = self._recover_transient(tool_name, arguments, result, tool_callback, response_policy, turn_id, diagnosis, plan)
            else:
                continue

            if recovered.status == "ok":
                if failed_attempts and isinstance(recovered.data, dict):
                    recovered.data.setdefault("prior_recovery_attempts", failed_attempts)
                return recovered, evidence
            if evidence:
                failed_attempts.append(evidence)
            if len(plans) == 1:
                return recovered, evidence

        if last_skipped:
            self.hooks.emit("SelfRecoverySkipped", session_id=self.session_id, turn_id=turn_id, tool=tool_name, reason="all_candidates_already_attempted")

        if failed_attempts:
            exhausted = RecoveryEvidence(
                reason="repair_planner_exhausted",
                original_status=result.status,
                original_message=result.message,
                original_error=result.error,
                attempts=sum(int(item.get("attempts") or 0) for item in failed_attempts),
                retry_status=str(failed_attempts[-1].get("retry_status") or "error"),
                retry_message=str(failed_attempts[-1].get("retry_message") or ""),
                details={"candidate_attempts": failed_attempts},
            )
            return _with_recovery_attempt(result, exhausted), exhausted.to_dict()

        return result, None

    def _recover_command_cwd(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        tool_callback: Callable | None,
        response_policy: Any,
        turn_id: int,
        diagnosis: ErrorDiagnosis | None = None,
        plan: RecoveryPlan | None = None,
    ) -> tuple[ToolResult, dict[str, Any] | None]:
        if tool_name != "execute_command" or plan is None or not plan.retry_args:
            return result, None
        data = result.data if isinstance(result.data, dict) else {}
        retry_hint = str(data.get("retry_hint") or "")
        original_cwd = str(arguments.get("cwd") or data.get("cwd") or "project")
        retry_args = dict(plan.retry_args)
        target_tool = plan.tool_name or tool_name
        key = self._key(target_tool, retry_args, plan.strategy)
        if key in self._attempted:
            return result, None
        self._attempted.add(key)
        evidence = RecoveryEvidence(
            reason=plan.reason,
            original_status=result.status,
            original_message=result.message,
            original_error=result.error,
            attempts=1,
            details={
                "strategy": plan.strategy,
                "diagnosis": (diagnosis or diagnose_tool_error(tool_name, arguments, result)).category,
                "original_cwd": original_cwd,
                "retry_cwd": retry_args.get("cwd"),
                "retry_hint": retry_hint,
                **plan.details,
            },
        )
        self._emit_attempt(tool_name, turn_id, evidence)
        recovered = self.executor.execute(target_tool, retry_args, tool_callback, _recovery_policy(response_policy))
        evidence.retry_status = recovered.status
        evidence.retry_message = recovered.message
        if isinstance(recovered.data, dict):
            recovered.data["recovered_from"] = evidence.to_dict()
        self._emit_result(tool_name, turn_id, evidence)
        if recovered.status == "ok":
            return recovered, evidence.to_dict()
        return _with_recovery_attempt(result, evidence), evidence.to_dict()

    def _recover_missing_python_dependency(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        tool_callback: Callable | None,
        response_policy: Any,
        turn_id: int,
        diagnosis: ErrorDiagnosis | None = None,
        plan: RecoveryPlan | None = None,
    ) -> tuple[ToolResult, dict[str, Any] | None]:
        if tool_name != "execute_python" or plan is None:
            return result, None
        target_tool = plan.tool_name or tool_name
        diagnosis = diagnosis or diagnose_tool_error(tool_name, arguments, result)
        retry_args = dict(plan.retry_args or {})
        key = self._key(target_tool, retry_args, plan.strategy)
        if key in self._attempted:
            return result, None
        self._attempted.add(key)
        evidence = RecoveryEvidence(
            reason=plan.reason,
            original_status=result.status,
            original_message=result.message,
            original_error=result.error,
            attempts=1,
            details={"strategy": plan.strategy, "diagnosis": diagnosis.category, **diagnosis.evidence, **plan.details},
        )
        self._emit_attempt(tool_name, turn_id, evidence)
        recovered = self.executor.execute(target_tool, retry_args, tool_callback, _recovery_policy(response_policy))
        evidence.retry_status = recovered.status
        evidence.retry_message = recovered.message
        if isinstance(recovered.data, dict):
            recovered.data["recovered_from"] = evidence.to_dict()
        self._emit_result(tool_name, turn_id, evidence)
        if recovered.status == "ok":
            return recovered, evidence.to_dict()
        return _with_recovery_attempt(result, evidence), evidence.to_dict()

    def _recover_transient(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        tool_callback: Callable | None,
        response_policy: Any,
        turn_id: int,
        diagnosis: ErrorDiagnosis | None = None,
        plan: RecoveryPlan | None = None,
    ) -> tuple[ToolResult, dict[str, Any] | None]:
        if plan is None:
            return result, None
        key = self._key(tool_name, arguments, plan.strategy)
        if key in self._attempted:
            return result, None
        self._attempted.add(key)
        evidence = RecoveryEvidence(
            reason=plan.reason,
            original_status=result.status,
            original_message=result.message,
            original_error=result.error,
            attempts=0,
            details={"strategy": plan.strategy, "diagnosis": (diagnosis or diagnose_tool_error(tool_name, arguments, result)).category, **plan.details},
        )
        self._emit_attempt(tool_name, turn_id, evidence)
        recovered = result
        for attempt in range(1, max(1, plan.max_attempts) + 1):
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
        return _with_recovery_attempt(result, evidence), evidence.to_dict()

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

    def _has_attempted(self, tool_name: str, arguments: dict[str, Any], reason: str) -> bool:
        return self._key(tool_name, arguments, reason) in self._attempted


def diagnose_tool_error(tool_name: str, arguments: dict[str, Any], result: ToolResult) -> ErrorDiagnosis:
    arguments = arguments or {}
    if result.status != "error":
        return ErrorDiagnosis("not_error", 1.0, retryable=False, safe_to_auto_repair=False)

    if tool_name == "execute_command":
        data = result.data if isinstance(result.data, dict) else {}
        retry_hint = str(data.get("retry_hint") or "").strip()
        original_cwd = str(arguments.get("cwd") or data.get("cwd") or "project")
        if retry_hint and original_cwd != "project":
            return ErrorDiagnosis(
                "cwd_or_path_mismatch",
                0.9,
                detail=retry_hint,
                retryable=True,
                safe_to_auto_repair=True,
                evidence={"original_cwd": original_cwd, "retry_hint": retry_hint},
            )

    missing_module = _missing_module_name(result)
    if tool_name == "execute_python" and missing_module:
        screenshot_context = _looks_like_screenshot_code(str(arguments.get("code") or ""))
        return ErrorDiagnosis(
            "missing_python_module",
            0.95,
            detail=missing_module,
            retryable=screenshot_context and missing_module in {"mss", "pyautogui", "pyscreeze", "pil", "pillow"},
            safe_to_auto_repair=screenshot_context and missing_module in {"mss", "pyautogui", "pyscreeze", "pil", "pillow"},
            evidence={"missing_module": missing_module, "screenshot_context": screenshot_context},
        )

    if tool_name == "execute_python" and _looks_like_screenshot_code(str(arguments.get("code") or "")) and _looks_like_screenshot_runtime_error(result):
        return ErrorDiagnosis(
            "screenshot_python_failure",
            0.86,
            detail="screenshot capture code failed",
            retryable=True,
            safe_to_auto_repair=True,
            evidence={"screenshot_context": True, "error_family": "screen_capture"},
        )

    if _is_transient_result(result):
        safe_retry = _can_retry_tool_exactly(tool_name, arguments)
        return ErrorDiagnosis(
            "transient_external_error",
            0.8,
            detail="temporary transport or service failure",
            retryable=safe_retry,
            safe_to_auto_repair=safe_retry,
            evidence={"safe_exact_retry": safe_retry},
        )

    return ErrorDiagnosis("unknown_error", 0.3, detail=result.error or result.message, retryable=False, safe_to_auto_repair=False)


def plan_recovery(
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
    diagnosis: ErrorDiagnosis,
    max_transient_retries: int = 2,
) -> RecoveryPlan | None:
    candidates = plan_recovery_candidates(tool_name, arguments, result, diagnosis, max_transient_retries)
    return candidates[0] if candidates else None


def plan_recovery_candidates(
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolResult,
    diagnosis: ErrorDiagnosis,
    max_transient_retries: int = 2,
) -> list[RecoveryPlan]:
    if not diagnosis.safe_to_auto_repair:
        return []

    if diagnosis.category == "cwd_or_path_mismatch" and tool_name == "execute_command":
        retry_args = dict(arguments or {})
        retry_args["cwd"] = "project"
        candidates = [
            RecoveryPlan(
                strategy="cwd_retry",
                reason="cwd_retry",
                retry_args=retry_args,
                details={"retry_cwd": "project", "candidate_order": 1},
            )
        ]
        probe_args = _command_file_probe_args(str((arguments or {}).get("command") or ""))
        if probe_args:
            candidates.append(
                RecoveryPlan(
                    strategy="command_file_probe",
                    reason="command_file_probe",
                    retry_args=probe_args,
                    requires_same_tool=False,
                    tool_name="execute_python",
                    details={"candidate_order": 2, "probe": "referenced_files"},
                )
            )
        return candidates

    if diagnosis.category in {"missing_python_module", "screenshot_python_failure"} and tool_name == "execute_python":
        module_name = str(diagnosis.evidence.get("missing_module") or diagnosis.detail)
        if diagnosis.evidence.get("screenshot_context"):
            fallback_path = os.path.join(PROJECT_CACHE_DIR, "fullscreen_screenshot.png")
            try:
                timeout = min(max(1, int((arguments or {}).get("timeout") or 30)), 30)
            except Exception:
                timeout = 30
            return [
                RecoveryPlan(
                    strategy="screenshot_capture_fallback",
                    reason="screenshot_capture_fallback",
                    retry_args={"code": _mss_screenshot_fallback_code(fallback_path), "timeout": timeout},
                    details={"fallback_path": fallback_path, "missing_module": module_name, "candidate_order": 1},
                ),
                RecoveryPlan(
                    strategy="screen_ui_snapshot_fallback",
                    reason="screen_ui_snapshot_fallback",
                    retry_args={},
                    requires_same_tool=False,
                    tool_name="get_screen_ui",
                    details={"fallback_kind": "ui_snapshot", "candidate_order": 2},
                ),
            ]

    if diagnosis.category == "transient_external_error":
        return [
            RecoveryPlan(
                strategy="transient_retry",
                reason="transient_retry",
                retry_args=dict(arguments or {}),
                max_attempts=max(0, min(int(max_transient_retries), 3)),
                details={"safe_exact_retry": True, "candidate_order": 1},
            )
        ]

    return []


def _can_retry_tool_exactly(tool_name: str, arguments: dict[str, Any]) -> bool:
    if tool_name in IDEMPOTENT_RETRY_TOOLS:
        return True
    if tool_name == "execute_command":
        return is_safe_verifier_command(str((arguments or {}).get("command") or ""))
    return False


def _command_file_probe_args(command: str) -> dict[str, Any] | None:
    names = _extract_command_file_names(command)
    if not names:
        return None
    payload = repr(names[:8])
    code = (
        "from pathlib import Path\n"
        f"names = {payload}\n"
        "roots = [Path.cwd(), Path.cwd() / 'workspace']\n"
        "for name in names:\n"
        "    matches = []\n"
        "    for root in roots:\n"
        "        try:\n"
        "            matches.extend(str(path) for path in root.rglob(name) if path.is_file())\n"
        "        except Exception:\n"
        "            pass\n"
        "    print(f'{name}: ' + (' | '.join(matches[:8]) if matches else 'not found'))\n"
    )
    return {"code": code, "timeout": 20}


def _extract_command_file_names(command: str) -> list[str]:
    found: list[str] = []
    for match in re.findall(r"(?<![\w.-])([\w.-]+\.(?:py|md|json|txt|yaml|yml|toml|ini|log))(?![\w.-])", command or "", flags=re.IGNORECASE):
        name = os.path.basename(match.strip("\"'` "))
        if name and name not in found:
            found.append(name)
    return found


def _is_transient_result(result: ToolResult) -> bool:
    text = " ".join(str(part or "") for part in [result.message, result.error, result.data]).casefold()
    return any(marker in text for marker in TRANSIENT_ERROR_MARKERS)


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
    capture_imports = [
        "import mss",
        "from mss",
        "import pyautogui",
        "from pyautogui",
        "imagegrab",
        "from pil",
    ]
    if not any(marker in lowered for marker in capture_imports):
        return False
    return any(marker in lowered for marker in ["screenshot", "screen", "monitor", "grab", ".png", ".jpg", ".jpeg"])


def _looks_like_screenshot_runtime_error(result: ToolResult) -> bool:
    text = " ".join(str(part or "") for part in [result.message, result.error, result.data]).casefold()
    markers = [
        "attributeerror",
        "typeerror",
        "nameerror",
        "screen shot",
        "screenshot",
        "grab",
        "monitor",
        "imagegrab",
        "pyautogui",
        "mss",
    ]
    return any(marker in text for marker in markers)


def _mss_screenshot_fallback_code(output_path: str) -> str:
    escaped = output_path.replace("\\", "\\\\")
    return (
        "import subprocess\n"
        "from pathlib import Path\n"
        f"output = Path(r'{escaped}')\n"
        "output.parent.mkdir(parents=True, exist_ok=True)\n"
        "ps_path = str(output).replace(\"'\", \"''\")\n"
        "script = f'''\n"
        "Add-Type -AssemblyName System.Windows.Forms\n"
        "Add-Type -AssemblyName System.Drawing\n"
        "$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen\n"
        "$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height\n"
        "$graphics = [System.Drawing.Graphics]::FromImage($bmp)\n"
        "$graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bounds.Size)\n"
        "$bmp.Save('{ps_path}', [System.Drawing.Imaging.ImageFormat]::Png)\n"
        "$graphics.Dispose()\n"
        "$bmp.Dispose()\n"
        "'''\n"
        "completed = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script], capture_output=True, text=True, timeout=25)\n"
        "if completed.returncode != 0:\n"
        "    raise RuntimeError((completed.stderr or completed.stdout or 'PowerShell screenshot failed')[:1000])\n"
        "if not output.exists() or output.stat().st_size <= 0:\n"
        "    raise RuntimeError('PowerShell screenshot did not create an image')\n"
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


def _with_recovery_attempt(result: ToolResult, evidence: RecoveryEvidence) -> ToolResult:
    data = dict(result.data) if isinstance(result.data, dict) else {}
    data["recovery_attempted"] = evidence.to_dict()
    return replace(result, data=data)


def self_repair_instruction(tool_name: str, arguments: dict[str, Any], result: ToolResult) -> str:
    data = result.data if isinstance(result.data, dict) else {}
    retry_hint = str(data.get("retry_hint") or "").strip()
    stderr = str(data.get("stderr") or result.error or "").strip()
    stdout = str(data.get("stdout") or "").strip()
    recovery_attempted = data.get("recovery_attempted") if isinstance(data, dict) else None
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
    if isinstance(recovery_attempted, dict):
        reason = str(recovery_attempted.get("reason") or recovery_attempted.get("details", {}).get("strategy") or "auto_recovery").strip()
        retry_status = str(recovery_attempted.get("retry_status") or "not_ok").strip()
        retry_message = str(recovery_attempted.get("retry_message") or "").strip()
        attempts = recovery_attempted.get("attempts") or 0
        parts.append(
            "deterministic_recovery_already_tried: "
            f"strategy={reason}; attempts={attempts}; status={retry_status}; message={retry_message[:500]}"
        )
        parts.append("Do not repeat that exact recovery path unless you change one concrete cause.")
    if arguments:
        parts.append(f"failed_arguments: {repr(arguments)[:900]}")
    return "\n".join(parts)
