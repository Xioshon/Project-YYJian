from __future__ import annotations

import copy
import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent_protocol import classify_approval, extract_primary_message
from core_tools import AgentTool, ToolResult, env_value
from skill_engine import SkillContext, execute_skill, skill_tools
from voice_contract import VOICE_REGISTER_EN, owner_text_is_simplified, voice_register_violation

from .context import (
    ContextCompiler,
    ShortContextStore,
    classify_turn_mode,
    is_benign_testing_note,
    owner_script_is_simplified_with_history,
    to_simplified_script,
    to_traditional_script,
)
from .events import SingleWriterEventLoop
from .memory import build_default_memory
from .models import (
    ActionEnvelope,
    ExecutionEvidence,
    PermissionState,
    RuntimeEvent,
    RuntimeState,
    StepStatus,
    TurnEnvelope,
    TurnMode,
    V3ToolResult,
    WorkflowState,
    WorkflowStatus,
    runtime_state_from_dict,
)
from .observations import ObservationService
from .permissions import PermissionController
from .planning import GoalPlannerV3
from .providers import ProviderFailure
from .storage import AtomicJsonStore, JsonlEventStore
from .tools import ToolCatalogV3
from .workflow import OBSERVATION_SOURCES, WorkflowEngine

# Common emoji blocks (emoticons, symbols/pictographs, transport, supplemental, extended,
# dingbats, misc symbols). Used to keep a single emoji from becoming a per-line tic.
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff\U00002190-\U000021ff]"
)

SCREEN_DESCRIBE_PROMPT = (
    "用繁體中文描述這張電腦屏幕截圖上實際可見的內容：目前是哪個應用程序或窗口、"
    "主要區域在顯示什麼、有哪些醒目的文字或狀態。只描述看得到的，不要猜測看不到的部分。"
)


def _reply_reflects_content(reply: str, summary: str) -> bool:
    """The persona reply must actually carry the observed content, not just claim to have looked."""
    if not summary:
        return True
    fragments = list(dict.fromkeys(re.findall(r"[一-鿿]{2,6}|[A-Za-z][A-Za-z0-9]{2,}", summary)))
    if not fragments:
        return len(reply) >= 10
    hits = sum(1 for fragment in fragments if fragment in reply)
    return hits >= min(2, len(fragments))


