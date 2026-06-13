import json
from dataclasses import dataclass
from typing import Any, Callable

from agent_outcome import tool_result_outcome
from agent_self_recovery import self_repair_instruction, should_prompt_self_repair
from core_tools import ToolResult


@dataclass
class PermissionReplayHandled:
    content: str
    reasoning: str = ""

    def to_chat_result(self) -> dict[str, str]:
        return {"content": self.content, "reasoning": self.reasoning}


class PermissionReplayController:
    """Runs an approved pending action exactly once, then repairs bounded failures."""

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
        memory: list[dict[str, Any]] | None = None,
        continue_after_error: Callable[[Callable | None], Any] | None = None,
        response_policy: Any = None,
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
        self.memory = memory
        self.continue_after_error = continue_after_error
        self.response_policy = response_policy
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
        repaired = self._maybe_continue_after_error(approved_action, result, replay_case, tool_callback)
        if repaired is not None:
            return repaired
        final_reply = self._format_replay_reply(approved_action, result, replay_case)
        reply_decision = self.hooks.emit("BeforeReply", session_id=self.session_id, turn_id=turn_id, content_preview=final_reply[:160])
        if reply_decision.annotate:
            final_reply += reply_decision.annotate
        self.append_assistant_reply(final_reply)
        self.reset_turn_state()
        self.hooks.emit("Stop", session_id=self.session_id, turn_id=turn_id, content_preview=final_reply[:160])
        return PermissionReplayHandled(final_reply)

    def _maybe_continue_after_error(self, approved_action: Any, result: ToolResult, replay_case: dict[str, Any] | None, tool_callback: Callable | None) -> PermissionReplayHandled | None:
        if replay_case or result.status != "error" or not self.memory or not self.continue_after_error:
            return None
        if not should_prompt_self_repair(approved_action.tool_name, result, self.response_policy):
            return None
        turn_id = self.turn_id_getter()
        tool_call_id = f"permission_replay_{turn_id}_{approved_action.tool_name}"
        self.memory.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": approved_action.tool_name,
                            "arguments": json.dumps(approved_action.arguments or {}, ensure_ascii=False),
                        },
                    }
                ],
            }
        )
        self.memory.append({"role": "tool", "tool_call_id": tool_call_id, "name": approved_action.tool_name, "content": result.to_text()[:4000]})
        self.memory.append({"role": "system", "content": self_repair_instruction(approved_action.tool_name, approved_action.arguments or {}, result)})
        if hasattr(self.permission_manager, "grant_repair_tool"):
            self.permission_manager.grant_repair_tool(approved_action.tool_name, turn_id)
        self.hooks.emit("PermissionReplaySelfRepair", session_id=self.session_id, turn_id=turn_id, tool=approved_action.tool_name)
        continued = self.continue_after_error(tool_callback)
        return PermissionReplayHandled(getattr(continued, "content", ""), getattr(continued, "reasoning", ""))

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
            final_reply = f"我已經跑了你剛剛確認的 `{tool_name}`：{result.message}"
            if outcome_summary:
                final_reply += "\n" + outcome_summary
            if artifacts:
                final_reply += "\n如果你要我發送或分析這些產物，直接說「發給我」或「分析一下」就好。"
            return final_reply
        if replay_case:
            return f"主人，我發現 `{tool_name}` 重複卡住，所以先停下來了。replay case: {replay_case.get('name')}"
        if result.status == "blocked":
            if result.requires_permission:
                self.session_brain.mark_permission_needed(tool_name, self.turn_id_getter(), self.session_id)
            return f"`{tool_name}` 還需要你再確認一下：{result.message}"
        final_reply = f"`{tool_name}` 執行失敗：{result.message}"
        if result.error:
            final_reply += f"\n{result.error[:800]}"
        return final_reply
