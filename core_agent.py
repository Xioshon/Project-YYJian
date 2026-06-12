import os
import re
import time
from typing import Any, Callable


from agent_hooks import DEFAULT_HOOK_MANAGER, HookDecision, HookManager, TRACE_LOG_FILE, emit_trace
from agent_action_verification import ActionVerificationResult, verify_action
from agent_latency import ResponsePolicy, policy_for_semantic_intent
from agent_llm import SiliconFlowAdapter
from agent_outcome import OutcomeController, detect_outcome_action, format_last_outcome_reply, is_result_followup, tool_result_outcome
from agent_permission_replay import PermissionReplayController
from agent_planner import DEFAULT_PLANNER
from agent_protocol import EMPTY_REPLY_FALLBACK, FAIL_SAFE_REPLY, TOOL_LOOP_TIMEOUT_REPLY, classify_approval, screenshot_tags
from agent_replay import record_failure_replay
from agent_runtime_context import build_runtime_context, should_include_task_context, worker_context
from agent_self_recovery import SelfRecoveryController
from agent_session import SessionBrain
from agent_task_graph import TaskGraphManager
from agent_tool_loop import ToolLoopController
from agent_tool_runtime import PermissionManager, ToolExecutor, ToolRegistry
from agent_transactions import TaskTransactionManager
from agent_worker import WorkerQueue
from core_tools import AgentTool, PROJECT_CACHE_DIR, ToolResult


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
        self.self_recovery = SelfRecoveryController(executor=self.executor, hooks=self.hooks, session_id=self.session_id)
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

    def _append_final_reply(self, user_input: str, final_reply: str) -> None:
        self.memory.append({"role": "user", "content": user_input})
        self.memory.append({"role": "assistant", "content": final_reply})
        self._remember_turn_summary(user_input, final_reply)
        self._save_history()

    def _user_input_with_runtime_context(
        self,
        user_input: str,
        turn_intent: str,
        worker_results: list[dict[str, Any]] | None = None,
        *,
        include_task_context: bool = False,
    ) -> str:
        return build_runtime_context(
            user_input,
            turn_intent=turn_intent,
            session_summary=self.session_brain.summary(),
            task_summary=self.task_graphs.summary(),
            worker_results=worker_results,
            include_task_context=include_task_context,
        )

    def _append_user_context_message(
        self,
        user_input: str,
        turn_intent: str,
        worker_results: list[dict[str, Any]] | None = None,
        *,
        include_task_context: bool = True,
    ) -> str:
        enriched = self._user_input_with_runtime_context(user_input, turn_intent, worker_results, include_task_context=include_task_context)
        self.memory.append({"role": "user", "content": enriched})
        return enriched

    def _append_assistant_only(self, final_reply: str) -> None:
        self.memory.append({"role": "assistant", "content": final_reply})
        self._save_history()

    def _reset_after_deterministic_turn(self) -> None:
        self.permission_manager.reset_after_turn()
        self.always_allow_tools = False

    def _tool_loop_controller(self, response_policy: ResponsePolicy) -> ToolLoopController:
        return ToolLoopController(
            llm=self.llm,
            registry=self.registry,
            executor=self.executor,
            hooks=self.hooks,
            session_brain=self.session_brain,
            task_graphs=self.task_graphs,
            permission_manager=self.permission_manager,
            response_policy=response_policy,
            clean_output=clean_assistant_output,
            recover_tool_result=self._recover_tool_result,
            after_tool_result=self._after_tool_result,
            capture_screen=_capture_screen,
            remember_turn_summary=self._remember_turn_summary,
            save_history=self._save_history,
            reset_turn_state=self._reset_after_deterministic_turn,
            session_id=self.session_id,
            turn_id=self.turn_id,
        )

    def _outcome_controller(self) -> OutcomeController:
        return OutcomeController(
            session_brain=self.session_brain,
            executor=self.executor,
            worker_queue=self.worker_queue,
            hooks=self.hooks,
            after_tool_result=self._after_tool_result,
            append_reply=self._append_final_reply,
            session_id=self.session_id,
            turn_id_getter=lambda: self.turn_id,
        )

    def _permission_replay_controller(self, turn_intent: str, worker_results: list[dict[str, Any]]) -> PermissionReplayController:
        return PermissionReplayController(
            permission_manager=self.permission_manager,
            session_brain=self.session_brain,
            executor=self.executor,
            hooks=self.hooks,
            after_tool_result=self._after_tool_result,
            append_user_context=lambda text: self._append_user_context_message(text, turn_intent, worker_results, include_task_context=True),
            append_assistant_reply=self._append_assistant_only,
            reset_turn_state=self._reset_after_deterministic_turn,
            session_id=self.session_id,
            turn_id_getter=lambda: self.turn_id,
        )

    def _after_tool_result(self, tool_name: str, arguments: dict, result: ToolResult) -> tuple[ActionVerificationResult, dict[str, Any] | None]:
        verification = verify_action(tool_name, arguments, result, self.session_id, self.turn_id)
        outcome_summary, artifacts = tool_result_outcome(tool_name, result)
        self.transactions.record_tool_result(tool_name, arguments, result, verification, self.session_id, self.turn_id)
        self.task_graphs.record_tool_result(tool_name, arguments, result, verification, self.session_id, self.turn_id)
        self.session_brain.mark_tool_result(tool_name, result.status, self.turn_id, self.session_id, summary=outcome_summary, artifacts=artifacts)
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
        return self.self_recovery.recover(tool_name, arguments or {}, result, tool_callback, response_policy, self.turn_id)

    def chat(self, user_input: str, tool_callback: Callable | None = None, response_policy: ResponsePolicy | None = None) -> dict[str, str]:
        response_policy = response_policy or ResponsePolicy()
        self.executor.interactive_mode = self.interactive_mode
        self.turn_id += 1
        self.executor.turn_id = self.turn_id
        assimilated_worker_results = self._assimilate_worker_evidence()
        pending_before = bool(self.permission_manager.pending)
        grant = self.permission_manager.classify_user_reply(user_input, self.turn_id)
        turn_classification = self.session_brain.classify_turn(user_input, grant=grant, pending_permission=pending_before, turn_id=self.turn_id, session_id=self.session_id)
        response_policy = policy_for_semantic_intent(turn_classification.intent, response_policy)
        if not turn_classification.is_chat:
            self.transactions.start_or_resume(user_input, self.session_id, self.turn_id)
            self._plan_turn_if_needed(user_input, turn_classification)
        self.hooks.emit("UserMessage", session_id=self.session_id, turn_id=self.turn_id, grant=grant, interactive_mode=self.interactive_mode, pending=bool(self.permission_manager.pending))
        outcome_controller = self._outcome_controller()
        outcome_action = detect_outcome_action(user_input) if grant == "none" and turn_classification.intent == "task_continuation" else ""
        if outcome_action:
            handled = outcome_controller.maybe_handle(outcome_action, user_input, tool_callback)
            if handled is not None:
                return handled.to_chat_result()
        if grant == "none" and turn_classification.intent == "task_continuation" and is_result_followup(user_input):
            final_reply = format_last_outcome_reply(self.session_brain)
            reply_decision = self.hooks.emit("BeforeReply", session_id=self.session_id, turn_id=self.turn_id, content_preview=final_reply[:160])
            if reply_decision.annotate:
                final_reply += reply_decision.annotate
            self._append_final_reply(user_input, final_reply)
            self.hooks.emit("Stop", session_id=self.session_id, turn_id=self.turn_id, content_preview=final_reply[:160])
            return {"content": final_reply, "reasoning": ""}
        if grant == "single":
            replayed = self._permission_replay_controller(turn_classification.intent, assimilated_worker_results).maybe_replay(grant, user_input, tool_callback)
            if replayed is not None:
                self._remember_turn_summary(user_input, replayed.content)
                return replayed.to_chat_result()
        if grant == "single":
            user_input += "\n\n[System notice: owner approved the previously blocked exact tool call once. Retry that same tool call if it is still needed.]"
        elif grant == "turn":
            user_input += "\n\n[System notice: owner approved tool use for this task turn. Use only necessary tools and report results.]"
        elif grant == "deny":
            user_input += "\n\n[System notice: owner rejected the pending tool action. Do not retry it.]"
        include_task_context = should_include_task_context(
            turn_classification.intent,
            pending_permission=pending_before,
            active_task=bool(self.task_graphs.active()),
            grant=grant,
            worker_results=assimilated_worker_results,
        )
        user_input = self._user_input_with_runtime_context(
            user_input,
            turn_classification.intent,
            assimilated_worker_results,
            include_task_context=include_task_context,
        )

        self.memory.append({"role": "user", "content": user_input})
        return self._tool_loop_controller(response_policy).run(self.memory, user_input, tool_callback).to_chat_result()