class YueYueRuntimeV3:
    """Single-writer, goal-driven YueYue runtime."""

    runtime_version = "v3"

    def __init__(self, root: str | Path, provider: Any, state_dir: str | Path | None = None):
        self.root = Path(root).resolve()
        self.state_dir = Path(state_dir or self.root / "workspace/project_cache/v3").resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.state_store = AtomicJsonStore(self.state_dir / "runtime_state.json", RuntimeState, runtime_state_from_dict)
        state = self.state_store.load()
        self.event_store = JsonlEventStore(self.state_dir / "runtime_events.jsonl")
        self.workflow_engine = WorkflowEngine(require_semantic_actions=True)
        self.events = SingleWriterEventLoop(state, self._reduce, self.event_store, persist=self.state_store.save)
        self.observations = ObservationService(self.state_dir / "observations")
        self.tools = ToolCatalogV3(self.observations)
        self.permission_controller = PermissionController()
        self.planner = GoalPlannerV3(provider, self.tools.names)
        self.short_context = ShortContextStore(self.state_dir / "short_context.json")
        self.context = ContextCompiler(self.root, self.short_context)
        # ROADMAP P2: long-term memory. Retrieval failures degrade to "no recall" (chat still
        # works with zero API balance); distillation runs every N chat turns and drops any fact
        # the owner's own words cannot ground.
        self.memory, self.memory_distiller = build_default_memory(
            self.root, env_value("SILICONFLOW_API_KEY"), provider, env_value("YUEYUE_CHAT_MODEL")
        )
        self.context.memory = self.memory
        self._turns_since_distill = 0
        # list_tasks skill introspection hook (module-level to avoid a runtime<->skills cycle).
        import skill_engine as _skill_engine

        def _introspect() -> dict:
            wf = self.state.workflow
            active = None
            if wf and wf.status not in {WorkflowStatus.COMPLETED, WorkflowStatus.CANCELLED}:
                active = {"objective": wf.goal.objective, "status": wf.status.value}
            return {"active": active, "queued": list(self.state.task_queue)}

        _skill_engine.RUNTIME_INTROSPECT = _introspect

        def _memory_write(fact: str, source: str) -> None:
            from .memory import MemoryEntry

            self.memory.add(MemoryEntry("fact", str(fact)[:200], time.strftime("%Y-%m-%d"), source=str(source)[:160]))

        _skill_engine.MEMORY_WRITE = _memory_write
        self.interactive_mode = False
        self.max_iterations = 18
        set_event_sink = getattr(self.provider, "set_event_sink", None)
        if callable(set_event_sink):
            set_event_sink(self._record_provider_event)

    @property
    def state(self) -> RuntimeState:
        return self.events.state

    def close(self) -> None:
        close = getattr(self.provider, "close", None)
        if callable(close):
            close()

    def submit_external_event(self, kind: str, payload: dict[str, Any]) -> str:
        event = RuntimeEvent(
            kind=str(kind or "external.evidence"),
            session_id=self.state.session_id,
            workflow_id=self.state.workflow.workflow_id if self.state.workflow else "",
            payload=dict(payload or {}),
        )
        self.events.submit(event)
        return event.event_id

    def chat(
        self, user_input: str, tool_callback: Callable | None = None, response_policy: Any | None = None
    ) -> dict[str, str]:
        primary = extract_primary_message(user_input) or str(user_input or "")
        route = str(getattr(response_policy, "route", "") or "")
        mode = _mode_from_route(route) if route else classify_turn_mode(primary)
        if mode == TurnMode.TASK and is_benign_testing_note(primary):
            mode = TurnMode.CHAT
        turn = TurnEnvelope(chat_id="telegram", text=primary, mode=mode)
        if route.casefold() == "screen_observe":
            with self.events.writer_scope():
                return {"content": self._screen_observe_turn(turn, tool_callback), "reasoning": ""}
        return {"content": self.process_turn(turn, tool_callback), "reasoning": ""}

    def process_turn(self, turn: TurnEnvelope, tool_callback: Callable | None = None) -> str:
        with self.events.writer_scope():
            reply = self._process_turn(turn, tool_callback)
            # Task-queue drain (owner concept 2026-07-20): when the active workflow just finished
            # and tasks are queued, start the next one in the SAME turn and append its outcome -
            # the owner batch-fires requests and they complete one after another, no re-prompting.
            drained = 0
            while (
                self.state.task_queue
                and drained < 2
                and (
                    self.state.workflow is None
                    or self.state.workflow.status in {WorkflowStatus.COMPLETED, WorkflowStatus.CANCELLED}
                )
            ):
                state = copy.deepcopy(self.state)
                next_objective = state.task_queue.pop(0)
                self._replace_state(state, "task_queue.pop", turn.turn_id)
                follow = TurnEnvelope(turn.chat_id, next_objective, TurnMode.TASK, turn.message_id)
                follow_reply = self._start_task(follow, tool_callback)
                reply = f"{reply}\n\n{follow_reply}"
                drained += 1
                if self.state.workflow and self.state.workflow.status not in {
                    WorkflowStatus.COMPLETED,
                    WorkflowStatus.CANCELLED,
                }:
                    break  # next task needs permission/attention - stop draining
            return reply

    def _workflow_is_stale(self, workflow: WorkflowState | None) -> bool:
        """An incomplete workflow (awaiting permission, blocked, or mid-run) that hasn't been
        touched for YUEYUE_WORKFLOW_STALE_MINUTES (default 30) counts as abandoned - the owner
        moved on. Genuine approvals arrive quickly, so this never expires an active task."""
        if not workflow or workflow.status in {WorkflowStatus.COMPLETED, WorkflowStatus.CANCELLED}:
            return False
        try:
            minutes = float(env_value("YUEYUE_WORKFLOW_STALE_MINUTES") or 30)
        except ValueError:
            minutes = 30.0
        minutes = max(1.0, minutes)
        return (time.time() - float(workflow.updated_at or 0.0)) > minutes * 60

    def _process_turn(self, turn: TurnEnvelope, tool_callback: Callable | None = None) -> str:
        self.events.drain()
        self._emit("turn.received", turn.turn_id, {"mode": turn.mode.value, "text": turn.text[:500]})
        if _is_cancel(turn.text):
            state = copy.deepcopy(self.state)
            state.workflow = None
            state.permission = PermissionState()
            self._replace_state(state, "workflow.cancelled", turn.turn_id)
            return self._compose_owner_voice("workflow_cancelled", {}, "好，先停在這裡。剛才的任務已經取消了。")

        # Auto-expire an abandoned workflow: an incomplete task left AWAITING_PERMISSION or
        # BLOCKED must not linger and hijack a later unrelated turn (observed 2026-07-14 - a
        # stale count-task permission fired on an unrelated chat message). If the pending
        # workflow has not been touched for a while, drop it and treat this turn as fresh.
        if self._workflow_is_stale(self.state.workflow):
            state = copy.deepcopy(self.state)
            state.workflow = None
            state.permission = PermissionState()
            self._replace_state(state, "workflow.expired", turn.turn_id)

        workflow = self.state.workflow
        has_pending = bool(self.state.permission.pending_action) or bool(
            workflow and workflow.status == WorkflowStatus.AWAITING_PERMISSION
        )
        approval = classify_approval(turn.text, has_pending=has_pending)
        if has_pending and approval != "none":
            return self._handle_permission_reply(turn, approval, tool_callback)

        if (
            workflow
            and turn.mode == TurnMode.TASK
            and workflow.status not in {WorkflowStatus.CANCELLED, WorkflowStatus.COMPLETED}
            and _is_continue(turn.text)
        ):
            if workflow.status == WorkflowStatus.BLOCKED:
                state = copy.deepcopy(self.state)
                state.workflow.status = WorkflowStatus.RUNNING
                step = state.workflow.current_step()
                if step and step.status == StepStatus.BLOCKED:
                    step.status = StepStatus.RUNNING
                self._replace_state(state, "workflow.resumed", turn.turn_id)
            return self._run_workflow(turn, tool_callback)

        if turn.mode in {TurnMode.CHAT, TurnMode.SOCIAL}:
            return self._chat_turn(turn)

        # A new task while one is still active (running/awaiting) queues instead of vanishing.
        if (
            self.state.workflow
            and self.state.workflow.status not in {WorkflowStatus.COMPLETED, WorkflowStatus.CANCELLED}
        ):
            state = copy.deepcopy(self.state)
            state.task_queue.append(turn.text[:500])
            self._replace_state(state, "task_queue.push", turn.turn_id)
            return self._compose_owner_voice(
                "task_queued",
                {"queued": turn.text[:120], "queue_length": len(self.state.task_queue),
                 "current": self.state.workflow.goal.objective[:120]},
                "好，這件先記下了，手上這個做完就接著弄～",
            )

        # One message may pack several INDEPENDENT tasks ("建A檔案，再建B檔案") - split them so
        # each gets its own goal contract; the first runs now, the rest queue and auto-drain.
        # A single task's sequential steps ("建檔再寫入") is NOT split. Opt-in YUEYUE_TASK_SPLIT=1.
        objective = self._maybe_split_tasks(turn)
        if objective != turn.text:
            turn = TurnEnvelope(turn.chat_id, objective, TurnMode.TASK, turn.message_id)
        return self._start_task(turn, tool_callback)

    def _maybe_split_tasks(self, turn: TurnEnvelope) -> str:
        """Return the FIRST objective to run now; enqueue any additional independent tasks. Returns
        turn.text unchanged for a single task. A cheap conjunction prefilter gates the model call."""
        if env_value("YUEYUE_TASK_SPLIT") != "1":
            return turn.text
        text = turn.text
        # Cheap conjunction prefilter - only a plausibly-multi message pays for the split call.
        # Loose is fine: the model does the real judgment and returns a single element for one task.
        if not any(m in text for m in ("然後", "然后", "再", "還有", "还有", "順便", "顺便",
                                       "接著", "接着", "以及", "；", ";", "另外", "同時", "同时")):
            return turn.text
        parts = self._split_objectives(text)
        if len(parts) <= 1:
            return turn.text
        state = copy.deepcopy(self.state)
        state.task_queue = [*state.task_queue, *(p[:500] for p in parts[1:])]
        self._replace_state(state, "task_queue.split", turn.turn_id)
        self._emit("task.split", turn.turn_id, {"count": len(parts), "parts": parts[:6]})
        return parts[0]

    def _split_objectives(self, text: str) -> list[str]:
        prompt = (
            "把主人這句話拆成幾件『互不相關、可以各自獨立完成』的任務。"
            "同一件事的多個步驟（例如：建立檔案然後寫入內容、打開網頁再點按鈕）算『一件』，不要拆。"
            "只有像『數一下檔案，順便查個天氣』或『建 A 檔，再建 B 檔』這種真正無關的才拆。"
            '只回 JSON 陣列，每個元素是一句完整、可獨立執行的任務描述（保留主人原本的意思）。'
            "只有一件事就回單元素陣列。\n主人說：" + text
        )
        try:
            response = self.provider.chat(
                [{"role": "user", "content": prompt}], [], model=_chat_voice_model()
            )
            import json as _json

            match = re.search(r"\[.*\]", str(getattr(response, "content", "") or ""), re.S)
            if not match:
                return [text]
            parts = _json.loads(match.group(0))
            cleaned = [str(p).strip() for p in parts if isinstance(p, (str, int, float)) and str(p).strip()]
            return cleaned[:5] if len(cleaned) >= 2 else [text]
        except Exception:
            return [text]

    _OPENER_FORBIDDEN = (
        "系統", "系统", "初始化", "ai", "模型", "workflow", "工作流", "記憶", "记忆", "清空",
        "上線", "上线", "重啟", "重启", "載入", "加载", "程式", "程序", "指令", "tool",
    )

    def _opener_leaks_meta(self, text: str) -> bool:
        lowered = str(text or "").casefold()
        return any(word in lowered for word in self._OPENER_FORBIDDEN)

    def compose_opener(self) -> str:
        """Model-composed first-contact icebreaker (owner request 2026-07-16).

        A fresh companion that sits silent until spoken to feels dead; YueYue opens warmly in her
        own voice to set the tone and give 主人 an easy entry point. Fully generated - never a
        canned line - and validated for register/internal-term leaks. Returns "" if it cannot
        produce a clean opener (then nothing is sent; the owner just messages first, as before).
        The caller decides WHEN this fires (once per fresh relationship)."""
        instruction = (
            "這是你和主人的第一次見面，你的記憶是全新的。你主動先開口，自然地打個招呼、破個冰。"
            "溫柔、清新、帶一點點你自己的小俏皮，一到兩句就好。可以順口起個輕鬆的小話題、或問一句"
            "讓主人好接話。不要提到系統、AI、模型、初始化、記憶被清空這類話，就像一個剛認識、但已經"
            "有點自來熟的可愛女孩子在跟主人搭話。"
        )
        prompt = instruction
        for _ in range(2):
            try:
                response = self.provider.chat(
                    [
                        {"role": "system", "content": self.context.system_prompt(TurnMode.CHAT)},
                        {"role": "user", "content": prompt},
                    ],
                    [],
                    model=_chat_voice_model(),
                )
                reply = _drop_trailing_full_stop(to_traditional_script(_clean_reply(response.content)))
            except Exception:
                return ""
            if (
                reply
                and len(reply) <= 120
                and not voice_register_violation(reply)
                and not self._opener_leaks_meta(reply)
            ):
                return reply
            prompt = instruction + "\n（你剛才那句不合適，重寫一句更自然、更短、字感乾淨的開場白）"
        return ""

    def _chat_turn(self, turn: TurnEnvelope) -> str:
        # ROADMAP P6 v2 (native tool calling, the Claude architecture): every chat turn the model
        # sees the FULL skill catalog as function tools and decides itself whether to reach for
        # one - "冷不冷" can call weather, "幫我記一下" can call notes, plain chatter calls
        # nothing. No keyword routing, no separate router call. Opt-in YUEYUE_SKILLS=1.
        tools = []
        if turn.mode == TurnMode.CHAT and env_value("YUEYUE_SKILLS") == "1":
            try:
                tools = skill_tools()
            except Exception:
                tools = []
        messages = self.context.compile_turn(turn)
        # A chat turn arriving while a task waits for approval MUST know that fact, or the model
        # invents an answer and contradicts itself (live 2026-07-20: said "沒有等待中的任務" one
        # minute after asking for permission, then "啊我看錯了"). Deterministic ground truth beats
        # hoping the model guesses right.
        pending_note = self._pending_task_note()
        if pending_note:
            messages.append({"role": "system", "content": pending_note})
        try:
            response = self.provider.chat(messages, tools, model=_chat_voice_model())
            if tools and getattr(response, "tool_calls", None):
                reply = self._run_chat_skills(turn, messages, response)
            else:
                reply = _clean_reply(response.content) or "我在呢，主人。"
        except Exception:
            reply = "我這邊剛剛斷了一下，不過還在。你再說一次就好。"
        if turn.mode in {TurnMode.CHAT, TurnMode.SOCIAL}:
            reply = self._apply_social_chat_reply_policy(turn.text, reply)
            reply = _drop_trailing_full_stop(reply)
            reply = self._deduplicate_emoji_against_recent(turn.chat_id, reply)
            if owner_script_is_simplified_with_history(turn.text, self.context.short_context, turn.chat_id):
                reply = to_simplified_script(reply)
        self.context.remember(turn, reply)
        self._emit("turn.replied", turn.turn_id, {"mode": turn.mode.value, "reply": reply[:500]})
        return reply

    def _pending_task_note(self) -> str:
        """Ground truth about the currently-awaiting task, injected into chat turns so YueYue can
        never contradict her own permission request."""
        workflow = self.state.workflow
        if not workflow or workflow.status in {WorkflowStatus.COMPLETED, WorkflowStatus.CANCELLED}:
            return ""
        pending = self.state.permission.pending_action
        lines = [
            "### 目前狀態（事實，不可否認）",
            f"- 有一個任務進行中：「{workflow.goal.objective[:120]}」（狀態 {workflow.status.value}）",
        ]
        if pending:
            lines.append(f"- 這一步在等主人同意才能做：{pending.tool_name}")
            lines.append("- 回答完主人的問題後，自然地提醒他這件事還等著他說「可以」。")
        if self.state.task_queue:
            lines.append(f"- 另外還有 {len(self.state.task_queue)} 件排隊中：" + "；".join(
                f"「{q[:40]}」" for q in self.state.task_queue[:3]))
        return "\n".join(lines)

    def _run_chat_skills(self, turn: TurnEnvelope, messages: list[dict[str, Any]], response: Any) -> str:
        """Execute the model's chosen skill calls, then let it weave the results into its own
        reply (one bounded round - chat skills are quick lookups/actions, not agentic loops)."""
        messages = [*messages, _assistant_tool_message(response)]
        ctx = SkillContext(chat_id=turn.chat_id, now=time.time())
        for call in response.tool_calls[:4]:
            name = str(call.get("name") or "")
            outcome = execute_skill(name, dict(call.get("arguments") or {}), ctx)
            self._emit(
                "skill.used", turn.turn_id, {"skill": name, "ok": outcome.ok, "note": outcome.note[:200]}
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or name),
                    "name": name,
                    "content": outcome.note,
                }
            )
        messages.append(
            {
                "role": "system",
                "content": "把上面工具結果自然地講給主人聽，用你自己的語氣，一到兩句，別像系統通知。",
            }
        )
        final = self.provider.chat(messages, [], model=_chat_voice_model())
        return _clean_reply(final.content) or "我在呢，主人。"

    def compose_reminder_fire(self, text: str) -> str:
        """Compose the owner-facing line when a scheduled reminder fires, in persona. Deterministic
        fallback keeps a firing reminder from ever silently failing to deliver its content."""
        instruction = (
            "現在是你之前答應主人要提醒他的時間到了。自然、親切地提醒主人這件事，一到兩句，"
            "帶點你自己的語氣。要提醒的事：" + str(text)
        )
        try:
            response = self.provider.chat(
                [
                    {"role": "system", "content": self.context.system_prompt(TurnMode.CHAT)},
                    {"role": "user", "content": instruction},
                ],
                [],
                model=_chat_voice_model(),
            )
            reply = _drop_trailing_full_stop(to_traditional_script(_clean_reply(response.content)))
            if reply and not voice_register_violation(reply):
                return reply
        except Exception:
            pass
        return f"主人，時間到囉，該「{text}」了"

    def _post_completion_veto(self, workflow: WorkflowState) -> str:
        """Return a veto reason when the outputs plainly do not answer the objective, else "".
        Opt-in (YUEYUE_TASK_POSTCHECK=1): one cheap chat-model call per completed task, live path
        only - scripted test providers never see it. Errors and ambiguity always allow."""
        if env_value("YUEYUE_TASK_POSTCHECK") != "1":
            return ""
        # Never consume a scripted test provider's queued responses (the .env opt-in leaks into
        # tests via env_value); the check is only meaningful against the real provider anyway.
        if type(self.provider).__name__ != "SiliconFlowProvider":
            return ""
        try:
            prompt = (
                "任務目標與提取到的結果如下。結果是否真的回答了目標要的東西？"
                "只回 YES，或 NO: 一句原因（結果空泛/答非所問/缺了目標要求的部分才算 NO；"
                "格式醜但內容對就是 YES）。\n"
                + json.dumps(
                    {"objective": workflow.goal.objective, "outputs": workflow.outputs},
                    ensure_ascii=False,
                )
            )
            response = self.provider.chat(
                [{"role": "user", "content": prompt}], [], model=_chat_voice_model()
            )
            verdict = str(getattr(response, "content", "") or "").strip()
            if verdict.upper().startswith("NO"):
                return verdict[:160]
        except Exception:
            pass
        return ""

    def maybe_distill_memory(self, chat_id: str, every: int = 12) -> bool:
        """ROADMAP P2 write path: every N chat turns, distill the recent window into long-term
        memory (one episode + grounded facts). Called from the LIVE Telegram path (main.py) after
        the reply is dispatched - deliberately NOT from _chat_turn, so scripted-provider tests
        never get a surprise distill call eating their queued responses, and the owner never
        waits on distillation latency. Failures are silent - memory is an enhancement, never a
        chat-breaking dependency. Explicit opt-in via YUEYUE_LTM=1."""
        if env_value("YUEYUE_LTM") != "1":
            return False
        self._turns_since_distill += 1
        if self._turns_since_distill < every:
            return False
        self._turns_since_distill = 0
        try:
            window = [
                (item.role, item.text) for item in self.context.short_context.recent(chat_id, every + 4)
            ]
            date = time.strftime("%Y-%m-%d")
            for entry in self.memory_distiller.distill(window, date):
                self.memory.add(entry)
                self._emit("memory.distilled", "", {"kind": entry.kind, "text": entry.text[:200]})
            return True
        except Exception:
            return False

    def _deduplicate_emoji_against_recent(self, chat_id: str, reply: str) -> str:
        """Strip any emoji from this reply that already appeared in a recent assistant reply,
        so a single emoji (the owner disliked 😾 landing on every line) cannot become a tic.
        Guidance alone can't fix this - the model doesn't reliably recall last turn's emoji -
        so the mechanical rule lives here."""
        emojis = _EMOJI_RE.findall(reply)
        if not emojis:
            return reply
        recent_emojis: set[str] = set()
        for turn in self.context.short_context.recent(chat_id, 2):
            if turn.role == "assistant":
                recent_emojis.update(_EMOJI_RE.findall(turn.text))
        repeats = {e for e in emojis if e in recent_emojis}
        if not repeats:
            return reply
        for emoji in repeats:
            reply = reply.replace(emoji, "")
        # Tidy any orphaned space/variation-selector left behind.
        reply = re.sub(r"️", "", reply)
        reply = re.sub(r"[ \t]{2,}", " ", reply).strip()
        return reply

    def _apply_social_chat_reply_policy(self, owner_text: str, reply: str) -> str:
        # Simplified leaks are mechanically repairable - fix them BEFORE gating instead of
        # burning a regeneration (and possibly the canned fallback) on them. If the owner is
        # typing Simplified, the mirroring step after this converts the reply back anyway.
        reply = to_traditional_script(reply)
        if not _chat_reply_violates_social_policy(reply, owner_text):
            return reply
        # Most violations are a pure length overrun (the model's own reaction was on-topic, just a
        # line or two too long) rather than banned/leaked content. Prefer trimming the model's real
        # answer over discarding it for a generic canned line the owner did not actually ask about -
        # canned fallback text is a last resort, not the default outcome of going one line over.
        for max_lines in (2, 1):
            truncated = _truncate_chat_reply(reply, max_lines)
            if truncated and not _chat_reply_violates_social_policy(truncated, owner_text):
                return truncated
        # Truncation alone didn't fix it (e.g. the violation is banned/leaked wording, not just
        # length) - ask the model to rewrite its own reply within the constraint instead of
        # silently replacing its real answer with a scripted line.
        regenerated = self._regenerate_compliant_chat_reply(owner_text, reply)
        if regenerated and not _chat_reply_violates_social_policy(regenerated, owner_text):
            return regenerated
        # Trace the surrender so live fallbacks are diagnosable (what did the model try to say?).
        self._emit(
            "chat.fallback_used",
            "",
            {"owner_text": owner_text[:200], "rejected_reply": reply[:300]},
        )
        return _social_chat_fallback(owner_text)

    def _regenerate_compliant_chat_reply(self, owner_text: str, reply: str) -> str:
        prompt = (
            "你剛剛的回覆太長、用錯了字感（台味語尾/粵語口語字/簡體字），"
            "或裡面提到了不該對主人講的內部詞（例如流程、系統、內部規則）。"
            "請用你自己的語氣，重新回一句自然的短回覆，一到兩句話，不要換行超過一次，"
            "不要提到流程、系統、內部規則、任何開發或除錯用語。"
            "用香港書面繁體+內地網聊語感，別用「喔/喲/耶」收尾，別寫「嘅/喺/㗎/唔/冇」這類粵語字。\n"
            f"主人剛剛說：{owner_text}\n"
            f"你原本想回：{reply}"
        )
        try:
            response = self.provider.chat(
                [
                    {"role": "system", "content": self.context.system_prompt(TurnMode.CHAT)},
                    {"role": "user", "content": prompt},
                ],
                [],
                model=_chat_voice_model(),
            )
            return _clean_reply(response.content)
        except Exception:
            return ""

    def _screen_observe_turn(self, turn: TurnEnvelope, tool_callback: Callable | None) -> str:
        """Deterministic screen-describe turn: capture, then actually look, then say what was seen.

        The owner asking "what's on the screen" must never get back a bare "captured it" -
        the capture->analyze chain is enforced here instead of being left to the model's
        own tool choice, and the reply must carry the visual content or an honest failure.
        """
        self._emit("screen_observe.started", turn.turn_id, {"text": turn.text[:300]})
        capture = self._execute_tool("capture_screen", {}, tool_callback)
        if capture.status != "ok":
            self._emit(
                "screen_observe.failed",
                turn.turn_id,
                {"stage": "capture", "error": str(capture.error or capture.message)[:400]},
            )
            reply = self._compose_owner_voice(
                "screen_capture_failed",
                {"owner_question": turn.text, "error": capture.message},
                "我剛剛想看屏幕，但截圖這一步失敗了，所以我還不知道畫面上是什麼。等下再叫我看一次。",
            )
            return self._finish_screen_observe(turn, reply)
        data = capture.data if isinstance(capture.data, dict) else {}
        screenshot_path = str(data.get("path") or "")
        window_title = str(data.get("window_title") or "")
        location_note = self._describe_artifact_location(screenshot_path)
        analysis = self._execute_tool(
            "analyze_media", {"file_path": screenshot_path, "prompt": SCREEN_DESCRIBE_PROMPT}, tool_callback
        )
        if analysis.status != "ok":
            self._emit(
                "screen_observe.failed",
                turn.turn_id,
                {"stage": "analyze", "error": str(analysis.error or analysis.message)[:400]},
            )
            reply = self._compose_owner_voice(
                "screen_analysis_failed",
                {
                    "owner_question": turn.text,
                    "window_title": window_title,
                    "screenshot_location": location_note,
                    "error": analysis.message,
                },
                f"截圖是拍到了（存在{location_note}），但我看圖那一步失敗了，"
                "所以還說不出畫面內容。你可以等一下再讓我看一次，或叫我直接把截圖傳給你。",
            )
            return self._finish_screen_observe(turn, reply)
        analysis_data = analysis.data if isinstance(analysis.data, dict) else {}
        summary = str(analysis_data.get("summary") or "").strip()
        self._emit(
            "screen_observe.analyzed",
            turn.turn_id,
            {"window_title": window_title, "summary": summary[:800], "screenshot_path": screenshot_path},
        )
        fallback = f"我看了一眼屏幕：{summary}"
        if window_title:
            fallback = f"我看了一眼屏幕，目前開著的窗口是「{window_title}」。{summary}"
        reply = self._compose_owner_voice(
            "screen_observed",
            {
                "owner_question": turn.text,
                "window_title": window_title,
                "screen_content": summary,
                "screenshot_location": location_note,
                "note": "把 screen_content 的重點講給主人聽；截圖沒有傳出去，主人開口才傳。",
            },
            fallback,
        )
        if not _reply_reflects_content(reply, summary):
            reply = fallback
        return self._finish_screen_observe(turn, reply)

    def _finish_screen_observe(self, turn: TurnEnvelope, reply: str) -> str:
        if owner_script_is_simplified_with_history(turn.text, self.context.short_context, turn.chat_id):
            reply = to_simplified_script(reply)
        self.context.remember(turn, reply)
        self._emit("turn.replied", turn.turn_id, {"mode": "screen_observe", "reply": reply[:500]})
        return reply

    def _describe_artifact_location(self, path: str) -> str:
        if not path:
            return "內部觀察紀錄"
        try:
            relative = Path(path).resolve().relative_to(self.root)
            return str(relative)
        except (OSError, ValueError):
            return str(path)

    def _start_task(self, turn: TurnEnvelope, tool_callback: Callable | None) -> str:
        try:
            planned = self.planner.plan(turn.text)
            contract_issues = self.workflow_engine.contract_issues(planned.steps)
            if contract_issues:
                replan = getattr(self.planner, "replan", None)
                if not callable(replan):
                    raise ValueError("; ".join(contract_issues))
                planned = replan(turn.text, contract_issues)
                contract_issues = self.workflow_engine.contract_issues(planned.steps)
                if contract_issues:
                    raise ValueError("; ".join(contract_issues))
        except Exception as exc:
            self._emit("planner.failed", turn.turn_id, {"error": str(exc)[:500]})
            return self._compose_owner_voice(
                "planner_failed",
                {"owner_request": turn.text},
                "我沒能把這個任務整理成可靠步驟，所以先不亂動。你稍後再讓我試一次。",
            )
        workflow = self.workflow_engine.create(planned.goal, planned.steps)
        state = copy.deepcopy(self.state)
        state.workflow = workflow
        workflow.status = WorkflowStatus.RUNNING
        if workflow.current_step():
            workflow.current_step().status = StepStatus.RUNNING
        self._replace_state(state, "workflow.started", turn.turn_id)
        return self._run_workflow(turn, tool_callback)

    def _handle_permission_reply(self, turn: TurnEnvelope, approval: str, tool_callback: Callable | None) -> str:
        state = copy.deepcopy(self.state)
        if approval == "deny":
            denied_action = state.permission.pending_action
            if state.workflow:
                state.workflow.status = WorkflowStatus.CANCELLED
            self.permission_controller.clear(state.permission)
            self._replace_state(state, "permission.denied", turn.turn_id)
            return self._compose_owner_voice(
                "permission_denied",
                {"tool": denied_action.tool_name if denied_action else ""},
                "好，這一步不做了。我不會再繼續點。",
            )
        pending_action = copy.deepcopy(state.permission.pending_action)
        decision = self.permission_controller.apply_reply(state.permission, turn.text)
        if decision == "none":
            return self._compose_owner_voice(
                "no_pending_permission", {}, "我沒有找到正在等你確認的動作。你直接說要我做什麼就好。"
            )
        if state.workflow:
            self.workflow_engine.approve(state.workflow)
        self._replace_state(state, "permission.granted", turn.turn_id)
        if pending_action and self.state.workflow:
            workflow = copy.deepcopy(self.state.workflow)
            permission = copy.deepcopy(self.state.permission)
            if not self.permission_controller.permits(permission, pending_action):
                return self._compose_owner_voice(
                    "permission_mismatch",
                    {"tool": pending_action.tool_name},
                    "這一步的授權沒有正確接上，我先停住，沒有繼續操作。",
                )
            result = self._execute_tool(pending_action.tool_name, pending_action.arguments, tool_callback)
            evidence = _evidence_from_result(
                workflow, pending_action.tool_name, result, pending_action.arguments
            )
            self.workflow_engine.add_evidence(workflow, evidence)
            self.workflow_engine.verify(workflow)
            replayed_state = copy.deepcopy(self.state)
            replayed_state.workflow = workflow
            replayed_state.permission = permission
            self._replace_state(
                replayed_state,
                "permission.replayed",
                turn.turn_id,
                {
                    "tool": pending_action.tool_name,
                    "status": result.status,
                    "evidence_id": evidence.evidence_id,
                },
            )
        resumed = TurnEnvelope(
            turn.chat_id,
            self.state.workflow.goal.objective if self.state.workflow else turn.text,
            TurnMode.TASK,
            turn.message_id,
        )
        return self._run_workflow(resumed, tool_callback)

    def _run_workflow(self, turn: TurnEnvelope, tool_callback: Callable | None) -> str:
        transcript: list[dict[str, Any]] = []
        no_progress = 0
        last_progress = ""
        last_observation_signature = ""
        last_call_signature = ""
        for _ in range(self.max_iterations):
            workflow = copy.deepcopy(self.state.workflow)
            if not workflow:
                return self._compose_owner_voice("workflow_state_missing", {}, "任務狀態不見了，我先停下，避免繼續誤操作。")
            decision = self.workflow_engine.verify(workflow)
            if decision.goal_satisfied:
                # ROADMAP P3: post-completion sanity check - a cheap model call asks whether the
                # extracted outputs actually ANSWER the objective (catches "答非所問" completions
                # where structurally-valid outputs miss the point). Confident NO -> honest block
                # with the reason, never a fake success; any ambiguity/error -> allow.
                veto = self._post_completion_veto(workflow)
                if veto:
                    self.workflow_engine.block(workflow, f"完成前自查沒過：{veto}")
                    state = copy.deepcopy(self.state)
                    state.workflow = workflow
                    self.permission_controller.clear(state.permission)
                    self._replace_state(state, "workflow.blocked", turn.turn_id)
                    return self._compose_blocked_reply(workflow)
                self.permission_controller.clear(self.state.permission)
                state = copy.deepcopy(self.state)
                state.workflow = workflow
                state.permission = PermissionState()
                self._replace_state(state, "workflow.completed", turn.turn_id)
                return self._compose_result_reply(workflow)

            state = copy.deepcopy(self.state)
            state.workflow = workflow
            self._replace_state(state, "workflow.verified", turn.turn_id)
            allowed = self.workflow_engine.allowed_tools(workflow)

            # report_result is a universal, side-effect-free escape hatch for submitting a value
            # the model derived from observations (a count/sum/extracted field), so a
            # derive-and-report task can complete instead of re-observing into the no-progress
            # guard. Only offered once at least one observation has succeeded - the model must
            # observe first, then report a value grounded in that evidence, never invent one.
            # Injected BEFORE the empty-tools check: a final tools-less "reply" step must be able
            # to finish the goal, not be declared a dead end (live 2026-07-21: the file WAS created,
            # the act step verified, then the reply step blocked with "No safe capability").
            has_observed = any(item.status == "ok" for item in workflow.evidence)
            if has_observed and ToolCatalogV3.REPORT_RESULT not in allowed:
                allowed = [*allowed, ToolCatalogV3.REPORT_RESULT]

            if not allowed:
                self.workflow_engine.block(workflow, "No safe capability is available for the current incomplete step.")
                state = copy.deepcopy(self.state)
                state.workflow = workflow
                self.permission_controller.clear(state.permission)
                self._replace_state(state, "workflow.blocked", turn.turn_id)
                return self._compose_blocked_reply(workflow)

            conversation = self.context.compile_turn(turn, workflow)
            # Render executed-tool evidence into the model's view. Without this, a permission
            # round-trip resumed _run_workflow with an empty transcript, so the model never saw
            # the result of the action the owner just approved (e.g. the count sitting in
            # execute_python stdout) and re-requested the same tool forever - each retry burning
            # another owner approval. The instruction "the answer is already in a tool result you
            # have" is only actionable if the results are actually in view.
            evidence_note = _evidence_note(workflow)
            if evidence_note:
                conversation.append({"role": "system", "content": evidence_note})
            conversation.extend(transcript[-12:])
            conversation.append({"role": "system", "content": self._execution_instruction(workflow, allowed)})
            try:
                response = self.provider.chat(
                    conversation,
                    self.tools.list(allowed),
                    tool_choice="required",
                    reasoning_effort=_task_reasoning_effort(),
                )
            except Exception as exc:
                self._emit("provider.error", turn.turn_id, {"error": str(exc)[:500]})
                return _provider_failure_reply(exc)
            if not response.tool_calls:
                no_progress += 1
                transcript.append({"role": "assistant", "content": response.content})
                transcript.append(
                    {
                        "role": "system",
                        "content": "The goal is incomplete. Choose one allowed tool that directly advances the current step, or state a concrete blocker.",
                    }
                )
                if no_progress >= 2:
                    self.workflow_engine.block(
                        workflow, "The model produced no executable action for the current step."
                    )
                    state = copy.deepcopy(self.state)
                    state.workflow = workflow
                    self.permission_controller.clear(state.permission)
                    self._replace_state(state, "workflow.blocked", turn.turn_id)
                    return self._compose_blocked_reply(workflow)
                continue

            transcript.append(_assistant_tool_message(response))
            for call in response.tool_calls:
                name = str(call.get("name") or "")
                arguments = self._prepare_tool_arguments(name, dict(call.get("arguments") or {}), workflow)
                if name not in allowed:
                    self.workflow_engine.block(workflow, f"The model requested unavailable capability: {name}.")
                    state = copy.deepcopy(self.state)
                    state.workflow = workflow
                    self.permission_controller.clear(state.permission)
                    self._replace_state(state, "workflow.blocked", turn.turn_id)
                    return self._compose_blocked_reply(workflow)
                if name == ToolCatalogV3.REPORT_RESULT:
                    grounded, why = _report_value_grounded(arguments, workflow)
                    if not grounded:
                        no_progress += 1
                        if no_progress >= 3:
                            self.workflow_engine.block(
                                workflow, f"report_result kept submitting unverifiable values: {why}."
                            )
                            state = copy.deepcopy(self.state)
                            state.workflow = workflow
                            self.permission_controller.clear(state.permission)
                            self._replace_state(state, "workflow.blocked", turn.turn_id)
                            return self._compose_blocked_reply(workflow)
                        transcript.append(
                            {
                                "role": "system",
                                "content": (
                                    f"report_result rejected: {why}. Only report values that are "
                                    "visible in the tool results above. If the answer is not there "
                                    "yet, run an allowed tool to obtain it first."
                                ),
                            }
                        )
                        break
                step = workflow.current_step()
                action = ActionEnvelope(
                    name, arguments, step.step_id if step else "", step.risk_level if step else "low"
                )
                permission = copy.deepcopy(self.state.permission)
                if self.permission_controller.needs_permission(
                    name, arguments
                ) and not self.permission_controller.permits(permission, action):
                    state = copy.deepcopy(self.state)
                    self.permission_controller.request(state.permission, action)
                    state.workflow.status = WorkflowStatus.AWAITING_PERMISSION
                    state.workflow.updated_at = time.time()
                    if state.workflow.current_step():
                        state.workflow.current_step().status = StepStatus.AWAITING_PERMISSION
                    self._replace_state(state, "permission.requested", turn.turn_id)
                    return self._compose_owner_voice(
                        "permission_requested",
                        {
                            "tool": name,
                            "arguments": {
                                key: (str(value)[:200] if isinstance(value, str) else value)
                                for key, value in arguments.items()
                            },
                            "step": step.name if step else "",
                            "objective": workflow.goal.objective,
                        },
                        "這一步需要你點頭，我才能繼續。你回「可以」，我就接著剛才的任務做。",
                    )

                result = self._execute_tool(name, arguments, tool_callback)
                evidence = _evidence_from_result(workflow, name, result, arguments)
                self.workflow_engine.add_evidence(workflow, evidence)
                semantic_evidence = self._semantic_verify_action(workflow)
                if semantic_evidence:
                    self.workflow_engine.add_evidence(workflow, semantic_evidence)
                decision = self.workflow_engine.verify(workflow)
                state = copy.deepcopy(self.state)
                state.workflow = workflow
                state.permission = permission
                self._replace_state(
                    state,
                    "tool.result",
                    turn.turn_id,
                    {"tool": name, "status": result.status, "evidence_id": evidence.evidence_id},
                )
                transcript.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or name),
                        "name": name,
                        "content": _tool_content(result, _evidence_terms(workflow)),
                    }
                )
                if decision.goal_satisfied:
                    break
                progress = self.workflow_engine.progress_signature(workflow)
                observation_signature = evidence.observation_revision if name in OBSERVATION_SOURCES else ""
                # Repeating the SAME call with the SAME arguments is never progress, even though
                # each attempt appends fresh evidence and therefore changes the progress signature.
                # Without this the loop re-ran one command for 7 minutes (live 2026-07-21).
                call_signature = f"{name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)[:400]}"
                repeated_call = call_signature == last_call_signature
                if repeated_call or progress == last_progress or (
                    observation_signature and observation_signature == last_observation_signature
                ):
                    no_progress += 1
                else:
                    no_progress = 0
                last_call_signature = call_signature
                last_progress = progress
                if observation_signature:
                    last_observation_signature = observation_signature
                if no_progress == 1:
                    transcript.append(
                        {
                            "role": "system",
                            "content": "No progress was observed. Do not repeat the same observation. Use a different allowed action or explain the blocker.",
                        }
                    )
                elif no_progress >= 2:
                    self.workflow_engine.block(
                        workflow, "No state change was observed after two duplicate observations."
                    )
                    state = copy.deepcopy(self.state)
                    state.workflow = workflow
                    self.permission_controller.clear(state.permission)
                    self._replace_state(state, "workflow.blocked", turn.turn_id)
                    return self._compose_blocked_reply(workflow)
        workflow = copy.deepcopy(self.state.workflow)
        if workflow:
            self.workflow_engine.block(
                workflow, "The bounded execution budget was exhausted before the goal was verified."
            )
            state = copy.deepcopy(self.state)
            state.workflow = workflow
            self.permission_controller.clear(state.permission)
            self._replace_state(state, "workflow.blocked", turn.turn_id)
            return self._compose_blocked_reply(workflow)
        return self._compose_owner_voice("execution_budget_exhausted", {}, "我沒有取得足夠證據，所以沒有假裝完成。")

    def _execute_tool(self, name: str, arguments: dict[str, Any], tool_callback: Callable | None) -> V3ToolResult:
        result = self.tools.execute(name, arguments, tool_callback)
        if result.status == "error" and result.retryable:
            # Immediate retries mostly re-hit the same transient condition; a short pause
            # is what actually lets flaky network/UI states clear.
            time.sleep(0.8)
            return self.tools.execute(name, arguments, tool_callback)
        return result

    def _prepare_tool_arguments(
        self, name: str, arguments: dict[str, Any], workflow: WorkflowState | None = None
    ) -> dict[str, Any]:
        prepared = dict(arguments or {})
        if name != "analyze_media":
            return prepared
        supplied = str(prepared.get("file_path") or "").strip()
        candidates = []
        if supplied:
            supplied_path = Path(supplied)
            candidates.extend([supplied_path, self.root / supplied_path, self.root / "workspace" / supplied_path])
        for candidate in candidates:
            try:
                if candidate.resolve().is_file():
                    prepared["file_path"] = str(candidate.resolve())
                    return prepared
            except OSError:
                continue
        active = workflow or self.state.workflow
        if active:
            for evidence in reversed(active.evidence):
                if evidence.source != "capture_screen" or evidence.status != "ok":
                    continue
                for artifact in reversed(evidence.artifacts):
                    path = Path(artifact)
                    try:
                        resolved = path.resolve()
                    except OSError:
                        continue
                    if resolved.is_file() and resolved.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}:
                        prepared["file_path"] = str(resolved)
                        return prepared
        return prepared

    def _semantic_verify_action(self, workflow: WorkflowState) -> ExecutionEvidence | None:
        step = workflow.current_step()
        if not step or step.kind != "act" or step.status != StepStatus.AWAITING_OBSERVATION:
            return None
        step_evidence = [item for item in workflow.evidence if item.step_id == step.step_id and item.status == "ok"]
        action_indexes = [
            index
            for index, item in enumerate(step_evidence)
            if item.source in {"focus_window", "click_ui_element", "click_screen", "press_hotkey", "type_keyboard"}
        ]
        if not action_indexes:
            return None
        observations = [item for item in step_evidence[action_indexes[-1] + 1 :] if item.source in OBSERVATION_SOURCES]
        if not observations:
            return None
        revision = observations[-1].observation_revision
        if any(
            item.source == "semantic_verifier" and item.observation_revision == revision
            for item in step_evidence[action_indexes[-1] + 1 :]
        ):
            return None
        submit = AgentTool(
            "submit_step_verification",
            "Judge whether the post-action evidence proves the current step condition.",
            lambda **_: ToolResult("ok", "accepted"),
            {
                "type": "object",
                "properties": {
                    "condition_satisfied": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["condition_satisfied", "reason"],
            },
            False,
        )
        evidence_payload = [
            {
                "source": item.source,
                "summary": item.summary[:1200],
                "facts": _compact_facts(item.facts, [step.name, step.done_condition]),
                "revision": item.observation_revision,
            }
            for item in observations[-3:]
        ]
        prompt = (
            "Verify only the resulting UI state. A menu item or button label is not proof that a page/panel is open. "
            "Return condition_satisfied=true only when the post-action evidence directly proves the done condition."
        )
        try:
            response = self.provider.chat(
                [
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"step": step.name, "done_condition": step.done_condition, "evidence": evidence_payload},
                            ensure_ascii=False,
                            default=str,
                        )[:12000],
                    },
                ],
                [submit],
                tool_choice="required",
                reasoning_effort=_task_reasoning_effort(),
            )
            call = next((item for item in response.tool_calls if item.get("name") == submit.name), None)
            arguments = dict(call.get("arguments") or {}) if call else {}
            if "condition_satisfied" not in arguments:
                return None
            return ExecutionEvidence(
                step.step_id,
                "semantic_verifier",
                "ok",
                str(arguments.get("reason") or "Semantic verification completed.")[:600],
                {"condition_satisfied": bool(arguments.get("condition_satisfied"))},
                observation_revision=revision,
            )
        except Exception as exc:
            self._emit("verifier.error", "", {"error": str(exc)[:500], "step_id": step.step_id})
            return None

    def _compose_plan_reply(self, workflow: WorkflowState) -> str:
        preview = "；".join(step.name for step in workflow.steps[:3])
        fallback = f"我會先{preview}。這串操作需要你點頭；回「可以」我就繼續，而且只在真的拿到結果後才收尾。"
        return self._compose_owner_voice(
            "permission_plan",
            {"objective": workflow.goal.objective, "steps": [step.name for step in workflow.steps]},
            fallback,
        )

    def _compose_result_reply(self, workflow: WorkflowState) -> str:
        values = [_display_value(value) for value in workflow.outputs.values()]
        fallback = f"找到了，結果是 {values[0]}。" if len(values) == 1 else "找到了：" + "；".join(values) + "。"
        return self._compose_owner_voice(
            "task_success",
            {
                "objective": workflow.goal.objective,
                "outputs": workflow.outputs,
                "execution_account": self._execution_account(workflow),
            },
            fallback,
        )

    def _compose_blocked_reply(self, workflow: WorkflowState) -> str:
        reason = workflow.verification.reason if workflow.verification else "沒有足夠證據繼續"
        fallback = f"我還沒取得你要的結果。已經確認的卡點是：{reason}。我先停下，避免繼續亂點。"
        return self._compose_owner_voice(
            "task_blocked",
            {
                "objective": workflow.goal.objective,
                "reason": reason,
                "outputs": workflow.outputs,
                "execution_account": self._execution_account(workflow),
            },
            fallback,
        )

    def _execution_account(self, workflow: WorkflowState) -> dict[str, Any]:
        """Compact 'what I actually did' record so the final reply can account for the work."""
        actions: list[str] = []
        artifacts: list[str] = []
        for evidence in workflow.evidence:
            if evidence.source in {"semantic_verifier"}:
                continue
            marker = f"{evidence.source}:{evidence.status}"
            if not actions or actions[-1] != marker:
                actions.append(marker)
            for artifact in evidence.artifacts:
                location = self._describe_artifact_location(str(artifact))
                if location not in artifacts:
                    artifacts.append(location)
        return {"actions_taken": actions[-12:], "artifacts": artifacts[:6]}

    def _compose_owner_voice(self, event: str, facts: dict[str, Any], fallback: str) -> str:
        prompt = (
            "Write one concise owner-facing YueYue reply in natural Traditional Chinese. "
            + VOICE_REGISTER_EN
            + " Use only the supplied facts. "
            "Do not mention runtime, policy, workflow, TaskGraph, internal tools, or hidden reasoning. "
            "Do not add recommendations that are not in the facts. "
            "If the facts include execution_account or screenshot/artifact locations, briefly account for what was "
            "actually done and where any files ended up, in plain owner language (e.g. 我截了圖看過、檔案存在哪) - "
            "never claim an action or file that is not in the facts. "
            "If the event name contains 'permission', you are ASKING the owner for permission before acting - "
            "phrase it as a request that genuinely needs their yes (explain what you want to do and why, using "
            "the facts), and never say you will do it, are doing it, or have done it.\n"
            + json.dumps({"event": event, "facts": facts}, ensure_ascii=False)
        )
        # One retry with a concrete critique before surrendering to the canned fallback: the
        # canned permission line repeating several times in one task (gap battery, live) was
        # exactly this function giving up after a single failed generation. The model usually
        # fixes a named violation on the second try; the fallback stays as the final net.
        critique = ""
        for _ in range(2):
            try:
                messages = [
                    {"role": "system", "content": self.context.system_prompt(TurnMode.TASK)},
                    {"role": "user", "content": prompt + critique},
                ]
                # Owner-voice lines are persona speech, not planning - they follow the chat
                # voice model so the register/tone stays consistent across chat and task turns.
                response = self.provider.chat(messages, [], model=_chat_voice_model())
                candidate = to_traditional_script(_clean_reply(response.content))
                if not candidate:
                    critique = "\nYour previous attempt was empty. Write the reply."
                    continue
                if _contains_internal_terms(candidate):
                    critique = (
                        "\nYour previous attempt leaked internal terms. Rewrite it in plain owner "
                        "language with no runtime/workflow/system words:\n" + candidate[:200]
                    )
                    continue
                # Register gate: task-voice replies had no output-side enforcement, which is how
                # spoken Cantonese (嘅/喺/㗎) leaked into a live task reply on 2026-07-12. The
                # fallbacks are all clean written Traditional, so falling back is always safe.
                violation = voice_register_violation(candidate)
                if violation:
                    critique = (
                        f"\nYour previous attempt violated the register ({violation}). Rewrite it in "
                        "standard written Traditional Chinese (香港書面繁體), no Taiwan-flavored final "
                        "particles, no spoken Cantonese, no Simplified:\n" + candidate[:200]
                    )
                    continue
                return candidate
            except Exception:
                break
        return fallback

    def _execution_instruction(self, workflow: WorkflowState, allowed: list[str]) -> str:
        step = workflow.current_step()
        return (
            "Execute exactly the current step. If you say you will click, call a click tool in the same response. "
            "Do not repeat an unchanged observation. Prefer click_ui_element when get_screen_ui exposes a named "
            "actionable element. Use click_screen only when no matching UI element exists, and only with absolute "
            "full-screen coordinates from the latest capture_screen artifact. "
            "If get_screen_ui fails or exposes no matching element, degrade to capture_screen followed by "
            "analyze_media on its artifact before declaring a blocker. "
            "When the requested answer is already present in a tool result you have (for example a "
            "number printed to stdout by execute_command/execute_python, or a value you counted or "
            "extracted from an observation), immediately call report_result with the output name and "
            "that value to finish - do NOT run another command to re-check or re-derive it. Running a "
            "second command when you already have the answer wastes the owner's approval and time.\n"
            f"Current step: {step.name if step else 'none'}\n"
            f"Done condition: {step.done_condition if step else ''}\n"
            f"Required facts: {', '.join(step.required_facts) if step else ''}\n"
            f"Allowed tools: {', '.join(allowed)}"
        )

    def _replace_state(self, state: RuntimeState, kind: str, turn_id: str, extra: dict[str, Any] | None = None) -> None:
        payload = {"state": asdict(state)}
        if extra:
            payload.update(extra)
        self._emit(kind, turn_id, payload)

    def _emit(self, kind: str, turn_id: str, payload: dict[str, Any]) -> None:
        event = RuntimeEvent(
            kind,
            self.state.session_id,
            turn_id=turn_id,
            workflow_id=self.state.workflow.workflow_id if self.state.workflow else "",
            payload=payload,
        )
        self.events.apply(event)

    def _record_provider_event(self, payload: dict[str, Any]) -> None:
        self._emit("provider.call", "", payload)

    def _reduce(self, state: RuntimeState, event: RuntimeEvent) -> RuntimeState:
        replacement = None
        if _may_replace_state(event.kind) and isinstance(event.payload.get("state"), dict):
            replacement = event.payload["state"]
        if replacement:
            state = runtime_state_from_dict(replacement)
        elif event.kind == "worker.evidence" and state.workflow:
            payload = event.payload
            workflow_id = str(payload.get("workflow_id") or event.workflow_id or "")
            step_id = str(payload.get("step_id") or "")
            current_step = state.workflow.current_step()
            if workflow_id == state.workflow.workflow_id and current_step and step_id == current_step.step_id:
                state = copy.deepcopy(state)
                evidence = ExecutionEvidence(
                    step_id=step_id,
                    source=str(payload.get("source") or "verifier_worker"),
                    status=str(payload.get("status") or "error"),
                    summary=str(payload.get("summary") or ""),
                    facts=dict(payload.get("facts") or {}),
                    artifacts=[str(item) for item in payload.get("artifacts") or []],
                    observation_revision=str(payload.get("observation_revision") or ""),
                )
                self.workflow_engine.add_evidence(state.workflow, evidence)
                self.workflow_engine.verify(state.workflow)
        state.updated_at = time.time()
        return state


