import json
from dataclasses import dataclass
from typing import Any, Callable

from agent_hooks import TRACE_LOG_FILE
from agent_replay import record_failure_replay
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
        self.session_id = session_id
        self.turn_id = turn_id

    def run(self, memory: list[dict[str, Any]], user_input_for_summary: str, tool_callback: Callable | None) -> ToolLoopResult:
        total_reasoning = ""
        successful_tools: list[str] = []
        tool_call_counts: dict[str, int] = {}

        for _ in range(self.response_policy.max_tool_iterations):
            response = self.llm.chat_with_tools(memory, self.registry.list())
            self.hooks.emit("llm.response", session_id=self.session_id, turn_id=self.turn_id, has_tool_calls=bool(response.get("tool_calls")), content_preview=(response.get("content") or "")[:160])
            if response.get("reasoning"):
                total_reasoning += response["reasoning"] + "\n\n"

            tool_calls = response.get("tool_calls") or []
            if tool_calls:
                memory.append(self._assistant_tool_call_message(response, tool_calls))
                stopped = self._run_tool_calls(memory, tool_calls, tool_callback, user_input_for_summary, total_reasoning, successful_tools, tool_call_counts)
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
        tool_call_counts: dict[str, int],
    ) -> ToolLoopResult | None:
        for call in tool_calls:
            arguments = call.get("arguments", {})
            signature = tool_signature(call["name"], arguments)
            tool_call_counts[signature] = tool_call_counts.get(signature, 0) + 1
            if tool_call_counts[signature] > max(1, self.response_policy.max_repeated_tool_calls):
                return self._stop_repeated_tool(memory, call, arguments, tool_call_counts[signature], user_input_for_summary, total_reasoning)

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
                    return None
                return self._stop_route_block(memory, call, result, user_input_for_summary, total_reasoning)
            if result.status == "ok":
                successful_tools.append(call["name"])
        return None

    def _stop_repeated_tool(self, memory: list[dict[str, Any]], call: dict[str, Any], arguments: dict[str, Any], count: int, user_input: str, total_reasoning: str) -> ToolLoopResult:
        repeated_result = ToolResult("error", "Repeated tool call stopped.", error="repeated_tool_call")
        replay_case = record_failure_replay(call["name"], arguments, repeated_result, session_id=self.session_id, turn_id=self.turn_id, count=count)
        self.task_graphs.mark_blocked("repeated tool call stopped", self.session_id, self.turn_id, tool_name=call["name"], arguments=arguments, result=repeated_result)
        final_reply = repeated_tool_stop_reply(call["name"], replay_case.get("name", ""))
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
        final_reply = failsafe_reply(tag)
        memory.append({"role": "assistant", "content": final_reply})
        self.reset_turn_state()
        self.remember_turn_summary(user_input, final_reply)
        self.save_history()
        return ToolLoopResult(final_reply, total_reasoning.strip())

    def _stop_failure_replay(self, memory: list[dict[str, Any]], call: dict[str, Any], replay_case: dict[str, Any], user_input: str, total_reasoning: str) -> ToolLoopResult:
        final_reply = failure_replay_reply(call["name"], replay_case.get("name", ""), TRACE_LOG_FILE)
        memory.append({"role": "assistant", "content": final_reply})
        self.reset_turn_state()
        self.hooks.emit("StopFailure", session_id=self.session_id, turn_id=self.turn_id, tool=call["name"], replay_case=replay_case.get("name"))
        self.remember_turn_summary(user_input, final_reply)
        self.save_history()
        return ToolLoopResult(final_reply, total_reasoning.strip())

    def _stop_route_block(self, memory: list[dict[str, Any]], call: dict[str, Any], result: ToolResult, user_input: str, total_reasoning: str) -> ToolLoopResult:
        final_reply = friendly_tool_block(call["name"], result, getattr(self.response_policy, "route", ""))
        memory.append({"role": "assistant", "content": final_reply})
        self.reset_turn_state()
        self.hooks.emit("StopFailure", session_id=self.session_id, turn_id=self.turn_id, tool=call["name"], reason="route_policy_block")
        self.remember_turn_summary(user_input, final_reply)
        self.save_history()
        return ToolLoopResult(final_reply, total_reasoning.strip())

    def _final_reply(self, memory: list[dict[str, Any]], response: dict[str, Any], user_input: str, total_reasoning: str, successful_tools: list[str]) -> ToolLoopResult:
        final_reply = self.clean_output(response.get("content", "")) or empty_reply_fallback()
        reply_decision = self.hooks.emit("BeforeReply", session_id=self.session_id, turn_id=self.turn_id, content_preview=final_reply[:160])
        if reply_decision.annotate:
            final_reply += reply_decision.annotate
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
        timeout_msg = tool_loop_timeout_reply()
        memory.append({"role": "assistant", "content": timeout_msg})
        self.reset_turn_state()
        self.remember_turn_summary(user_input, timeout_msg)
        self.save_history()
        return ToolLoopResult(timeout_msg, total_reasoning.strip())
