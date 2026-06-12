import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from agent_hooks import DEFAULT_HOOK_MANAGER, HookManager
from agent_latency import ResponsePolicy
from agent_protocol import classify_approval
from agent_user_voice import friendly_tool_block, permission_request_reply
from core_tools import AgentTool, ROOT_DIR, ToolResult, is_workspace_path, resolve_path


def stable_args(args: dict) -> str:
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


def bundle_for_tool(tool_name: str) -> tuple[str, set[str]]:
    if tool_name in HIGH_RISK_TOOLS:
        return f"single_tool:{tool_name}", {tool_name}
    for bundle_name, tools in PERMISSION_BUNDLES.items():
        if tool_name in tools:
            return bundle_name, set(tools)
    return f"single_tool:{tool_name}", {tool_name}


def is_safe_verifier_command(command: str) -> bool:
    normalized = " ".join((command or "").strip().split())
    if not normalized:
        return False
    return any(re.match(pattern, normalized, flags=re.IGNORECASE) for pattern in SAFE_VERIFIER_COMMAND_PATTERNS)


def is_safe_python_verifier(code: str) -> bool:
    text = (code or "").strip()
    if not text or len(text) > 1200:
        return False
    lowered = text.casefold()
    if any(term in lowered for term in ["os.remove", "unlink", "rmtree", "subprocess", "requests.", "httpx.", "socket", "open("]):
        return False
    return "py_compile" in lowered or "compileall" in lowered


def is_safe_workspace_media(path: str) -> bool:
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
                bundle_name, allowed_tools = bundle_for_tool(self.pending.tool_name)
            self.grant = PermissionGrant(scope="turn", remaining_uses=50, bundle_name=bundle_name, allowed_tools=allowed_tools)
            self.pending = None
            self.approved_action = None
            self.hooks.emit("PermissionGranted", session_id=self.session_id, turn_id=turn_id, scope="turn", bundle=bundle_name, allowed_tools=sorted(allowed_tools))
            self.hooks.emit("PermissionBundleGranted", session_id=self.session_id, turn_id=turn_id, bundle=bundle_name, allowed_tools=sorted(allowed_tools))
            return "turn"
        if decision == "single" and self.pending:
            self.approved_action = self.pending
            self.grant = PermissionGrant(scope="single", tool_name=self.pending.tool_name, arguments_key=stable_args(self.pending.arguments), remaining_uses=1)
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
            if tool_name == self.grant.tool_name and stable_args(arguments) == self.grant.arguments_key:
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
            return not is_safe_workspace_media(str(arguments.get("file_path") or ""))
        if tool.name == "execute_command" and is_safe_verifier_command(str(arguments.get("command") or "")):
            return False
        if tool.name == "execute_python" and is_safe_python_verifier(str(arguments.get("code") or "")):
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
                friendly_tool_block(tool_name, route=policy.route),
                requires_permission=False,
                data={"route": policy.route, "tool": tool_name, "retry_hint": "你可以說「繼續」接回原任務，或直接補一句新的任務目標。"},
            )
        if policy and not policy.allow_vision and tool_name == "analyze_media":
            self.hooks.emit("ToolSkippedByPolicy", session_id=self.session_id, turn_id=self.turn_id, tool=tool_name, arguments=arguments, reason="vision_disabled")
            return ToolResult("blocked", friendly_tool_block(tool_name, route=policy.route), requires_permission=False, data={"route": policy.route, "tool": tool_name})
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
                    permission_request_reply(tool_name, arguments),
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