def _evidence_note(workflow: WorkflowState, limit: int = 6) -> str:
    """Render the tail of the workflow's executed-tool evidence as a system note so the model can
    see what already ran and what it returned (transcript alone loses this across permission
    round-trips)."""
    entries = [item for item in workflow.evidence if item.source]
    if not entries:
        return ""
    lines = ["### Tool results so far (real executions - use these, do not re-run for the same answer)"]
    for item in entries[-limit:]:
        parts = [f"[{item.source}] {item.status}"]
        if item.summary:
            parts.append(str(item.summary)[:600])
        facts = {k: v for k, v in (item.facts or {}).items() if k not in {"revision"}}
        if facts:
            parts.append("facts=" + json.dumps(facts, ensure_ascii=False, default=str)[:500])
        lines.append("- " + " | ".join(parts))
    return "\n".join(lines)


def _report_value_grounded(arguments: dict[str, Any], workflow: WorkflowState) -> tuple[bool, str]:
    """Anti-fabrication gate for report_result: every reported value must be traceable to actual
    tool evidence. The prompt-side rule ('never invent one') is not enforcement - the battery
    showed the model reporting a filename that does not exist anywhere in its observations."""
    corpus_parts: list[str] = []
    for item in workflow.evidence:
        corpus_parts.append(str(item.summary or ""))
        if item.facts:
            corpus_parts.append(json.dumps(item.facts, ensure_ascii=False, default=str))
    corpus = re.sub(r"\s+", "", " ".join(corpus_parts)).casefold()
    if not corpus:
        return False, "no tool evidence exists yet; observe first"
    results = arguments.get("results")
    for entry in results if isinstance(results, list) else []:
        if not isinstance(entry, dict):
            continue
        value = re.sub(r"\s+", "", str(entry.get("value") if entry.get("value") is not None else "")).casefold()
        if not value:
            continue
        if value in corpus:
            continue
        # long/derived values: accept when the bulk of their meaningful tokens appear in evidence.
        # Ceiling, not floor: with 2 tokens a floor of int(1.4)=1 let "soul_core.md" pass because
        # the generic "md" token alone matched - exactly the fabrication this exists to reject.
        tokens = [t.casefold() for t in re.findall(r"[\w一-鿿]{2,}", str(entry.get("value"))) if len(t) >= 2]
        if tokens and sum(1 for t in tokens if t in corpus) >= math.ceil(len(tokens) * 0.7):
            continue
        return False, f"value {str(entry.get('value'))[:80]!r} does not appear in any tool result"
    return True, ""


