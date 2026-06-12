from dataclasses import dataclass
from typing import Any, Callable

from agent_outcome import tool_result_outcome
from core_tools import ToolResult


@dataclass
class PermissionReplayHandled:
    content: str
    reasoning: str = ""

    def to_chat_result(self) -> dict[str, str]:
        return {"content": self.content, "reasoning": self.reasoning}


class PermissionReplayController:
    """Runs an approved pending action exactly once, without LLM replanning."""

    def __init__(
        self,
        *,
        permission_manager: Any,
        session_brain: Any,
        executor: Any,
        hooks: Any,
        after_tool_result: Callable[[str, dict, ToolResult], tuple[Any, dict[str, Any] | None]],
        append_user_context: Callable[[str], str],
        append_assistant_reply: Callable[[str], None],
        reset_turn_state: Callable[[], None],
        session_id: str,
        turn_id_getter: Callable[[], int],
    ):
        self.permission_manager = permission_manager
        self.session_brain = session_brain
        self.executor = executor
        self.hooks = hooks
        self.after_tool_result = after_tool_result
        self.append_user_context = append_user_context
        self.append_assistant_reply = append_assistant_reply
        self.reset_turn_state = reset_turn_state
        self.session_id = session_id
        self.turn_id_getter = turn_id_getter

    def maybe_replay(self, grant: str, user_input: str, tool_callback: Callable | None) -> PermissionReplayHandled | None:
        if grant != "single":
            return None
        approved_action = self.permission_manager.pop_approved_action()
        if not approved_action:
            return None

        turn_id = self.turn_id_getter()
        self.append_user_context(user_input)
        self.hooks.emit(
            "PermissionReplay",
            session_id=self.session_id,
            turn_id=turn_id,
            tool=approved_action.tool_name,
            arguments=approved_action.arguments,
        )
        result = self.executor.execute(approved_action.tool_name, approved_action.arguments, tool_callback, None)
        verification, replay_case = self.after_tool_result(approved_action.tool_name, approved_action.arguments, result)
        self.hooks.emit(
            "PermissionReplayResult",
            session_id=self.session_id,
            turn_id=turn_id,
            tool=approved_action.tool_name,
            status=result.status,
            verification_status=getattr(verification, "status", ""),
        )
        final_reply = self._format_replay_reply(approved_action, result, replay_case)
        reply_decision = self.hooks.emit("BeforeReply", session_id=self.session_id, turn_id=turn_id, content_preview=final_reply[:160])
        if reply_decision.annotate:
            final_reply += reply_decision.annotate
        self.append_assistant_reply(final_reply)
        self.reset_turn_state()
        self.hooks.emit("Stop", session_id=self.session_id, turn_id=turn_id, content_preview=final_reply[:160])
        return PermissionReplayHandled(final_reply)

    def _format_replay_reply(self, approved_action: Any, result: ToolResult, replay_case: dict[str, Any] | None) -> str:
        tool_name = approved_action.tool_name
        if result.status == "ok":
            self.session_brain.mark_validation_needed(
                "verify tool results: " + tool_name,
                self.turn_id_getter(),
                self.session_id,
                evidence=[tool_name],
            )
            outcome_summary, artifacts = tool_result_outcome(tool_name, result)
            final_reply = f"已執行剛剛批准的 `{tool_name}`：{result.message}"
            if outcome_summary:
                final_reply += "\n" + outcome_summary
            if artifacts:
                final_reply += "\n如果你要我發送或分析這些產物，直接說「發給我」或「分析一下」就好。"
            return final_reply
        if replay_case:
            return f"主人，我發現 `{tool_name}` 重複卡住，所以先停下來了。Replay case: {replay_case.get('name')}"
        if result.status == "blocked":
            if result.requires_permission:
                self.session_brain.mark_permission_needed(tool_name, self.turn_id_getter(), self.session_id)
            return f"`{tool_name}` 仍被攔截：{result.message}"
        final_reply = f"`{tool_name}` 執行失敗：{result.message}"
        if result.error:
            final_reply += f"\n{result.error[:800]}"
        return final_reply
