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
from agent_protocol import EMPTY_REPLY_FALLBACK, FAIL_SAFE_REPLY, TOOL_LOOP_TIMEOUT_REPLY, classify_approval, extract_primary_message, screenshot_tags
from agent_reply_composer import ReplyComposer, ReplyEvent
from agent_replay import record_failure_replay
from agent_runtime_context import build_runtime_context, should_include_task_context, worker_context
from agent_self_recovery import SelfRecoveryController
from agent_session import SessionBrain
from agent_task_graph import TaskGraphManager
from agent_tool_loop import ToolLoopController
from agent_tool_runtime import PendingPermission, PermissionGrant, PermissionManager, ToolExecutor, ToolRegistry
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
        self.reply_composer = ReplyComposer(self.llm)
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
            reply_composer=self.reply_composer,
            session_id=self.session_id,
            turn_id=self.turn_id,
        )

    def _outcome_controller(self) -> OutcomeController:
        return OutcomeController(
            session_brain=self.session_brain,
            task_graphs=self.task_graphs,
            executor=self.executor,
            worker_queue=self.worker_queue,
            hooks=self.hooks,
            after_tool_result=self._after_tool_result,
            recover_tool_result=self._recover_tool_result,
            append_reply=self._append_final_reply,
            session_id=self.session_id,
            turn_id_getter=lambda: self.turn_id,
            reply_composer=self.reply_composer,
        )

    def _permission_replay_controller(self, turn_intent: str, worker_results: list[dict[str, Any]], response_policy: ResponsePolicy) -> PermissionReplayController:
        return PermissionReplayController(
            permission_manager=self.permission_manager,
            session_brain=self.session_brain,
            executor=self.executor,
            hooks=self.hooks,
            after_tool_result=self._after_tool_result,
            append_user_context=lambda text: self._append_user_context_message(text, turn_intent, worker_results, include_task_context=True),
            append_assistant_reply=self._append_assistant_only,
            memory=self.memory,
            continue_after_error=lambda tool_callback: self._tool_loop_controller(response_policy).run(self.memory, "permission replay repair", tool_callback),
            recover_tool_result=self._recover_tool_result,
            response_policy=response_policy,
            reset_turn_state=self._reset_after_deterministic_turn,
            reply_composer=self.reply_composer,
            session_id=self.session_id,
            turn_id_getter=lambda: self.turn_id,
        )

    def _after_tool_result(self, tool_name: str, arguments: dict, result: ToolResult, task_id: str = "") -> tuple[ActionVerificationResult, dict[str, Any] | None]:
        verification = verify_action(tool_name, arguments, result, self.session_id, self.turn_id)
        outcome_summary, artifacts = tool_result_outcome(tool_name, result)
        self.transactions.record_tool_result(tool_name, arguments, result, verification, self.session_id, self.turn_id)
        self.task_graphs.record_tool_result(tool_name, arguments, result, verification, self.session_id, self.turn_id, task_id=task_id)
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

    def _plan_turn_if_needed(self, user_input: str, turn_classification: Any, grant: str = "none"):
        if getattr(turn_classification, "is_chat", False):
            return None
        plan = DEFAULT_PLANNER.plan(user_input, intent=getattr(turn_classification, "intent", "task"), session_id=self.session_id, turn_id=self.turn_id)
        if plan.intent == "cancel":
            self.task_graphs.cancel_active("owner_cancelled", self.session_id, self.turn_id)
            return plan
        force_new = self._should_start_fresh_task(turn_classification, grant)
        self.task_graphs.plan_steps(
            user_input,
            plan.step_names(),
            session_id=self.session_id,
            turn_id=self.turn_id,
            planner_version=plan.planner_version,
            step_specs=plan.step_specs(),
            force_new=force_new,
        )
        self.task_graphs.select_next_step(self.session_id, self.turn_id)
        return plan

    def _should_start_fresh_task(self, turn_classification: Any, grant: str = "none") -> bool:
        if grant in {"single", "turn"}:
            return False
        return getattr(turn_classification, "intent", "") in {"task", "screen_observe"}

    def _plan_needs_owner_approval(self, plan: Any, turn_classification: Any, grant: str = "none") -> bool:
        if grant != "none" or getattr(turn_classification, "intent", "") != "task" or not plan:
            return False
        step_count = len(getattr(plan, "steps", []) or [])
        tools = {
            tool
            for step in getattr(plan, "steps", []) or []
            for tool in getattr(step, "allowed_tools", []) or []
        }
        objective = getattr(plan, "objective", "") or ""
        ui_tools = {"press_hotkey", "click_ui_element", "type_keyboard", "get_screen_ui"}
        sequencers = ["然后", "然後", "再", "接着", "接著", "并且", "並且", "二级", "二級", "菜单", "菜單"]
        ui_words = ["codex", "设置", "設定", "菜单", "菜單", "剩余用量", "剩餘用量", "窗口", "視窗", "alt+tab"]
        return bool(tools & ui_tools) and (step_count >= 4 or sum(marker in objective for marker in sequencers) >= 2 or any(marker in objective for marker in ui_words))

    def _format_plan_approval_reply(self, plan: Any) -> str:
        steps = getattr(plan, "steps", []) or []
        objective = getattr(plan, "objective", "") or ""
        friendly_steps = [self._friendly_plan_step(getattr(step, "name", "") or f"step {index}", objective) for index, step in enumerate(steps[:6], start=1)]
        preview = "；".join(step for step in friendly_steps[:3] if step)
        fallback = (
            f"我先看清楚再動手：{preview}。你回「可以」，月月就一步一步做；"
            "找不到目標我會先停下來跟你說，不硬亂點。"
        )
        return self.reply_composer.compose(
            ReplyEvent(
                "planner_summary",
                user_input=objective,
                summary="\n".join(friendly_steps),
                risk="guarded",
                next_action="owner can approve with 可以; execute one observed step at a time",
                extra={"step_count": len(steps), "steps": friendly_steps},
            ),
            fallback,
        )

    def _friendly_plan_step(self, name: str, objective: str = "") -> str:
        target = self._extract_ui_target_label(objective)
        action = self._extract_ui_action_label(objective)
        expectation = self._extract_ui_expectation_label(objective)
        mapping = {
            "identify target app, screen, and requested path": f"先確認{target}和要做的操作",
            "click visible target window or taskbar icon when possible": f"優先直接點可見的{target}或任務欄圖示，找不到才用快捷鍵切換",
            "open the requested menu or control": action,
            "observe the visible result": expectation,
            "report the requested value or blocker clearly": "把結果或卡點回報給你",
        }
        return mapping.get(name, name)

    def _extract_ui_target_label(self, objective: str) -> str:
        text = objective or ""
        patterns = [
            r"(?:叫|叫做|名叫|named|called)\s*([A-Za-z0-9][A-Za-z0-9 _.-]{2,60})",
            r"([A-Za-z][A-Za-z0-9 _.-]{2,60})\s*(?:的)?(?:窗口|視窗|窗体|app|program|程式|程序)",
            r"(?:打開|打开|切到|導航到|导航到|找到|有)\s*([A-Za-z0-9][A-Za-z0-9 _.-]{2,60})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                value = self._clean_ui_label(match.group(1))
                if value:
                    return f"{value} 視窗"
        blocked = {
            "close connection",
            "open connection",
            "connected",
            "disconnected",
            "owner primary message",
        }
        for phrase in re.findall(r"\b[A-Za-z][A-Za-z0-9]*(?:\s+[A-Za-z0-9][A-Za-z0-9]*){1,5}\b", text):
            value = self._clean_ui_label(phrase)
            folded = value.casefold()
            if not value or folded in blocked or folded.startswith(("close ", "open ")):
                continue
            return f"{value} 視窗"
        return "目標視窗"

    def _extract_ui_action_label(self, objective: str) -> str:
        text = objective or ""
        quoted = re.findall(r"[`'\"“”「」『』]([^`'\"“”「」『』]{2,60})[`'\"“”「」『』]", text)
        for value in quoted:
            cleaned = self._clean_ui_label(value)
            if cleaned and not re.fullmatch(r"connected|disconnected", cleaned, flags=re.IGNORECASE):
                return f"按你指定的「{cleaned}」"
        match = re.search(r"(?:按|點|点|点击|點擊|press|click)\s*([A-Za-z0-9][A-Za-z0-9 _.-]{2,60})", text, flags=re.IGNORECASE)
        if match:
            value = self._clean_ui_label(match.group(1))
            if value:
                return f"按你指定的「{value}」"
        if any(marker in text for marker in ["設定", "设置", "菜單", "菜单"]):
            return "打開你指定的設定或菜單"
        return "按你指定的按鈕或菜單"

    def _extract_ui_expectation_label(self, objective: str) -> str:
        text = objective or ""
        transition = re.search(r"([A-Za-z][A-Za-z0-9 _.-]{1,40})\s*(?:變成|变成|->|=>|\bto\b)\s*([A-Za-z][A-Za-z0-9 _.-]{1,40})", text, flags=re.IGNORECASE)
        if transition:
            before = self._clean_ui_label(transition.group(1))
            after = self._clean_ui_label(transition.group(2))
            if before and after:
                return f"確認狀態從 {before} 變成 {after}"
        if re.search(r"\bconnected\b", text, flags=re.IGNORECASE) and re.search(r"\bdisconnected\b", text, flags=re.IGNORECASE):
            return "確認狀態從 Connected 變成 Disconnected"
        if any(marker in text for marker in ["剩餘", "剩余", "用量", "百分比"]):
            return "讀取畫面上的用量或百分比"
        return "觀察畫面確認結果"

    def _clean_ui_label(self, value: str) -> str:
        cleaned = re.sub(r"\s+", " ", (value or "").strip(" ：:，,。.;；"))
        cleaned = re.sub(r"\b(the|a|an|of|and|then|with|should|is|are)\b$", "", cleaned, flags=re.IGNORECASE).strip()
        return cleaned[:60]

    def _approve_pending_plan_if_needed(self, user_input: str) -> bool:
        graph = self.task_graphs.active()
        if not graph or graph.status != "awaiting_plan_approval":
            return False
        decision = classify_approval(user_input, has_pending=True)
        if decision == "deny":
            self.task_graphs.cancel_active("owner_denied_plan", self.session_id, self.turn_id)
            return False
        if decision == "none":
            return False
        approved = self.task_graphs.approve_plan(self.session_id, self.turn_id)
        if not approved:
            return False
        allowed = {"press_hotkey", "click_ui_element", "type_keyboard"}
        self.permission_manager.grant = PermissionGrant(scope="turn", remaining_uses=50, bundle_name="computer_control_bundle", allowed_tools=allowed)
        self.executor.permissions.grant = self.permission_manager.grant
        emit_trace(
            "planner.plan_approval_granted_computer_bundle",
            session_id=self.session_id,
            turn_id=self.turn_id,
            task_id=approved.task_id,
            allowed_tools=sorted(allowed),
        )
        return True

    def _recover_tool_result(self, tool_name: str, arguments: dict, result: ToolResult, tool_callback: Callable | None, response_policy: ResponsePolicy | None) -> tuple[ToolResult, dict[str, Any] | None]:
        return self.self_recovery.recover(tool_name, arguments or {}, result, tool_callback, response_policy, self.turn_id)

    def _has_outcome_context(self) -> bool:
        state = self.session_brain.state
        return bool(
            state.last_tool
            or state.last_artifacts
            or state.pending_validation
            or state.verification_plan
            or self.task_graphs.active()
        )

    def _restore_pending_permission_if_needed(self, user_input: str) -> None:
        if self.permission_manager.pending:
            return
        if classify_approval(user_input, has_pending=True) == "none":
            return
        graph = self.task_graphs.active()
        if not graph:
            return
        candidates = [
            step
            for step in reversed(graph.steps)
            if step.status == "awaiting_permission" and step.tool_name
        ]
        if not candidates:
            return
        step = candidates[0]
        self.permission_manager.pending = PendingPermission(tool_name=step.tool_name, arguments=step.arguments or {}, created_at=time.time())
        emit_trace(
            "permission.pending_restored",
            session_id=self.session_id,
            turn_id=self.turn_id,
            task_id=graph.task_id,
            step_id=step.step_id,
            tool=step.tool_name,
            arguments=step.arguments or {},
        )

    def chat(self, user_input: str, tool_callback: Callable | None = None, response_policy: ResponsePolicy | None = None) -> dict[str, str]:
        response_policy = response_policy or ResponsePolicy()
        primary_input = extract_primary_message(user_input)
        semantic_input = primary_input or user_input
        self.executor.interactive_mode = self.interactive_mode
        self.turn_id += 1
        self.executor.turn_id = self.turn_id
        assimilated_worker_results = self._assimilate_worker_evidence()
        plan_approved = self._approve_pending_plan_if_needed(semantic_input)
        self._restore_pending_permission_if_needed(semantic_input)
        pending_before = bool(self.permission_manager.pending)
        grant = self.permission_manager.classify_user_reply(semantic_input, self.turn_id)
        if plan_approved and grant in {"none", "single"}:
            grant = "turn"
            allowed = {"press_hotkey", "click_ui_element", "type_keyboard"}
            self.permission_manager.grant = PermissionGrant(scope="turn", remaining_uses=50, bundle_name="computer_control_bundle", allowed_tools=allowed)
            self.executor.permissions.grant = self.permission_manager.grant
        turn_classification = self.session_brain.classify_turn(semantic_input, grant=grant, pending_permission=pending_before, turn_id=self.turn_id, session_id=self.session_id)
        response_policy = policy_for_semantic_intent(turn_classification.intent, response_policy)
        self.hooks.emit("UserMessage", session_id=self.session_id, turn_id=self.turn_id, grant=grant, interactive_mode=self.interactive_mode, pending=bool(self.permission_manager.pending))
        outcome_controller = self._outcome_controller()
        has_outcome_context = self._has_outcome_context()
        outcome_action = detect_outcome_action(semantic_input) if grant == "none" and (turn_classification.intent == "task_continuation" or has_outcome_context) else ""
        if outcome_action:
            handled = outcome_controller.maybe_handle(outcome_action, semantic_input, tool_callback)
            if handled is not None:
                return handled.to_chat_result()
        if grant == "none" and (turn_classification.intent == "task_continuation" or has_outcome_context) and is_result_followup(semantic_input):
            final_reply = format_last_outcome_reply(self.session_brain)
            reply_decision = self.hooks.emit("BeforeReply", session_id=self.session_id, turn_id=self.turn_id, content_preview=final_reply[:160])
            if reply_decision.annotate:
                final_reply += reply_decision.annotate
            self._append_final_reply(user_input, final_reply)
            self.hooks.emit("Stop", session_id=self.session_id, turn_id=self.turn_id, content_preview=final_reply[:160])
            return {"content": final_reply, "reasoning": ""}
        if grant == "single":
            replayed = self._permission_replay_controller(turn_classification.intent, assimilated_worker_results, response_policy).maybe_replay(grant, user_input, tool_callback)
            if replayed is not None:
                self._remember_turn_summary(user_input, replayed.content)
                return replayed.to_chat_result()
        if not turn_classification.is_chat:
            self.transactions.start_or_resume(semantic_input, self.session_id, self.turn_id)
            plan = self._plan_turn_if_needed(semantic_input, turn_classification, grant=grant)
            if self._plan_needs_owner_approval(plan, turn_classification, grant):
                self.task_graphs.mark_awaiting_plan_approval(self.session_id, self.turn_id)
                final_reply = self._format_plan_approval_reply(plan)
                reply_decision = self.hooks.emit("BeforeReply", session_id=self.session_id, turn_id=self.turn_id, content_preview=final_reply[:160])
                if reply_decision.annotate:
                    final_reply += reply_decision.annotate
                self._append_final_reply(user_input, final_reply)
                self.hooks.emit("Stop", session_id=self.session_id, turn_id=self.turn_id, content_preview=final_reply[:160])
                return {"content": final_reply, "reasoning": ""}
        if grant == "single":
            user_input += "\n\n[任務提醒：主人剛剛只批准了上一個被暫停的工具動作；如果還需要，就只重試那一步。]"
        elif grant == "turn":
            user_input += "\n\n[任務提醒：主人允許這一輪需要的工具；只做必要步驟，完成後說清楚結果。]"
        elif grant == "deny":
            user_input += "\n\n[任務提醒：主人拒絕了剛剛那個工具動作，不要再重試它。]"
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