def _evidence_from_result(
    workflow: WorkflowState, source: str, result: V3ToolResult, arguments: dict[str, Any] | None = None
) -> ExecutionEvidence:
    step = workflow.current_step()
    terms = [item.name for item in workflow.goal.requested_outputs]
    terms.extend(item.description for item in workflow.goal.requested_outputs)
    summary = _compact_result_text(result.message, terms)
    facts = copy.deepcopy(result.facts)
    # What an action actually did is real, groundable fact: write_file's result says only "File
    # written." with no echo of the content, so a model honestly reporting the content it just
    # wrote failed the grounding gate ("does not appear in any tool result"). Capture the salient
    # arguments of the executed call into evidence.
    for key in ("filename", "path", "content", "command", "code", "keyword", "directory", "url"):
        value = (arguments or {}).get(key)
        if value is not None and f"arg_{key}" not in facts:
            facts[f"arg_{key}"] = str(value)[:400]
    if isinstance(result.data, dict):
        for key in (
            "summary",
            "title",
            "description",
            "value",
            "percentage",
            "status",
            "window_id",
            "window_title",
            "snapshot_id",
            "revision",
            "ui_elements",
            "visible_text",
            # Command output IS the observation. Without these keys the actual result (e.g. the
            # version number sitting in stdout) never reached evidence at all - the model and the
            # output binding both saw only the generic "Command completed." message.
            "stdout",
            "stderr",
            "returncode",
        ):
            if key in result.data and key not in facts:
                facts[key] = result.data[key]
    elif isinstance(result.data, list) and result.data:
        # Some tools return their payload as a bare list (e.g. search_in_files match entries).
        # Without this the whole result vanished from evidence - the model saw only the generic
        # "Search completed." summary and the found filenames were never groundable/bindable.
        facts["results"] = json.dumps(result.data[:20], ensure_ascii=False, default=str)[:2000]
    revision = str(facts.get("revision") or facts.get("screenshot_id") or "")
    return ExecutionEvidence(
        step.step_id if step else "", source, result.status, summary, facts, result.artifacts, revision
    )


