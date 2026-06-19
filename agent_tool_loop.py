import json
from dataclasses import dataclass
from typing import Any, Callable

from agent_hooks import TRACE_LOG_FILE
from agent_reply_composer import ReplyComposer, ReplyEvent
from agent_replay import record_failure_replay
from agent_self_recovery import self_repair_instruction, should_prompt_self_repair
from agent_user_voice import empty_reply_fallback, failsafe_reply, failure_replay_reply, friendly_tool_block, repeated_tool_stop_reply, tool_loop_timeout_reply
from core_tools import ToolResult


@dataclass
class ToolLoopResult:
    content: str
    reasoning: str = ""

    def to_chat_result(self) -> dict[str, str]:
        return {"content": self.content, "reasoning": self.reasoning}


def tool_signature(tool_name: str, arguments: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        encoded = str(arguments or {})
    return f"{tool_name}:{encoded[:1200]}"


class ToolLoopController:
    """Owns the LLM tool-call loop and its safety stop conditions."""

    def __init__(
        self,
        *,
        llm: Any,
        registry: Any,
        executor: Any,
        hooks: Any,
        session_brain: Any,
        task_graphs: Any,
        permission_manager: Any,
        response_policy: Any,
        clean_output: Callable[[str], str],
        recover_tool_result: Callable[[str, dict, ToolResult, Callable | None, Any], tuple[ToolResult, dict[str, Any] | None]],
        after_tool_result: Callable[[str, dict, ToolResult], tuple[Any, dict[str, Any] | None]],
        capture_screen: Callable[[], str],
        remember_turn_summary: Callable[[str, str], None],
        save_history: Callable[[], None],
        reset_turn_state: Callable[[], None],
        reply_composer: ReplyComposer | None = None,
        session_id: str,
        turn_id: int,
    ):
        self.llm = llm
        self.registry = registry
        self.executor = executor
        self.hooks = hooks
        self.session_brain = session_brain
        self.task_graphs = task_graphs
        self.permission_manager = permission_manager
        self.response_policy = response_policy
        self.clean_output = clean_output
        self.recover_tool_result = recover_tool_result
        self.after_tool_result = after_tool_result
        self.capture_screen = capture_screen
        self.remember_turn_summary = remember_turn_summary
        self.save_history = save_history
        self.reset_turn_state = reset_turn_state
        self.reply_composer = reply_composer or ReplyComposer()
        self.session_id = session_id
        self.turn_id = turn_id

    def run(self, memory: list[dict[str, Any]], user_input_for_summary: str, tool_callback: Callable | None) -> ToolLoopResult:
        total_reasoning = ""
        successful_tools: list[str] = []
        repeat_state: dict[str, Any] = {"last_signature": "", "consecutive_count": 0}
        repair_prompted_signatures: set[str] = set()

        for _ in range(self.response_policy.max_tool_iterations):
            response = self.llm.chat_with_tools(memory, self.registry.list())
            self.hooks.emit("llm.response", session_id=self.session_id, turn_id=self.turn_id, has_tool_calls=bool(response.get("tool_calls")), content_preview=(response.get("content") or "")[:160])
            if response.get("reasoning"):
                total_reasoning += response["reasoning"] + "\n\n"

            tool_calls = response.get("tool_calls") or []
            if tool_calls:
                memory.append(self._assistant_tool_call_message(response, tool_calls))
                stopped = self._run_tool_calls(memory, tool_calls, tool_callback, user_input_for_summary, total_reasoning, successful_tools, repeat_state, repair_prompted_signatures)
                if stopped is not None:
                    return stopped
                continue

            return self._final_reply(memory, response, user_input_for_summary, total_reasoning, successful_tools)

        return self._timeout(memory, user_input_for_summary, total_reasoning)

    def _assistant_tool_call_message(self, response: dict[str, Any], tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": response.get("content") or "",
            "tool_calls": [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": call.get("raw_arguments", json.dumps(call.get("arguments", {}), ensure_ascii=False)),
                    },
                }
                for call in tool_calls
            ],
        }

    def _run_tool_calls(
        self,
        memory: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
        tool_callback: Callable | None,
        user_input_for_summary: str,
        total_reasoning: str,
        successful_tools: list[str],
        repeat_state: dict[str, Any],
        repair_prompted_signatures: set[str],
    ) -> ToolLoopResult | None:
        for call in tool_calls:
            arguments = call.get("arguments", {})
            signature = tool_signature(call["name"], arguments)
            if repeat_state.get("last_signature") == signature:
                repeat_state["consecutive_count"] = int(repeat_state.get("consecutive_count") or 0) + 1
            else:
                repeat_state["last_signature"] = signature
                repeat_state["consecutive_count"] = 1
            if repeat_state["consecutive_count"] > max(1, self.response_policy.max_repeated_tool_calls):
                return self._stop_repeated_tool(memory, call, arguments, repeat_state["consecutive_count"], user_input_for_summary, total_reasoning)

            result = self.executor.execute(call["name"], arguments, tool_callback, self.response_policy)
            result, _recovery = self.recover_tool_result(call["name"], arguments, result, tool_callback, self.response_policy)
            _verification, replay_case = self.after_tool_result(call["name"], arguments, result)
            result_text = result.to_text()[:4000]

            if "fail-safe" in result_text.casefold() or "failsafe" in result_text.casefold():
                return self._stop_failsafe(memory, call, user_input_for_summary, total_reasoning)

            memory.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": result_text})

            if replay_case:
                return self._stop_failure_replay(memory, call, replay_case, user_input_for_summary, total_reasoning)
            if result.status == "blocked":
                if result.requires_permission:
                    self.session_brain.mark_permission_needed(call["name"], self.turn_id, self.session_id)
                    return self._stop_permission_request(memory, call, result, user_input_for_summary, total_reasoning)
                return self._stop_route_block(memory, call, result, user_input_for_summary, total_reasoning)
            if result.status == "error" and self._maybe_prompt_self_repair(memory, call, arguments, result, signature, repair_prompted_signatures):
                continue
            if result.status == "ok":
                successful_tools.append(call["name"])
        return None

    def _maybe_prompt_self_repair(
        self,
        memory: list[dict[str, Any]],
        call: dict[str, Any],
        arguments: dict[str, Any],
        result: ToolResult,
        signature: str,
        repair_prompted_signatures: set[str],
    ) -> bool:
        if signature in repair_prompted_signatures:
            return False
        max_repairs = max(0, int(getattr(self.response_policy, "max_self_repair_attempts", 1) or 1))
        if len(repair_prompted_signatures) >= max_repairs:
            return False
        if not should_prompt_self_repair(call["name"], result, self.response_policy):
            return False
        repair_prompted_signatures.add(signature)
        instruction = self_repair_instruction(call["name"], arguments, result)
        memory.append({"role": "system", "content": instruction})
        self.hooks.emit("SelfRepairPrompt", session_id=self.session_id, turn_id=self.turn_id, tool=call["name"], signature=signature)
        return True

    def _stop_repeated_tool(self, memory: list[dict[str, Any]], call: dict[str, Any], arguments: dict[str, Any], count: int, user_input: str, total_reasoning: str) -> ToolLoopResult:
        repeated_result = ToolResult("error", "Repeated tool call stopped.", error="repeated_tool_call")
        replay_case = record_failure_replay(call["name"], arguments, repeated_result, session_id=self.session_id, turn_id=self.turn_id, count=count)
        self.task_graphs.mark_blocked("repeated tool call stopped", self.session_id, self.turn_id, tool_name=call["name"], arguments=arguments, result=repeated_result)
        fallback = repeated_tool_stop_reply(call["name"], replay_case.get("name", ""))
        final_reply = self.reply_composer.compose(
            ReplyEvent(
                "repeated_tool_stop",
                user_input=user_input,
                tool_name=call["name"],
                reason="same tool and arguments repeated in one step",
                extra={"replay_name": replay_case.get("name", ""), "count": count},
            ),
            fallback,
        )
        self._clear_transient_self_repair(memory)
        memory.append({"role": "assistant", "content": final_reply})
        self.reset_turn_state()
        self.hooks.emit("StopFailure", session_id=self.session_id, turn_id=self.turn_id, tool=call["name"], replay_case=replay_case.get("name"), reason="repeated_tool_call")
        self.remember_turn_summary(user_input, final_reply)
        self.save_history()
        return ToolLoopResult(final_reply, total_reasoning.strip())

    def _stop_failsafe(self, memory: list[dict[str, Any]], call: dict[str, Any], user_input: str, total_reasoning: str) -> ToolLoopResult:
        screen = self.capture_screen()
        tag = f" [系統截圖: {screen}]" if screen else ""
        result_text = ToolResult("error", f"Fail-safe triggered. Stop all actions immediately.{tag}").to_text()
        memory.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": result_text})
        final_reply = self.reply_composer.compose(
            ReplyEvent("failsafe", user_input=user_input, tool_name=call["name"], reason="fail-safe triggered", extra={"tag": tag}),
            failsafe_reply(tag),
        )
        self._clear_transient_self_repair(memory)
        memory.append({"role": "assistant", "content": final_reply})
        self.reset_turn_state()
        self.remember_turn_summary(user_input, final_reply)
        self.save_history()
        return ToolLoopResult(final_reply, total_reasoning.strip())

    def _stop_failure_replay(self, memory: list[dict[str, Any]], call: dict[str, Any], replay_case: dict[str, Any], user_input: str, total_reasoning: str) -> ToolLoopResult:
        fallback = failure_replay_reply(call["name"], replay_case.get("name", ""), TRACE_LOG_FILE)
        final_reply = self.reply_composer.compose(
            ReplyEvent(
                "tool_error",
                user_input=user_input,
                tool_name=call["name"],
                result=ToolResult("error", "Tool failed repeatedly.", error="failure_replay"),
                reason="failure replay generated",
                extra={"replay_name": replay_case.get("name", "")},
            ),
            fallback,
        )
        self._clear_transient_self_repair(memory)
        memory.append({"role": "assistant", "content": final_reply})
        self.reset_turn_state()
        self.hooks.emit("StopFailure", session_id=self.session_id, turn_id=self.turn_id, tool=call["name"], replay_case=replay_case.get("name"))
        self.remember_turn_summary(user_input, final_reply)
        self.save_history()
        return ToolLoopResult(final_reply, total_reasoning.strip())

    def _stop_route_block(self, memory: list[dict[str, Any]], call: dict[str, Any], result: ToolResult, user_input: str, total_reasoning: str) -> ToolLoopResult:
        fallback = friendly_tool_block(call["name"], result, getattr(self.response_policy, "route", ""))
        final_reply = self.reply_composer.compose(
            ReplyEvent(
                "tool_error",
                user_input=user_input,
                tool_name=call["name"],
                result=result,
                reason="tool blocked by route or policy",
                next_action="ask owner for a clearer task or continue the active task if appropriate",
            ),
            fallback,
        )
        reply_decision = self.hooks.emit("BeforeReply", session_id=self.session_id, turn_id=self.turn_id, content_preview=final_reply[:160])
        if reply_decision.annotate:
            final_reply += reply_decision.annotate
        self._clear_transient_self_repair(memory)
        memory.append({"role": "assistant", "content": final_reply})
        self.reset_turn_state()
        self.hooks.emit("StopFailure", session_id=self.session_id, turn_id=self.turn_id, tool=call["name"], reason="route_policy_block")
        self.remember_turn_summary(user_input, final_reply)
        self.save_history()
        return ToolLoopResult(final_reply, total_reasoning.strip())

    def _stop_permission_request(self, memory: list[dict[str, Any]], call: dict[str, Any], result: ToolResult, user_input: str, total_reasoning: str) -> ToolLoopResult:
        fallback = result.message or f"`{call['name']}` 這一步需要你確認一下喔。你回「可以」我就繼續剛剛那一步。"
        final_reply = self.reply_composer.compose(
            ReplyEvent(
                "permission_request",
                user_input=user_input,
                tool_name=call["name"],
                result=result,
                risk="guarded",
                next_action="owner can approve with 可以",
            ),
            fallback,
        )
        reply_decision = self.hooks.emit("BeforeReply", session_id=self.session_id, turn_id=self.turn_id, content_preview=final_reply[:160])
        if reply_decision.annotate:
            final_reply += reply_decision.annotate
        self._clear_transient_self_repair(memory)
        memory.append({"role": "assistant", "content": final_reply})
        self.hooks.emit("Stop", session_id=self.session_id, turn_id=self.turn_id, content_preview=final_reply[:160], reason="permission_request")
        self.remember_turn_summary(user_input, final_reply)
        self.save_history()
        return ToolLoopResult(final_reply, total_reasoning.strip())

    def _final_reply(self, memory: list[dict[str, Any]], response: dict[str, Any], user_input: str, total_reasoning: str, successful_tools: list[str]) -> ToolLoopResult:
        final_reply = self.clean_output(response.get("content", "")) or empty_reply_fallback()
        reply_decision = self.hooks.emit("BeforeReply", session_id=self.session_id, turn_id=self.turn_id, content_preview=final_reply[:160])
        if reply_decision.annotate:
            final_reply += reply_decision.annotate
        self._clear_transient_self_repair(memory)
        memory.append({"role": "assistant", "content": final_reply})
        self.remember_turn_summary(user_input, final_reply)
        self.hooks.emit("Stop", session_id=self.session_id, turn_id=self.turn_id, content_preview=final_reply[:160])
        if successful_tools:
            self.session_brain.mark_validation_needed("verify tool results: " + ", ".join(successful_tools[-5:]), self.turn_id, self.session_id, evidence=successful_tools[-5:])
        self.reset_turn_state()
        self.save_history()
        if successful_tools:
            self.task_graphs.mark_completed(self.session_id, self.turn_id)
        return ToolLoopResult(final_reply, total_reasoning.strip())

    def _timeout(self, memory: list[dict[str, Any]], user_input: str, total_reasoning: str) -> ToolLoopResult:
        timeout_msg = self.reply_composer.compose(
            ReplyEvent("timeout", user_input=user_input, reason="tool loop reached iteration budget"),
            tool_loop_timeout_reply(),
        )
        self._clear_transient_self_repair(memory)
        memory.append({"role": "assistant", "content": timeout_msg})
        self.reset_turn_state()
        self.remember_turn_summary(user_input, timeout_msg)
        self.save_history()
        return ToolLoopResult(timeout_msg, total_reasoning.strip())

    def _clear_transient_self_repair(self, memory: list[dict[str, Any]]) -> None:
        memory[:] = [
            message
            for message in memory
            if not (message.get("role") == "system" and str(message.get("content") or "").startswith("[SelfRepair]"))
        ]
