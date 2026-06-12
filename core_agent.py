import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from openai import OpenAI

from agent_hooks import DEFAULT_HOOK_MANAGER, HookDecision, HookManager, TRACE_LOG_FILE, emit_trace
from agent_action_verification import ActionVerificationResult, verify_action
from agent_latency import ResponsePolicy
from agent_planner import DEFAULT_PLANNER
from agent_protocol import EMPTY_REPLY_FALLBACK, FAIL_SAFE_REPLY, TOOL_LOOP_TIMEOUT_REPLY, classify_approval, screenshot_tags
from agent_replay import record_failure_replay
from agent_session import SessionBrain
from agent_task_graph import TaskGraphManager
from agent_transactions import TaskTransactionManager
from agent_worker import WorkerQueue
from core_tools import AgentTool, API_KEY, PROJECT_CACHE_DIR, ROOT_DIR, ToolResult, is_workspace_path, resolve_path


def _capture_screen() -> str:
    try:
        import pyautogui
        filename = f"error_screen_{int(time.time())}.png"
        filepath = os.path.join(PROJECT_CACHE_DIR, filename)
        pyautogui.screenshot(filepath)
        return filename
    except Exception:
        return ""


def clean_assistant_output(text: str) -> str:
    cleaned = re.sub(r"<\s*\|?\s*DSML\s*\|?\s*>.*?(?:<\s*/\s*\|?\s*DSML\s*\|?\s*>|$)", "", text or "", flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<[^>]*\bDSML\b[^>]*>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"```(?:json|xml)?\s*<\s*\|?\s*DSML.*?```", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()


def _worker_context(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    lines = []
    for item in items[-5:]:
        lines.append(f"{item.get('step_id', 'step')} worker={item.get('status', 'unknown')} job={item.get('job_id', '')}")
    return "\n".join(lines)


def _tool_signature(tool_name: str, arguments: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        encoded = str(arguments or {})
    return f"{tool_name}:{encoded[:1200]}"


class SiliconFlowAdapter:
    def __init__(self, model: str = "deepseek-ai/DeepSeek-V4-Pro", thinking_level: str = "auto"):
        self.model = model
        self.thinking_level = thinking_level

    def chat_with_tools(self, messages: list[dict], tools: list[AgentTool]) -> dict:
        if not API_KEY or len(API_KEY) < 10:
            raise ValueError("SILICONFLOW_API_KEY is not configured.")

        openai_tools = [
            {"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters}}
            for tool in tools
        ]
        formatted_messages = []
        for message in messages:
            item = {"role": message["role"], "content": message.get("content", "")}
            for key in ("name", "tool_calls", "tool_call_id"):
                if key in message:
                    item[key] = message[key]
            formatted_messages.append(item)

        guardrail = (
            "Reply naturally in Traditional Chinese unless the user asks otherwise. "
            "Do not expose hidden reasoning. Use tools only when useful. "
            "Sticker replies may include [表情包: filename] or [sticker: filename] when emotionally appropriate."
        )
        if formatted_messages and formatted_messages[0]["role"] == "system":
            formatted_messages[0]["content"] = formatted_messages[0].get("content", "") + "\n\n" + guardrail
        else:
            formatted_messages.insert(0, {"role": "system", "content": guardrail})

        kwargs: dict[str, Any] = {"model": self.model, "messages": formatted_messages}
        if openai_tools:
            kwargs["tools"] = openai_tools
            kwargs["tool_choice"] = "auto"

        last_error = ""
        for attempt in range(2):
            try:
                client = OpenAI(api_key=API_KEY, base_url="https://api.siliconflow.cn/v1", http_client=httpx.Client(timeout=60.0))
                response = client.chat.completions.create(**kwargs)
                choice = response.choices[0]
                message = choice.message
                result = {"role": "assistant", "content": message.content or "", "reasoning": getattr(message, "reasoning_content", "") or ""}
                if getattr(message, "tool_calls", None):
                    result["tool_calls"] = []
                    for tool_call in message.tool_calls:
                        try:
                            args = json.loads(tool_call.function.arguments or "{}")
                        except Exception:
                            args = {}
                        result["tool_calls"].append(
                            {
                                "id": tool_call.id,
                                "name": tool_call.function.name,
                                "arguments": args,
                                "raw_arguments": tool_call.function.arguments or "{}",
                            }
                        )
                return result
            except Exception as exc:
                last_error = str(exc)
                time.sleep(1)
        return {"role": "assistant", "content": f"[LLM API error] {last_error}", "reasoning": ""}


def _stable_args(args: dict) -> str:
    return json.dumps(args or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class PendingPermission:
    tool_name: str
    arguments: dict
    created_at: float


@dataclass
class PermissionGrant:
    scope: str = "none"
    tool_name: str = ""
    arguments_key: str = ""
    remaining_uses: int = 0
    bundle_name: str = ""
    allowed_tools: set[str] = field(default_factory=set)


PERMISSION_BUNDLES: dict[str, set[str]] = {
    "computer_control_bundle": {"click_ui_element", "type_keyboard", "press_hotkey"},
    "file_workspace_bundle": {"write_file", "delete_file", "download_file"},
    "telegram_media_bundle": {"send_telegram_media"},
    "screenshot_bundle": {"get_screen_ui", "send_telegram_media", "delete_file"},
}
HIGH_RISK_TOOLS = {"execute_command", "execute_python", "execute_async_command"}

LOW_RISK_TOOLS = {
    "read_file",
    "list_files",
    "search_in_files",
    "get_screen_ui",
    "search_knowledge",
    "read_knowledge",
    "search_sticker",
    "analyze_media",
    "react_to_message",
    "update_memory",
    "update_profile",
}
SAFE_VERIFIER_COMMAND_PATTERNS = [
    r"^python\s+-m\s+py_compile\b",
    r"^python\s+self_test\.py\b",
    r"^python\s+agent_eval\.py\b",
    r"^python\s+agent_observability\.py\b",
]
SAFE_MEDIA_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tgs", ".webm", ".mp4"}
SAFE_MEDIA_DIRS = {
    os.path.join(ROOT_DIR, "workspace", "project_cache"),
    os.path.join(ROOT_DIR, "workspace", "assets"),
    os.path.join(ROOT_DIR, "workspace", "telegram_images"),
}


def _bundle_for_tool(tool_name: str) -> tuple[str, set[str]]:
    if tool_name in HIGH_RISK_TOOLS:
        return f"single_tool:{tool_name}", {tool_name}
    for bundle_name, tools in PERMISSION_BUNDLES.items():
        if tool_name in tools:
            return bundle_name, set(tools)
    return f"single_tool:{tool_name}", {tool_name}


def _is_safe_verifier_command(command: str) -> bool:
    normalized = " ".join((command or "").strip().split())
    if not normalized:
        return False
    return any(re.match(pattern, normalized, flags=re.IGNORECASE) for pattern in SAFE_VERIFIER_COMMAND_PATTERNS)


def _is_safe_python_verifier(code: str) -> bool:
    text = (code or "").strip()
    if not text or len(text) > 1200:
        return False
    lowered = text.casefold()
    if any(term in lowered for term in ["os.remove", "unlink", "rmtree", "subprocess", "requests.", "httpx.", "socket", "open("]):
        return False
    return "py_compile" in lowered or "compileall" in lowered


def _is_safe_workspace_media(path: str) -> bool:
    filepath = resolve_path(path)
    if not is_workspace_path(filepath):
        return False
    ext = os.path.splitext(filepath)[1].lower()
    if ext not in SAFE_MEDIA_EXTENSIONS:
        return False
    try:
        abs_path = os.path.abspath(filepath)
        return any(os.path.commonpath([safe_dir, abs_path]) == safe_dir for safe_dir in SAFE_MEDIA_DIRS)
    except ValueError:
        return False


class PermissionManager:
    def __init__(self, hooks: HookManager | None = None, session_id: str = "") -> None:
        self.pending: PendingPermission | None = None
        self.approved_action: PendingPermission | None = None
        self.grant = PermissionGrant()
        self.hooks = hooks or DEFAULT_HOOK_MANAGER
        self.session_id = session_id

    def record_blocked(self, tool_name: str, arguments: dict, turn_id: int = 0) -> None:
        self.pending = PendingPermission(tool_name=tool_name, arguments=arguments or {}, created_at=time.time())
        self.hooks.emit("PermissionRequest", session_id=self.session_id, turn_id=turn_id, tool=tool_name, arguments=arguments or {})

    def classify_user_reply(self, text: str, turn_id: int = 0) -> str:
        decision = classify_approval(text, has_pending=bool(self.pending))
        if decision == "deny":
            self.hooks.emit("PermissionDenied", session_id=self.session_id, turn_id=turn_id, text=text)
            self.pending = None
            self.approved_action = None
            self.grant = PermissionGrant()
            return "deny"
        if decision == "turn":
            bundle_name = "limited_turn_bundle"
            allowed_tools: set[str] = set().union(*PERMISSION_BUNDLES.values())
            if self.pending:
                bundle_name, allowed_tools = _bundle_for_tool(self.pending.tool_name)
            self.grant = PermissionGrant(scope="turn", remaining_uses=50, bundle_name=bundle_name, allowed_tools=allowed_tools)
            self.pending = None
            self.approved_action = None
            self.hooks.emit("PermissionGranted", session_id=self.session_id, turn_id=turn_id, scope="turn", bundle=bundle_name, allowed_tools=sorted(allowed_tools))
            self.hooks.emit("PermissionBundleGranted", session_id=self.session_id, turn_id=turn_id, bundle=bundle_name, allowed_tools=sorted(allowed_tools))
            return "turn"
        if decision == "single" and self.pending:
            self.approved_action = self.pending
            self.grant = PermissionGrant(
                scope="single",
                tool_name=self.pending.tool_name,
                arguments_key=_stable_args(self.pending.arguments),
                remaining_uses=1,
            )
            self.hooks.emit("PermissionGranted", session_id=self.session_id, turn_id=turn_id, scope="single", tool=self.pending.tool_name)
            self.pending = None
            return "single"
        return "none"

    def can_execute(self, tool_name: str, arguments: dict, turn_id: int = 0) -> bool:
        if self.grant.scope == "turn" and self.grant.remaining_uses > 0:
            if self.grant.allowed_tools and tool_name not in self.grant.allowed_tools:
                self.hooks.emit(
                    "PermissionBundleDenied",
                    session_id=self.session_id,
                    turn_id=turn_id,
                    scope="turn",
                    bundle=self.grant.bundle_name,
                    tool=tool_name,
                    allowed_tools=sorted(self.grant.allowed_tools),
                )
                return False
            self.grant.remaining_uses -= 1
            self.hooks.emit("PermissionConsumed", session_id=self.session_id, turn_id=turn_id, scope="turn", tool=tool_name, remaining_uses=self.grant.remaining_uses)
            self.hooks.emit("PermissionBundleConsumed", session_id=self.session_id, turn_id=turn_id, bundle=self.grant.bundle_name, tool=tool_name, remaining_uses=self.grant.remaining_uses)
            return True
        if self.grant.scope == "single" and self.grant.remaining_uses > 0:
            if tool_name == self.grant.tool_name and _stable_args(arguments) == self.grant.arguments_key:
                self.grant.remaining_uses -= 1
                self.hooks.emit("PermissionConsumed", session_id=self.session_id, turn_id=turn_id, scope="single", tool=tool_name, remaining_uses=self.grant.remaining_uses)
                return True
            self.hooks.emit("PermissionDeniedMismatch", session_id=self.session_id, turn_id=turn_id, scope="single", expected_tool=self.grant.tool_name, tool=tool_name)
            return False
        return False

    def pop_approved_action(self) -> PendingPermission | None:
        action = self.approved_action
        self.approved_action = None
        return action

    def reset_after_turn(self) -> None:
        if self.grant.scope == "turn" or self.grant.remaining_uses <= 0:
            self.grant = PermissionGrant()


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, AgentTool] = {}

    def add(self, tool: AgentTool) -> None:
        self.tools[tool.name] = tool

    def list(self) -> list[AgentTool]:
        return list(self.tools.values())

    def get(self, name: str) -> AgentTool | None:
        return self.tools.get(name)

    def names(self) -> list[str]:
        return sorted(self.tools)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, permissions: PermissionManager, interactive_mode: bool = True, hooks: HookManager | None = None, session_id: str = ""):
        self.registry = registry
        self.permissions = permissions
        self.interactive_mode = interactive_mode
        self.always_allow_tools = False
        self.hooks = hooks or DEFAULT_HOOK_MANAGER
        self.session_id = session_id
        self.turn_id = 0

    def _requires_confirm(self, tool: AgentTool, arguments: dict) -> bool:
        arguments = arguments or {}
        if tool.name in LOW_RISK_TOOLS:
            return False
        if tool.name == "send_telegram_media":
            return not _is_safe_workspace_media(str(arguments.get("file_path") or ""))
        if tool.name == "execute_command" and _is_safe_verifier_command(str(arguments.get("command") or "")):
            return False
        if tool.name == "execute_python" and _is_safe_python_verifier(str(arguments.get("code") or "")):
            return False
        cross_boundary = any(
            not is_workspace_path(resolve_path(value))
            for key, value in arguments.items()
            if key in {"filename", "directory", "file_path"} and isinstance(value, str)
        )
        return bool(tool.requires_confirm or cross_boundary)

    def execute(self, tool_name: str, arguments: dict, callback: Callable | None = None, policy: ResponsePolicy | None = None) -> ToolResult:
        tool = self.registry.get(tool_name)
        if not tool:
            return ToolResult("error", f"Unknown tool: {tool_name}. Available tools: {', '.join(self.registry.names())}")

        arguments = arguments if isinstance(arguments, dict) else {}
        if policy and policy.allowed_tools is not None and tool_name not in policy.allowed_tools:
            self.hooks.emit("ToolSkippedByPolicy", session_id=self.session_id, turn_id=self.turn_id, tool=tool_name, arguments=arguments, reason="tool_not_allowed_for_route", route=policy.route)
            return ToolResult(
                "blocked",
                f"這一步我先停住：`{tool_name}` 不適合在現在這種回覆節奏裡直接跑。你如果是要我繼續剛剛的任務，直接說「繼續」或「可以」就好。",
                requires_permission=False,
                data={"route": policy.route, "tool": tool_name},
            )
        if policy and not policy.allow_vision and tool_name == "analyze_media":
            self.hooks.emit("ToolSkippedByPolicy", session_id=self.session_id, turn_id=self.turn_id, tool=tool_name, arguments=arguments, reason="vision_disabled")
            return ToolResult("blocked", "analyze_media skipped by response policy.", requires_permission=False)
        requires_confirm = self._requires_confirm(tool, arguments)
        pre_decision = self.hooks.emit("PreToolUse", session_id=self.session_id, turn_id=self.turn_id, tool=tool_name, arguments=arguments, requires_confirm=requires_confirm)
        if pre_decision.replace_args is not None:
            arguments = pre_decision.replace_args
        if pre_decision.block:
            return ToolResult("blocked", pre_decision.message or f"Tool {tool_name} blocked by hook.", requires_permission=requires_confirm)

        if requires_confirm and not self.always_allow_tools and not self.permissions.can_execute(tool_name, arguments, self.turn_id):
            if self.interactive_mode:
                answer = input(f"Allow tool {tool_name}? (y/n/a): ").strip().casefold()
                if answer == "a":
                    self.always_allow_tools = True
                elif answer != "y":
                    return ToolResult("blocked", f"User rejected {tool_name}.", requires_permission=True)
            else:
                self.permissions.record_blocked(tool_name, arguments, self.turn_id)
                self.hooks.emit("tool.blocked", session_id=self.session_id, turn_id=self.turn_id, tool=tool_name, arguments=arguments)
                return ToolResult(
                    "blocked",
                    f"Tool {tool_name} requires approval. Ask the owner if this exact action is allowed.",
                    data={"tool": tool_name, "arguments": arguments},
                    requires_permission=True,
                )

        if callback:
            try:
                callback(tool_name, arguments, "start")
            except Exception:
                pass
        self.hooks.emit("tool.start", session_id=self.session_id, turn_id=self.turn_id, tool=tool_name, arguments=arguments, requires_confirm=requires_confirm)
        try:
            raw = tool.func(**arguments)
            result = raw if isinstance(raw, ToolResult) else ToolResult("ok", str(raw))
        except TypeError as exc:
            result = ToolResult("error", "Tool arguments are invalid.", error=str(exc))
        except Exception as exc:
            result = ToolResult("error", "Tool raised an exception.", error=str(exc))
        self.hooks.emit("tool.end", session_id=self.session_id, turn_id=self.turn_id, tool=tool_name, result=result.to_text(), status=result.status, message=result.message, error=result.error)
        self.hooks.emit("PostToolUse", session_id=self.session_id, turn_id=self.turn_id, tool=tool_name, result=result.to_text(), status=result.status)
        if result.status == "error":
            self.hooks.emit("ToolError", session_id=self.session_id, turn_id=self.turn_id, tool=tool_name, result=result.to_text(), error=result.error)
        if callback:
            try:
                callback(tool_name, arguments, "end", result)
            except TypeError:
                try:
                    callback(tool_name, arguments, "end")
                except Exception:
                    pass
            except Exception:
                pass
        return result


class CompanionAgent:
    def __init__(self, llm_adapter: SiliconFlowAdapter, system_prompt: str, current_history_file: str):
        self.llm = llm_adapter
        self.session_id = os.path.splitext(os.path.basename(current_history_file))[0] or str(int(time.time()))
        self.turn_id = 0
        self.hooks = DEFAULT_HOOK_MANAGER
        self.registry = ToolRegistry()
        self.permission_manager = PermissionManager(self.hooks, self.session_id)
        self.session_brain = SessionBrain()
        self.transactions = TaskTransactionManager()
        self.task_graphs = TaskGraphManager()
        self.worker_queue = WorkerQueue()
        self._tool_failure_counts: dict[str, int] = {}
        self.memory: list[dict] = [{"role": "system", "content": system_prompt}]
        self.history_file = current_history_file
        self.interactive_mode = True
        self.executor = ToolExecutor(self.registry, self.permission_manager, self.interactive_mode, self.hooks, self.session_id)
        self.hooks.emit("SessionStart", session_id=self.session_id, turn_id=0, history_file=current_history_file)

    @property
    def tools(self) -> dict[str, AgentTool]:
        return self.registry.tools

    @property
    def always_allow_tools(self) -> bool:
        return self.executor.always_allow_tools

    @always_allow_tools.setter
    def always_allow_tools(self, value: bool) -> None:
        self.executor.always_allow_tools = bool(value)

    def add_tool(self, tool: AgentTool) -> None:
        self.registry.add(tool)

    def _save_history(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
            with open(self.history_file, "w", encoding="utf-8") as file:
                json.dump(self.memory, file, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _remember_turn_summary(self, user_input: str, assistant_reply: str) -> None:
        try:
            from agent_memory import update_chat_summary

            clean_user = re.sub(r"\s+", " ", (user_input or "").split("[SessionBrain]")[0]).strip()
            clean_reply = re.sub(r"\s+", " ", assistant_reply or "").strip()
            if clean_user and clean_reply:
                update_chat_summary(f"Owner: {clean_user[:180]} | YueYue: {clean_reply[:180]}")
        except Exception:
            pass

    def _after_tool_result(self, tool_name: str, arguments: dict, result: ToolResult) -> tuple[ActionVerificationResult, dict[str, Any] | None]:
        verification = verify_action(tool_name, arguments, result, self.session_id, self.turn_id)
        self.transactions.record_tool_result(tool_name, arguments, result, verification, self.session_id, self.turn_id)
        self.task_graphs.record_tool_result(tool_name, arguments, result, verification, self.session_id, self.turn_id)
        self.session_brain.mark_tool_result(tool_name, result.status, self.turn_id, self.session_id)
        if verification.status == "fail":
            self.session_brain.mark_verification_result("fail", [verification.message], self.turn_id, self.session_id)
        elif verification.status == "observe_needed":
            self.session_brain.mark_validation_needed(
                "observe and verify UI action: " + tool_name,
                self.turn_id,
                self.session_id,
                evidence=[tool_name, verification.message],
            )

        replay_case = None
        if result.status == "ok":
            self._tool_failure_counts[tool_name] = 0
        elif result.status != "blocked" or not result.requires_permission:
            self._tool_failure_counts[tool_name] = self._tool_failure_counts.get(tool_name, 0) + 1
            if self._tool_failure_counts[tool_name] >= 3:
                replay_case = record_failure_replay(
                    tool_name,
                    arguments,
                    result,
                    session_id=self.session_id,
                    turn_id=self.turn_id,
                    count=self._tool_failure_counts[tool_name],
                )
                self.transactions.mark_blocked(f"{tool_name} failed repeatedly", self.session_id, self.turn_id)
                self.task_graphs.mark_blocked(
                    f"{tool_name} failed repeatedly",
                    self.session_id,
                    self.turn_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    result=result,
                )
        return verification, replay_case

    def _assimilate_worker_evidence(self) -> list[dict[str, Any]]:
        try:
            assimilated = self.task_graphs.assimilate_worker_results(self.worker_queue.list_results(limit=100), self.session_id, self.turn_id)
            if assimilated:
                ok = all(item.get("status") == "done" for item in assimilated)
                evidence = [f"{item.get('step_id')}:{item.get('status')}" for item in assimilated[-5:]]
                self.session_brain.mark_verification_result("ok" if ok else "error", evidence, self.turn_id, self.session_id)
            return assimilated
        except Exception as exc:
            self.hooks.emit("worker.assimilation_failed", session_id=self.session_id, turn_id=self.turn_id, error=str(exc))
            return []

    def _plan_turn_if_needed(self, user_input: str, turn_classification: Any) -> None:
        if getattr(turn_classification, "is_chat", False):
            return
        plan = DEFAULT_PLANNER.plan(user_input, intent=getattr(turn_classification, "intent", "task"), session_id=self.session_id, turn_id=self.turn_id)
        if plan.intent == "cancel":
            self.task_graphs.cancel_active("owner_cancelled", self.session_id, self.turn_id)
            return
        self.task_graphs.plan_steps(user_input, plan.step_names(), session_id=self.session_id, turn_id=self.turn_id, planner_version=plan.planner_version)

    def _recover_tool_result(self, tool_name: str, arguments: dict, result: ToolResult, tool_callback: Callable | None, response_policy: ResponsePolicy | None) -> tuple[ToolResult, dict[str, Any] | None]:
        if tool_name != "execute_command" or result.status != "error":
            return result, None
        data = result.data if isinstance(result.data, dict) else {}
        retry_hint = str(data.get("retry_hint") or "")
        original_cwd = str((arguments or {}).get("cwd") or data.get("cwd") or "project")
        if not retry_hint or original_cwd == "project":
            return result, None
        retry_args = dict(arguments or {})
        retry_args["cwd"] = "project"
        self.hooks.emit(
            "ToolRecoveryAttempt",
            session_id=self.session_id,
            turn_id=self.turn_id,
            tool=tool_name,
            reason="cwd_retry",
            original_cwd=original_cwd,
            retry_cwd="project",
            retry_hint=retry_hint,
        )
        recovered = self.executor.execute(tool_name, retry_args, tool_callback, response_policy)
        recovery = {
            "reason": "cwd_retry",
            "original_status": result.status,
            "original_message": result.message,
            "original_error": result.error,
            "original_cwd": original_cwd,
            "retry_cwd": "project",
            "retry_status": recovered.status,
        }
        if isinstance(recovered.data, dict):
            recovered.data["recovered_from"] = recovery
        self.hooks.emit("ToolRecoveryResult", session_id=self.session_id, turn_id=self.turn_id, tool=tool_name, **recovery)
        if recovered.status == "ok":
            return recovered, recovery
        return result, recovery

    def chat(self, user_input: str, tool_callback: Callable | None = None, response_policy: ResponsePolicy | None = None) -> dict[str, str]:
        response_policy = response_policy or ResponsePolicy()
        self.executor.interactive_mode = self.interactive_mode
        self.turn_id += 1
        self.executor.turn_id = self.turn_id
        assimilated_worker_results = self._assimilate_worker_evidence()
        pending_before = bool(self.permission_manager.pending)
        grant = self.permission_manager.classify_user_reply(user_input, self.turn_id)
        turn_classification = self.session_brain.classify_turn(user_input, grant=grant, pending_permission=pending_before, turn_id=self.turn_id, session_id=self.session_id)
        if not turn_classification.is_chat:
            self.transactions.start_or_resume(user_input, self.session_id, self.turn_id)
            self._plan_turn_if_needed(user_input, turn_classification)
        self.hooks.emit("UserMessage", session_id=self.session_id, turn_id=self.turn_id, grant=grant, interactive_mode=self.interactive_mode, pending=bool(self.permission_manager.pending))
        if grant == "single":
            approved_action = self.permission_manager.pop_approved_action()
            if approved_action:
                user_input += f"\n\n[SessionBrain]\n{self.session_brain.summary()}\n\n[TaskGraph]\n{self.task_graphs.summary()}\nturn_intent: {turn_classification.intent}"
                worker_context = _worker_context(assimilated_worker_results)
                if worker_context:
                    user_input += f"\n\n[WorkerEvidence]\n{worker_context}"
                self.memory.append({"role": "user", "content": user_input})
                self.hooks.emit(
                    "PermissionReplay",
                    session_id=self.session_id,
                    turn_id=self.turn_id,
                    tool=approved_action.tool_name,
                    arguments=approved_action.arguments,
                )
                result = self.executor.execute(approved_action.tool_name, approved_action.arguments, tool_callback, None)
                verification, replay_case = self._after_tool_result(approved_action.tool_name, approved_action.arguments, result)
                self.hooks.emit(
                    "PermissionReplayResult",
                    session_id=self.session_id,
                    turn_id=self.turn_id,
                    tool=approved_action.tool_name,
                    status=result.status,
                    verification_status=verification.status,
                )
                if result.status == "ok":
                    self.session_brain.mark_validation_needed(
                        "verify tool results: " + approved_action.tool_name,
                        self.turn_id,
                        self.session_id,
                        evidence=[approved_action.tool_name],
                    )
                    final_reply = f"已執行剛剛批准的 `{approved_action.tool_name}`：{result.message}"
                elif replay_case:
                    final_reply = f"主人，我發現 `{approved_action.tool_name}` 重複卡住，所以先停下來了。Replay case: {replay_case.get('name')}"
                elif result.status == "blocked":
                    if result.requires_permission:
                        self.session_brain.mark_permission_needed(approved_action.tool_name, self.turn_id, self.session_id)
                    final_reply = f"`{approved_action.tool_name}` 仍被攔截：{result.message}"
                else:
                    final_reply = f"`{approved_action.tool_name}` 執行失敗：{result.message}"
                    if result.error:
                        final_reply += f"\n{result.error[:800]}"
                reply_decision = self.hooks.emit("BeforeReply", session_id=self.session_id, turn_id=self.turn_id, content_preview=final_reply[:160])
                if reply_decision.annotate:
                    final_reply += reply_decision.annotate
                self.memory.append({"role": "assistant", "content": final_reply})
                self._remember_turn_summary(user_input, final_reply)
                self.permission_manager.reset_after_turn()
                self.always_allow_tools = False
                self.hooks.emit("Stop", session_id=self.session_id, turn_id=self.turn_id, content_preview=final_reply[:160])
                self._save_history()
                return {"content": final_reply, "reasoning": ""}
        if grant == "single":
            user_input += "\n\n[System notice: owner approved the previously blocked exact tool call once. Retry that same tool call if it is still needed.]"
        elif grant == "turn":
            user_input += "\n\n[System notice: owner approved tool use for this task turn. Use only necessary tools and report results.]"
        elif grant == "deny":
            user_input += "\n\n[System notice: owner rejected the pending tool action. Do not retry it.]"
        user_input += f"\n\n[SessionBrain]\n{self.session_brain.summary()}\n\n[TaskGraph]\n{self.task_graphs.summary()}\nturn_intent: {turn_classification.intent}"
        worker_context = _worker_context(assimilated_worker_results)
        if worker_context:
            user_input += f"\n\n[WorkerEvidence]\n{worker_context}"

        self.memory.append({"role": "user", "content": user_input})
        total_reasoning = ""
        max_iterations = response_policy.max_tool_iterations
        successful_tools: list[str] = []
        tool_call_counts: dict[str, int] = {}

        for _ in range(max_iterations):
            response = self.llm.chat_with_tools(self.memory, self.registry.list())
            self.hooks.emit("llm.response", session_id=self.session_id, turn_id=self.turn_id, has_tool_calls=bool(response.get("tool_calls")), content_preview=(response.get("content") or "")[:160])
            if response.get("reasoning"):
                total_reasoning += response["reasoning"] + "\n\n"

            tool_calls = response.get("tool_calls") or []
            if tool_calls:
                self.memory.append(
                    {
                        "role": "assistant",
                        "content": response.get("content") or "",
                        "tool_calls": [
                            {
                                "id": call["id"],
                                "type": "function",
                                "function": {"name": call["name"], "arguments": call.get("raw_arguments", json.dumps(call.get("arguments", {}), ensure_ascii=False))},
                            }
                            for call in tool_calls
                        ],
                    }
                )
                for call in tool_calls:
                    signature = _tool_signature(call["name"], call.get("arguments", {}))
                    tool_call_counts[signature] = tool_call_counts.get(signature, 0) + 1
                    if tool_call_counts[signature] > max(1, response_policy.max_repeated_tool_calls):
                        repeated_result = ToolResult("error", f"Repeated tool call stopped by {response_policy.route} loop controller.", error="repeated_tool_call")
                        replay_case = record_failure_replay(
                            call["name"],
                            call.get("arguments", {}),
                            repeated_result,
                            session_id=self.session_id,
                            turn_id=self.turn_id,
                            count=tool_call_counts[signature],
                        )
                        self.task_graphs.mark_blocked(
                            "repeated tool call stopped",
                            self.session_id,
                            self.turn_id,
                            tool_name=call["name"],
                            arguments=call.get("arguments", {}),
                            result=repeated_result,
                        )
                        final_reply = f"主人，我發現 `{call['name']}` 在同一輪重複卡住，所以先停下來了。Replay case: {replay_case.get('name')}"
                        self.memory.append({"role": "assistant", "content": final_reply})
                        self.permission_manager.reset_after_turn()
                        self.always_allow_tools = False
                        self.hooks.emit("StopFailure", session_id=self.session_id, turn_id=self.turn_id, tool=call["name"], replay_case=replay_case.get("name"), reason="repeated_tool_call")
                        self._remember_turn_summary(user_input, final_reply)
                        self._save_history()
                        return {"content": final_reply, "reasoning": total_reasoning.strip()}
                    result = self.executor.execute(call["name"], call.get("arguments", {}), tool_callback, response_policy)
                    result, recovery = self._recover_tool_result(call["name"], call.get("arguments", {}), result, tool_callback, response_policy)
                    verification, replay_case = self._after_tool_result(call["name"], call.get("arguments", {}), result)
                    result_text = result.to_text()[:4000]
                    if "fail-safe" in result_text.casefold() or "failsafe" in result_text.casefold():
                        screen = _capture_screen()
                        tag = f" [系統截圖: {screen}]" if screen else ""
                        result_text = ToolResult("error", f"Fail-safe triggered. Stop all actions immediately.{tag}").to_text()
                        self.always_allow_tools = False
                        self.memory.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": result_text})
                        final_reply = f"主人，我遇到 fail-safe，已立刻停止所有操作。{tag}"
                        self.memory.append({"role": "assistant", "content": final_reply})
                        self._remember_turn_summary(user_input, final_reply)
                        self._save_history()
                        return {"content": final_reply, "reasoning": total_reasoning.strip()}
                    self.memory.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": result_text})
                    if replay_case:
                        final_reply = (
                            f"`{call['name']}` failed repeatedly, so I stopped this loop and saved a replay case: "
                            f"{replay_case.get('name')}. Trace: {TRACE_LOG_FILE}"
                        )
                        self.memory.append({"role": "assistant", "content": final_reply})
                        self.permission_manager.reset_after_turn()
                        self.always_allow_tools = False
                        self.hooks.emit("StopFailure", session_id=self.session_id, turn_id=self.turn_id, tool=call["name"], replay_case=replay_case.get("name"))
                        self._remember_turn_summary(user_input, final_reply)
                        self._save_history()
                        return {"content": final_reply, "reasoning": total_reasoning.strip()}
                    if result.status == "blocked":
                        if result.requires_permission:
                            self.session_brain.mark_permission_needed(call["name"], self.turn_id, self.session_id)
                        else:
                            final_reply = f"這一步我先停住：`{call['name']}` 不適合放在現在這個聊天路線裡直接跑。{result.message}"
                            self.memory.append({"role": "assistant", "content": final_reply})
                            self.permission_manager.reset_after_turn()
                            self.always_allow_tools = False
                            self.hooks.emit("StopFailure", session_id=self.session_id, turn_id=self.turn_id, tool=call["name"], reason="route_policy_block")
                            self._remember_turn_summary(user_input, final_reply)
                            self._save_history()
                            return {"content": final_reply, "reasoning": total_reasoning.strip()}
                        break
                    if result.status == "ok":
                        successful_tools.append(call["name"])
                continue

            final_reply = clean_assistant_output(response.get("content", ""))
            if not final_reply:
                final_reply = "主人，我已經處理完了。"
            reply_decision = self.hooks.emit("BeforeReply", session_id=self.session_id, turn_id=self.turn_id, content_preview=final_reply[:160])
            if reply_decision.annotate:
                final_reply += reply_decision.annotate
            self.memory.append({"role": "assistant", "content": final_reply})
            self._remember_turn_summary(user_input, final_reply)
            self.hooks.emit("Stop", session_id=self.session_id, turn_id=self.turn_id, content_preview=final_reply[:160])
            if successful_tools:
                self.session_brain.mark_validation_needed(
                    "verify tool results: " + ", ".join(successful_tools[-5:]),
                    self.turn_id,
                    self.session_id,
                    evidence=successful_tools[-5:],
                )
            self.permission_manager.reset_after_turn()
            self.always_allow_tools = False
            self._save_history()
            if successful_tools:
                self.task_graphs.mark_completed(self.session_id, self.turn_id)
            return {"content": final_reply, "reasoning": total_reasoning.strip()}

        self.always_allow_tools = False
        timeout_msg = "主人，我卡在工具迴圈裡了，已停止本輪操作。這次不再繼續重試，避免把同一個工具刷屏。"
        self.memory.append({"role": "assistant", "content": timeout_msg})
        self._remember_turn_summary(user_input, timeout_msg)
        self._save_history()
        return {"content": timeout_msg, "reasoning": total_reasoning.strip()}