def _compact_result_text(text: str, terms: list[str], limit: int = 2400) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    keywords = set()
    for term in terms:
        keywords.update(re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,8}", term or ""))
    matches = [
        line for line in value.splitlines() if any(keyword.casefold() in line.casefold() for keyword in keywords)
    ]
    middle = "\n".join(matches[:20])
    return value[:700] + "\n...[middle omitted]...\n" + middle[:900] + "\n...[tail]...\n" + value[-700:]


def _tool_content(result: V3ToolResult, terms: list[str] | None = None) -> str:
    terms = [str(item) for item in terms or [] if str(item).strip()]
    payload = {
        "status": result.status,
        "message": _compact_result_text(result.message, terms, 5000),
        "facts": _compact_facts(result.facts, terms),
        "artifacts": result.artifacts,
        "error": result.error,
        "error_category": result.error_category,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _evidence_terms(workflow: WorkflowState) -> list[str]:
    step = workflow.current_step()
    terms = [item.name for item in workflow.goal.requested_outputs]
    terms.extend(item.description for item in workflow.goal.requested_outputs)
    if step:
        terms.extend(step.required_facts)
        terms.extend((step.name, step.done_condition))
    return list(dict.fromkeys(item for item in terms if item))


def _compact_facts(facts: dict[str, Any], terms: list[str]) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key, value in facts.items():
        if key in {"ui_elements", "visible_text"} and isinstance(value, list):
            compacted[key] = _compact_sequence(value, terms)
        elif key == "ui_snapshot" and isinstance(value, dict):
            snapshot = dict(value)
            if isinstance(snapshot.get("elements"), list):
                snapshot["elements"] = _compact_sequence(snapshot["elements"], terms)
            compacted[key] = snapshot
        elif isinstance(value, list) and len(value) > 120:
            compacted[key] = _compact_sequence(value, terms)
        else:
            compacted[key] = value
    return compacted


def _compact_sequence(values: list[Any], terms: list[str]) -> list[Any]:
    if len(values) <= 100:
        return values
    normalized_terms = [_normalize_search_term(item) for item in terms if _normalize_search_term(item)]
    matches = []
    for item in values:
        searchable = _normalize_search_term(json.dumps(item, ensure_ascii=False, default=str))
        if normalized_terms and any(term in searchable for term in normalized_terms):
            matches.append(item)
    selected = [*values[:25], *matches[:50], *values[-25:]]
    output: list[Any] = []
    seen: set[str] = set()
    for item in selected:
        identity = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if identity not in seen:
            seen.add(identity)
            output.append(item)
    return output


def _normalize_search_term(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(value or "").casefold(), flags=re.UNICODE)


def _assistant_tool_message(response: Any) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": response.content,
        "tool_calls": [
            {
                "id": call.get("id"),
                "type": "function",
                "function": {
                    "name": call.get("name"),
                    "arguments": call.get("raw_arguments")
                    or json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                },
            }
            for call in response.tool_calls
        ],
    }


