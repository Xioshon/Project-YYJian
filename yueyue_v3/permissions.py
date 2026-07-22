from __future__ import annotations

import time

from agent_protocol import classify_approval

from .models import ActionEnvelope, PermissionState

COMPUTER_TOOLS = {"focus_window", "click_screen", "click_ui_element", "type_keyboard", "press_hotkey"}
FILE_TOOLS = {"write_file", "delete_file", "download_file"}
HIGH_RISK_TOOLS = {"execute_command", "execute_python", "execute_async_command", "delete_file", "download_file"}
# Code-execution tools are ONE capability class: "run code I wrote to reach this goal". Approving
# any of them for a task covers the others, because cmd-vs-python is an implementation detail that
# grants no capability the first 可以 didn't already imply. delete_file / download_file stay
# per-tool (a declared destructive / network action the owner should see and approve explicitly).
CODE_EXEC_TOOLS = {"execute_command", "execute_python", "execute_async_command"}
CODE_EXEC_BUNDLE = "code_exec_task"
FREE_TOOLS = {
    "read_file",
    "list_files",
    "search_in_files",
    "get_screen_ui",
    "capture_screen",
    "list_windows",
    "search_sticker",
    "analyze_media",
    "inspect_url",
    "read_url_context",
    "reindex_url_cache",
    "react_to_message",
    "update_memory",
    "update_profile",
}


class PermissionController:
    def needs_permission(self, tool_name: str, arguments: dict) -> bool:
        if tool_name in FREE_TOOLS:
            return False
        return (
            tool_name in COMPUTER_TOOLS
            or tool_name in FILE_TOOLS
            or tool_name in HIGH_RISK_TOOLS
            or tool_name == "send_telegram_media"
        )

    def request(self, state: PermissionState, action: ActionEnvelope) -> PermissionState:
        state.pending_action = action
        state.scope = "pending"
        state.allowed_tools = []
        state.bundle = self.bundle_for(action.tool_name)
        return state

    def apply_reply(self, state: PermissionState, text: str) -> str:
        decision = classify_approval(text, has_pending=bool(state.pending_action))
        if decision == "deny":
            state.scope = "none"
            state.pending_action = None
            state.allowed_tools = []
            state.bundle = ""
            return "deny"
        if decision in {"single", "turn"} and state.pending_action:
            pending = state.pending_action
            bundle = self.bundle_for(pending.tool_name)
            # file_workspace_bundle joins computer_control_bundle as a workflow-scoped grant: one
            # 可以 covers the remaining workspace file writes of the SAME workflow. Gap-battery
            # evidence 2026-07-15: a create-append-verify task burned 8 approvals, which no owner
            # experiences as one task. The blast radius stays bounded - these tools are already
            # hard-restricted to the workspace at the tool layer, and HIGH_RISK tools
            # (execute_command/execute_python/delete/download) keep per-call approval via the
            # single:<tool> bundle check in permits().
            if decision == "turn" or bundle in {"computer_control_bundle", "file_workspace_bundle"}:
                state.scope = "workflow"
                if bundle == "computer_control_bundle":
                    state.allowed_tools = sorted(COMPUTER_TOOLS)
                elif bundle == "file_workspace_bundle":
                    state.allowed_tools = sorted(FILE_TOOLS - HIGH_RISK_TOOLS)
                else:
                    state.allowed_tools = [pending.tool_name]
                state.expires_at = time.time() + 3600
            else:
                # HIGH_RISK: the approval covers the CURRENT task, not one single call. Live
                # 2026-07-20: creating one file asked 可以 three times; 2026-07-23: a list-files
                # task approved execute_command then re-asked when it switched to execute_python.
                # A code-exec approval now covers the whole code-exec class for this task (same
                # capability the owner already granted); delete/download stay single-tool. Bounded
                # to this workflow with a 10-minute expiry either way.
                state.scope = "task"
                if pending.tool_name in CODE_EXEC_TOOLS:
                    state.allowed_tools = sorted(CODE_EXEC_TOOLS)
                    bundle = CODE_EXEC_BUNDLE
                else:
                    state.allowed_tools = [pending.tool_name]
                state.expires_at = time.time() + 600
            state.granted_at = time.time()
            state.bundle = bundle
            state.pending_action = None
            return "turn" if state.scope == "workflow" else "single"
        return "none"

    def permits(self, state: PermissionState, action: ActionEnvelope) -> bool:
        if not self.needs_permission(action.tool_name, action.arguments):
            return True
        if state.expires_at and time.time() > state.expires_at:
            self.clear(state)
            return False
        if action.tool_name not in state.allowed_tools:
            return False
        if action.tool_name in CODE_EXEC_TOOLS:
            # Any code-exec tool is covered by a code-exec grant, or by its own single grant.
            if state.bundle not in {CODE_EXEC_BUNDLE, f"single:{action.tool_name}"}:
                return False
        elif action.tool_name in HIGH_RISK_TOOLS and state.bundle != f"single:{action.tool_name}":
            return False
        # scope "task" keeps the grant alive for further calls of the SAME high-risk tool within
        # this workflow (cleared when the workflow ends or the 10-minute expiry passes); "single"
        # is consumed on first use.
        if state.scope == "single":
            self.clear(state)
        return True

    def clear(self, state: PermissionState) -> None:
        state.scope = "none"
        state.bundle = ""
        state.allowed_tools = []
        state.pending_action = None
        state.granted_at = 0.0
        state.expires_at = 0.0

    def bundle_for(self, tool_name: str) -> str:
        if tool_name in COMPUTER_TOOLS:
            return "computer_control_bundle"
        if tool_name in HIGH_RISK_TOOLS:
            return f"single:{tool_name}"
        if tool_name in FILE_TOOLS:
            return "file_workspace_bundle"
        if tool_name == "send_telegram_media":
            return "telegram_media_bundle"
        return f"single:{tool_name}"