def _chat_voice_model() -> str:
    """Per-route model override for persona/chat speech. Empty string falls back to the
    provider's default (the strong/task model), so leaving YUEYUE_CHAT_MODEL unset keeps the
    old single-model behavior. Task planning/execution always stays on the default model -
    only owner-facing voice generation follows this override. Checks the real OS env first,
    then .env - main.py never loads .env into process environment generically."""
    return env_value("YUEYUE_CHAT_MODEL").strip()


def _task_reasoning_effort() -> str:
    """Reasoning depth for task execution / planning / verification calls only (never chat).
    Default "high" - the real ceiling for SiliconFlow DeepSeek per the 2026-07-12 live probe
    (low/medium/high are honored; "high" produced ~2x the reasoning of baseline, while an
    out-of-range "max" was accepted but not actually deeper). Set YUEYUE_TASK_REASONING_EFFORT
    to low/medium to trade depth for latency, or empty to disable."""
    raw = env_value("YUEYUE_TASK_REASONING_EFFORT")
    return raw.strip() if raw.strip() else "high"


def _mode_from_route(route: str) -> TurnMode:
    value = route.casefold()
    if value in {"social", "social_sticker"}:
        return TurnMode.SOCIAL
    if value in {"vision", "vision_task"}:
        return TurnMode.VISION
    if value in {"task", "tool_task", "screen_observe", "task_continuation"}:
        return TurnMode.TASK
    return TurnMode.CHAT


def _is_cancel(text: str) -> bool:
    value = re.sub(r"\s+", "", str(text or "").casefold())
    return any(
        marker in value
        for marker in ("取消任务", "取消任務", "停止任务", "停止任務", "算了不做", "cancel task", "stop task")
    )


def _is_continue(text: str) -> bool:
    value = re.sub(r"[\s，。！？,.!?~～]+", "", str(text or "").casefold())
    return value in {
        "繼續",
        "继续",
        "繼續吧",
        "继续吧",
        "接著做",
        "接着做",
        "再試一次",
        "再试一次",
        "有結果嗎",
        "有结果吗",
    }


def _display_value(value: Any) -> str:
    if isinstance(value, dict) and "value" in value:
        return str(value["value"])
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)


CHAT_META_LEAKAGE_MARKERS = (
    "agent runtime",
    "runtime",
    "debug",
    "bot \u6a21\u5f0f",
    "\u5beb\u500b bot",
    "\u5199\u4e2a bot",
    "\u6a21\u578b\u670d\u52d9",
    "\u6a21\u578b\u670d\u52a1",
    "\u4efb\u52d9\u9032\u5ea6",
    "\u4efb\u52a1\u8fdb\u5ea6",
    "workflow",
    "PromptCompiler",
    "provider",
    "v3",
    "\u958b\u767c",
    "\u5f00\u53d1",
    "\u7406\u60f3\u7684\u6708\u6708",
    "\u5c0d\u7684\u6708\u6708",
    "\u5bf9\u7684\u6708\u6708",
    "\u56de\u6536\u7ad9",
    "\u6c92\u6709\u7e7c\u7e8c\u4e82\u64cd\u4f5c",
    "\u6ca1\u6709\u7ee7\u7eed\u4e71\u64cd\u4f5c",
    "permission",
    "execute_command",
)

CHAT_DEVELOPMENT_REQUEST_MARKERS = (
    "debug",
    "runtime",
    "workflow",
    "PromptCompiler",
    "provider",
    "v3",
    "\u4fee",
    "\u958b\u767c",
    "\u5f00\u53d1",
    "\u4ee3\u78bc",
    "\u4ee3\u7801",
    "\u7a0b\u5f0f",
    "\u7a0b\u5e8f",
    "\u6a21\u578b",
)

SOCIAL_OVERPRODUCTION_MARKERS = (
    "\u4e09\u4ef6\u4e8b",
    "\u4e00\u6b65\u4e00\u6b65",
    "\u6574\u7406\u60c5\u7dd2",
    "\u6574\u7406\u4eca\u5929\u7684\u4efb\u52d9",
    "\u6574\u7406\u4eca\u5929\u7684\u4efb\u52a1",
    "\u4efb\u52d9\u548c\u884c\u7a0b",
    "\u4efb\u52a1\u548c\u884c\u7a0b",
    "\u9700\u8981\u6211\u5e6b\u4f60",
    "\u9700\u8981\u6211\u5e2e\u4f60",
    "\u62d6\u53bb\u56de\u6536\u7ad9",
    "\u91cd\u65b0\u958b\u767c",
    "\u9577\u7bc7",
    "\u957f\u7bc7",
)

SOCIAL_GENERIC_COMFORT_MARKERS = (
    "\u5148\u5225\u786c\u6490",
    "\u5225\u786c\u6490",
    "\u5225\u81ea\u5df1\u61cb\u8457",
    "\u8aaa\u7d66\u6211\u807d",
    "\u8bf4\u7ed9\u6211\u542c",
    "\u5728\uff0c\u966a\u4f60\u4e00\u4e0b",
    "\u966a\u4f60\u4e00\u4e0b",
    "\u5225\u628a\u81c9\u76ba\u6210\u90a3\u6a23",
    "\u6708\u6708\u77ed\u4e00\u9ede",
    "\u5225\u61cb\u8457",
    "\u6211\u966a\u4f60",
    "\u6211\u6703\u966a\u4f60",
    "\u6211\u4f1a\u966a\u4f60",
    "\u6211\u5728\u9019\u88e1",
    "\u6211\u5728\u8fd9\u91cc",
    "\u4f60\u7d2f\u4e86\u5c31\u4f11\u606f\u4e00\u4e0b",
    "\u4e3b\u4eba\u8f9b\u82e6\u4e86",
    "\u6708\u6708\u6703\u966a\u8457\u4f60",
    "\u6708\u6708\u4f1a\u966a\u7740\u4f60",
)

SOCIAL_META_REPLY_MARKERS = (
    "\u9019\u53e5\u8a71\u4f60\u525b\u624d\u8aaa\u904e",
    "\u8fd9\u53e5\u8bdd\u4f60\u521a\u624d\u8bf4\u8fc7",
    "\u525b\u624d\u8aaa\u904e",
    "\u521a\u624d\u8bf4\u8fc7",
    "\u8b8a\u56de\u6a5f\u5668\u4eba",
    "\u53d8\u56de\u673a\u5668\u4eba",
    "\u53ea\u662f\u5728\u6e2c\u8a66",
    "\u53ea\u662f\u5728\u6d4b\u8bd5",
    "\u5feb\u8aaa\u4f60\u5230\u5e95\u8981\u804a\u4ec0\u9ebc",
    "\u5feb\u8bf4\u4f60\u5230\u5e95\u8981\u804a\u4ec0\u4e48",
)

TEST_CONTEXT_REPLY_MARKERS = (
    "\u6e2c\u8a66\uff01",
    "\u6d4b\u8bd5\uff01",
    "\u9592\u804a\u6a21\u5f0f",
    "\u95f2\u804a\u6a21\u5f0f",
    "\u6a21\u5f0f",
    "\u8cbc\u5716\u9b25\u4e5f\u884c",
    "\u8d34\u56fe\u6597\u4e5f\u884c",
)

CONTROLLED_FALLBACK_MARKERS = (
    "\u53c8\u628a\u6708\u6708\u62ce\u51fa\u4f86\u76ef\u5834",
    "\u4f60\u5148\u4e1f\u4e00\u53e5\u904e\u4f86",
    "\u6211\u4e0d\u8dd1",
)

LIGHT_CATGIRL_STYLE_MARKERS = (
    "\u5c11\u8aaa\u6708\u6708\u5c0f\u6c23",
    "\u5c11\u8bf4\u6708\u6708\u5c0f\u6c14",
    "\u5c0f\u6c23",
    "\u5c0f\u6c14",
    "\u6562\u5acc\u68c4",
    "\u6562\u5acc\u5f03",
    "\u6c92\u4e0b\u6b21",
    "\u6ca1\u4e0b\u6b21",
    "\u624d\u4e0d\u662f\u7279\u5730\u6311\u7684",
    "\u624d\u4e0d\u662f\u7279\u5730\u6311\u7684",
    "\u53c8\u8981\u554a",
    "\u518d\u7d66\u4f60\u4e00\u6b21",
    "\u518d\u7ed9\u4f60\u4e00\u6b21",
    "\u8cde\u4f60",
    "\u8d4f\u4f60",
    "\u5634\u4e0a\u8aaa\u7d2f",
    "\u5634\u4e0a\u8bf4\u7d2f",
    "\u624b\u9084\u5728\u90a3\u908a\u78e8",
    "\u624b\u8fd8\u5728\u90a3\u8fb9\u78e8",
    "\u9017\u5f97\u9084\u633a\u771f",
    "\u9017\u5f97\u8fd8\u633a\u771f",
)

ANTI_CHATGPT_PROCESSING_MARKERS = (
    "\u9019\u500b\u6211\u6709\u770b\u5230",
    "\u8fd9\u4e2a\u6211\u6709\u770b\u5230",
    "\u6211\u6709\u770b\u5230",
    "\u6709\u770b\u5230",
    "\u6709\u63a5\u5230",
    "\u6536\u5230",
    "\u6536\u5230\u4e86",
    "\u77e5\u9053\u4e86",
    "\u77e5\u9053\u5566",
    "\u4e0d\u6703\u4e82\u8dd1\u504f",
    "\u4e0d\u4f1a\u4e71\u8dd1\u504f",
    "\u6211\u5728\u9019\u908a",
    "\u6211\u5728\u8fd9\u8fb9",
    "\u6211\u5728\u9019\u88e1",
    "\u6211\u5728\u8fd9\u91cc",
    "\u5148\u6162\u4e00\u9ede",
    "\u5148\u6162\u4e00\u70b9",
    "\u5148\u8b1b\u4e00\u9ede",
    "\u5148\u8bf4\u4e00\u70b9",
    "\u6211\u966a\u4f60",
    "\u4eca\u5929\u5148\u5b88\u8457\u4f60",
    "\u4eca\u5929\u5148\u5b88\u7740\u4f60",
    "\u55ef\uff0c\u5c31\u9019\u5f35",
    "\u55ef\uff0c\u5c31\u8fd9\u5f20",
    "\u9019\u5f35\u4e5f\u53ef\u4ee5",
    "\u8fd9\u5f20\u4e5f\u53ef\u4ee5",
    "\u6211\u4e00\u76f4\u90fd\u5728",
    "\u6211\u4e00\u76f4\u90fd\u5728\u8fd9\u91cc",
    "\u4e00\u76f4\u90fd\u5728",
)

CHAT_PROVIDER_TEMPLATE_MARKERS = (
    "\u6a21\u677f\u8c93",
    "\u6a21\u677f\u732b",
    "\u4f60\u4e00\u500b\u4eba\u7684",
    "\u4f60\u4e00\u4e2a\u4eba\u7684",
    "\u7b28\u86cb\u8c93\u5a18",
    "\u7b28\u86cb\u732b\u5a18",
    "\u55b5\u4e00\u8072\u5c31\u597d",
    "\u55b5\u4e00\u58f0\u5c31\u597d",
    "\u966a\u8457\u5c31\u597d",
    "\u966a\u7740\u5c31\u597d",
    "\u9b25\u5716",
    "\u6597\u56fe",
    "\u4f11\u606f\u6642\u9593",
    "\u4f11\u606f\u65f6\u95f4",
    "\u5e73\u5e38\u4e0d\u6703\u8ddf\u5225\u4eba\u8aaa",
    "\u5e73\u5e38\u4e0d\u4f1a\u8ddf\u522b\u4eba\u8bf4",
    "\u52aa\u529b\u66f4\u81ea\u7136",
    "\u4e0d\u662f\u6a21\u677f",
    "\u8b80\u7a3f\u6a5f",
    "\u8bfb\u7a3f\u673a",
    "\u8089\u9ebb",
    "\u622a\u5716",
    "\u622a\u56fe",
    "\u9ecf\u4f60\u4e00\u4e0b",
    "\u7c98\u4f60\u4e00\u4e0b",
    "\u8c93\u5a18\u4f11\u606f\u7ad9",
    "\u732b\u5a18\u4f11\u606f\u7ad9",
    "\u4f11\u606f\u7ad9",
    "\u6708\u6708\u90fd\u77e5\u9053",
)

CHAT_ROLEPLAY_BODY_MARKERS = (
    "\u5c3e\u5df4",
    "\u5c3e\u5df4",
    "\u8033\u6735",
    "\u8033\u6735",
    "\u722a\u5b50",
    "\u722a\u5b50",
)

CHAT_ROLEPLAY_ACTION_MARKERS = (
    "\u8f15\u8f15",
    "\u8f7b\u8f7b",
    "\u7e5e",
    "\u7ed5",
    "\u653e\u958b",
    "\u653e\u5f00",
    "\u6643",
    "\u8cbc",
    "\u8d34",
    "\u8e6d",
    "\u4f38",
    "\u62ac",
    "\u62cd",
    "\u6293",
    "\u5782",
    "\u6296",
)


def _normalize_chat_text(text: str) -> str:
    value = str(text or "").casefold()
    return re.sub(r"[\s\u3000\uff0c,.\u3002!\uff01?\uff1f~\uff5e\u300c\u300d\u300e\u300f\"'`]+", "", value)


def _is_test_context_note(text: str) -> bool:
    normalized = _normalize_chat_text(text)
    return any(
        marker in normalized
        for marker in (
            "\u53ea\u662f\u6e2c\u8a66",
            "\u53ea\u662f\u6d4b\u8bd5",
            "\u90a3\u6642\u5019\u53ea\u662f\u6e2c\u8a66",
            "\u90a3\u65f6\u5019\u53ea\u662f\u6d4b\u8bd5",
        )
    )


def _is_simple_social_prompt(text: str) -> bool:
    normalized = _normalize_chat_text(text)
    return any(
        marker in normalized
        for marker in (
            "\u966a\u6211\u804a",
            "\u50cf\u6a5f\u5668\u4eba",
            "\u50cf\u673a\u5668\u4eba",
            "\u6709\u9ede\u7d2f",
            "\u6709\u70b9\u7d2f",
            "\u6709\u9ede\u7169",
            "\u6709\u70b9\u70e6",
            "\u8abf\u4f60",
            "\u8c03\u4f60",
        )
    )


def _has_roleplay_action_line(text: str) -> bool:
    for line in [item.strip() for item in str(text or "").splitlines() if item.strip()]:
        lowered = line.casefold()
        has_body = any(marker.casefold() in lowered for marker in CHAT_ROLEPLAY_BODY_MARKERS)
        has_action = any(marker.casefold() in lowered for marker in CHAT_ROLEPLAY_ACTION_MARKERS)
        if has_body and has_action:
            return True
    return False


_BUBBLE_ESTIMATE_SOFT_LIMIT = 60


def _estimated_telegram_bubbles(text: str) -> list[str]:
    """Mirror how main.py actually delivers bubbles: split on NEWLINES first (each output line
    is its own Telegram message), and only sub-split a chunk into sentences when it exceeds the
    soft length limit. The old version split on every ？/！/。 - which wrongly counted one
    natural spoken line like 「欸欸！？怎麼可能？記錯了喵？」 or a soft 「辛苦了…主人喵…」 as
    several bubbles and rejected the owner's own preferred cadence. A short single line stays one
    bubble no matter how many sentence-enders it carries."""
    chunks = [chunk.strip() for chunk in re.split(r"\n+", str(text or "")) if chunk.strip()]
    parts: list[str] = []
    for chunk in chunks:
        if len(chunk) <= _BUBBLE_ESTIMATE_SOFT_LIMIT:
            parts.append(chunk)
            continue
        sentences = [item.strip() for item in re.split(r"(?<=[。！？!?~～…])\s*", chunk) if item.strip()]
        group = ""
        for sentence in sentences:
            if group and len(group) + len(sentence) > _BUBBLE_ESTIMATE_SOFT_LIMIT:
                parts.append(group)
                group = sentence
            else:
                group = f"{group}{sentence}" if group else sentence
        if group:
            parts.append(group)
    return parts


def _chat_reply_violates_social_policy(reply: str, owner_text: str = "") -> bool:
    value = str(reply or "")
    lowered = value.casefold()
    owner_lowered = str(owner_text or "").casefold()
    if not value.strip():
        return True
    lines = _estimated_telegram_bubbles(value)
    # General ceiling for ordinary CHAT/SOCIAL turns: default to one short line, two at most
    # for a genuinely heavier moment. This used to be a loose safety net (>4), which let most
    # casual messages drift back to 3-4 line monologues whenever the wording didn't happen to
    # match one of the specific _is_simple_social_prompt trigger phrases below.
    if len(lines) > 2:
        return True
    if _is_simple_social_prompt(owner_text) and (len(lines) > 2 or len(value) > 100):
        return True
    if _is_test_context_note(owner_text) and (
        len(lines) > 2
        or len(value) > 90
        or any(marker.casefold() in lowered for marker in TEST_CONTEXT_REPLY_MARKERS)
    ):
        return True
    if any(len(line) > 64 for line in lines):
        return True
    if len(value) > 180:
        return True
    # Capability questions (你會做什麼/能幫我什麼) legitimately need ability words like 提醒/任務 -
    # live 2026-07-20 the honest answer got rejected as meta-leak and the owner saw the fallback.
    asking_abilities = any(
        marker in owner_lowered for marker in (
            "會做什麼", "会做什么", "會些什麼", "能做什麼", "能做什么", "能幫我什麼", "能帮我什么",
            "會什麼", "会什么", "會玩", "会玩", "有什麼遊戲", "有什么游戏", "什麼遊戲", "什么游戏",
            "能玩", "會幫", "会帮", "有什麼技能", "有什么技能", "有什麼功能", "有什么功能",
        )
    )
    if not asking_abilities:
        for marker in CHAT_META_LEAKAGE_MARKERS:
            marker_value = marker.casefold()
            if marker_value in lowered and marker_value not in owner_lowered:
                return True
    if any(marker.casefold() in lowered for marker in SOCIAL_OVERPRODUCTION_MARKERS):
        return True
    if any(marker.casefold() in lowered for marker in SOCIAL_GENERIC_COMFORT_MARKERS):
        return True
    if any(marker.casefold() in lowered for marker in SOCIAL_META_REPLY_MARKERS):
        return True
    if any(marker.casefold() in lowered for marker in CONTROLLED_FALLBACK_MARKERS):
        return True
    if any(marker.casefold() in lowered for marker in LIGHT_CATGIRL_STYLE_MARKERS):
        return True
    if any(marker.casefold() in lowered for marker in ANTI_CHATGPT_PROCESSING_MARKERS):
        return True
    if any(marker.casefold() in lowered for marker in CHAT_PROVIDER_TEMPLATE_MARKERS):
        return True
    if _has_roleplay_action_line(value):
        return True
    if value.count("\uff08") + value.count("(") > 1:
        return True
    if value.count("\u5c0d\u4e0d\u8d77") + value.count("\u5bf9\u4e0d\u8d77") > 1:
        return True
    if value.count("\u55b5") > 2:
        return True
    # Owner register gate (voice_contract): Taiwan-flavored final particles, spoken-Cantonese
    # characters, or raw Simplified (unless the owner's own message is Simplified - script
    # mirroring is deliberate). Violations flow into the same truncate/regenerate/fallback
    # pipeline as every other quality breach.
    if voice_register_violation(value, allow_simplified=owner_text_is_simplified(owner_text)):
        return True
    # Too many sentence-enders reads as choppy/over-punctuated - but a soft trailing ellipsis
    # (\u3002\u3002\u3002 / \u2026 / ~~~), which the owner's preferred tender cadence uses, is one stylistic beat,
    # not many sentences. Collapse runs of the same mark before counting so \u300c\u8f9b\u82e6\u4e86\u2026\u4e3b\u4eba\u55b5\u2026\u300d
    # is not penalised as 6 sentence-enders.
    collapsed = re.sub(r"([\u3002\uff01\uff1f!?~\uff5e\u2026])\1+", r"\1", value)
    return sum(collapsed.count(mark) for mark in ["\u3002", "\uff01", "\uff1f", "!", "?"]) > 4


def _social_chat_fallback(owner_text: str) -> str:
    # Last-resort pooled lines. Persona recalibration 2026-07-16: base register is soft, passive,
    # clean-cute - mischief is a tiny hidden glint, never a combative jab. Each line must pass the
    # register gate (test_social_chat_fallback_is_register_clean_on_every_branch drives them all).
    normalized = _normalize_chat_text(owner_text)
    if "像機器人" in normalized or "像机器人" in normalized:
        return "誒……才不是呢。月月只是剛剛想事情想得太認真了嘛"
    if "陪我聊" in normalized or "陪我聊一下" in normalized:
        return "好呀。。。月月一直都在的，想聊什麼？"
    if "只是測試" in normalized or "只是测试" in normalized:
        return "嗯哼，月月就知道～不過被主人惦記著也挺開心的"
    if (
        "最近" in normalized
        and ("調你" in normalized or "调你" in normalized)
        and "累" in normalized
    ):
        return "嗯。。。辛苦了，月月乖乖的，你別累壞自己就好"
    if "有點煩" in normalized or "有点烦" in normalized:
        return "怎麼啦。。。說給月月聽聽嘛，不說也沒關係，我陪著你"
    if any(
        marker in normalized
        for marker in ("有點累", "有点累", "好累", "很累", "累死", "累了", "累啦")
    ):
        return "辛苦了。。。先歇一會，其他的月月幫你記著"
    # Universal default: must read naturally after ANY owner message (praise, statement,
    # question). "Spaced out, say it again" is honest, cute, and never a non-sequitur.
    return "誒，等等，月月剛剛走神了。。。你再說一次嘛"


def _truncate_chat_reply(reply: str, max_lines: int) -> str:
    # Truncate by estimated Telegram bubble, not raw "\n" count - a reply with no newlines
    # but several ？/！/～-terminated sentences must still be trimmed down to max_lines bubbles.
    bubbles = _estimated_telegram_bubbles(reply)
    return "\n".join(bubbles[:max_lines])


def _drop_trailing_full_stop(text: str) -> str:
    """Real casual chat messages usually just stop, or end with ! / ?; a plain trailing
    full stop (。) reads as more formal/written than spoken. Only strips the very last
    character, so periods used mid-message (e.g. before main.py splits into bubbles) are
    untouched."""
    value = str(text or "").rstrip()
    if value.endswith("。") and not value.endswith(("！。", "？。", "~。")):
        value = value[:-1].rstrip()
    return value


def _provider_failure_reply(error: Exception) -> str:
    category = error.category if isinstance(error, ProviderFailure) else "unknown"
    if category == "insufficient_balance":
        return "這次接不上，我先停手，不硬試。"
    if category == "authentication":
        return "這次接不上，我先停手，不硬闖。"
    if category == "rate_limit":
        return "那邊有點塞，我先停手，不一直敲。"
    return "這次卡住了，我先停手。你要我再試，再叫我。"


def _clean_reply(text: str) -> str:
    value = re.sub(r"<[^>]*DSML[^>]*>.*?(?:</[^>]*DSML[^>]*>|$)", "", str(text or ""), flags=re.IGNORECASE | re.DOTALL)
    return value.strip()


def _may_replace_state(kind: str) -> bool:
    return str(kind or "").startswith(("workflow.", "permission.", "tool.result", "task_queue.", "test."))


def _contains_internal_terms(text: str) -> bool:
    value = str(text or "").casefold()
    return any(
        marker in value
        for marker in (
            "runtime",
            "route policy",
            "taskgraph",
            "task graph",
            "permissionmanager",
            "sessionbrain",
            "tool call",
            "control plane",
        )
    )
