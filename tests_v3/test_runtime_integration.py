from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import main
from agent_turns import InboundMessagePart, MessageCoalescer
from yueyue_v3.models import ExecutionEvidence, GoalContract, RequestedOutput, StepContract, V3ToolResult
from yueyue_v3.planning import PlannedWorkflow
from yueyue_v3.providers import ProviderResponse, ScriptedProvider
from yueyue_v3.rendering import RenderLedger
from yueyue_v3.runtime import YueYueRuntimeV3


@dataclass
class StaticPlanner:
    planned: PlannedWorkflow

    def plan(self, _objective: str) -> PlannedWorkflow:
        return self.planned


class FakeTools:
    def __init__(self):
        self.calls: list[str] = []

    @property
    def names(self):
        return lambda: ["get_screen_ui", "click_ui_element"]

    def list(self, allowed=None):
        names = allowed or self.names()
        return [
            type("Tool", (), {"name": name, "description": name, "parameters": {"type": "object"}})() for name in names
        ]

    def execute(self, name, arguments, callback=None):
        self.calls.append(name)
        if name == "click_ui_element":
            return V3ToolResult("ok", "clicked", facts={"action": "click", "observe_needed": True})
        if self.calls.count("get_screen_ui") == 1:
            return V3ToolResult(
                "ok",
                "observed",
                facts={
                    "revision": "r1",
                    "visible_text": ["剩餘用量 1%"],
                    "ui_elements": [{"element_id": "9", "name": "剩餘用量 1%"}],
                },
            )
        return V3ToolResult(
            "ok", "五小時用量：剩餘 37%", facts={"revision": "r2", "visible_text": ["五小時用量：剩餘 37%"]}
        )


def _planned_workflow() -> PlannedWorkflow:
    return PlannedWorkflow(
        GoalContract(
            "查看五小時剩餘用量",
            [RequestedOutput("五小時剩餘用量", "五小時限制的剩餘百分比", True, "percentage", ["五小時"])],
            ["取得五小時剩餘百分比"],
            "guarded",
        ),
        [
            StepContract("observe", "找到剩餘用量入口", "observe", "看見剩餘用量", ["get_screen_ui"], ["剩餘用量"]),
            StepContract(
                "open",
                "打開剩餘用量",
                "act",
                "點擊並重新觀察",
                ["click_ui_element", "get_screen_ui"],
                risk_level="guarded",
            ),
            StepContract("read", "讀取五小時用量", "verify", "取得五小時百分比", ["get_screen_ui"], ["五小時"]),
        ],
    )


def test_permission_reply_resumes_original_workflow_until_goal_is_verified(runtime_root) -> None:
    provider = ScriptedProvider(
        [
            ProviderResponse("", "", [{"id": "observe", "name": "get_screen_ui", "arguments": {}}]),
            ProviderResponse("", "", [{"id": "click", "name": "click_ui_element", "arguments": {"element_id": "9"}}]),
            # Runtime asks the model to phrase the permission request itself (owner-tested
            # behavior: a scripted one-liner used to always be shown here regardless of context).
            ProviderResponse("這一步需要你點頭，我才能繼續點擊畫面上的項目。", "", []),
            ProviderResponse("", "", [{"id": "observe2", "name": "get_screen_ui", "arguments": {}}]),
            ProviderResponse("找到啦，五小時還剩 37%。", "", []),
        ]
    )
    runtime = YueYueRuntimeV3(runtime_root, provider)
    runtime.planner = StaticPlanner(_planned_workflow())
    runtime.tools = FakeTools()

    first = runtime.chat("幫我查看五小時剩餘用量", response_policy=type("Policy", (), {"route": "tool_task"})())
    # The model's own permission wording reaches the owner - but 「點頭」 is rewritten on the way
    # out (owner flagged it twice; see voice_contract.repair_nod_idiom).
    assert "說可以" in first["content"]
    assert "點頭" not in first["content"]
    assert runtime.tools.calls == ["get_screen_ui"]

    final = runtime.chat("嗯，繼續吧")
    assert runtime.state.workflow.verification.goal_satisfied is True
    assert runtime.state.workflow.outputs["五小時剩餘用量"]["value"] == "37%"
    assert runtime.tools.calls == ["get_screen_ui", "click_ui_element", "get_screen_ui"]
    assert "37%" in final["content"]
    execution_calls = [call for call in provider.calls if call["tools"]]
    for call in execution_calls:
        if call["tools"] == ["submit_step_verification"]:
            continue
        user_messages = [item for item in call["messages"] if item.get("role") == "user"]
        task_contracts = [
            item
            for item in call["messages"]
            if item.get("role") == "system" and "### Current task contract" in str(item.get("content", ""))
        ]
        assert len(user_messages) == 1
        assert len(task_contracts) == 1
        assert len(call["messages"]) <= 18


def test_main_can_build_v3_without_changing_telegram_call_site(runtime_root) -> None:
    agent = main.build_agent(
        runtime_name="v3",
        provider_override=ScriptedProvider([ProviderResponse("在呢。", "", [])]),
        state_dir=runtime_root / "workspace" / "project_cache" / "v3",
    )
    assert isinstance(agent, YueYueRuntimeV3)
    # CHAT/SOCIAL replies drop a bare trailing full stop (real chat messages just stop or
    # end with ~/!/?); the provider's raw "在呢。" becomes "在呢" after that polish step.
    assert agent.chat("嗨嗨")["content"] == "在呢"


def test_social_prompt_note_is_never_added_to_task_routes() -> None:
    assert main.should_inject_social_note("chat") is True
    assert main.should_inject_social_note("social_sticker") is True
    assert main.should_inject_social_note("tool_task") is False
    assert main.should_inject_social_note("vision_task") is False


def test_task_continuation_route_resumes_existing_workflow(runtime_root) -> None:
    provider = ScriptedProvider(
        [
            ProviderResponse("", "", [{"id": "observe", "name": "get_screen_ui", "arguments": {}}]),
            ProviderResponse("", "", [{"id": "click", "name": "click_ui_element", "arguments": {"element_id": "9"}}]),
            ProviderResponse("這一步需要你點頭，我才能繼續點擊畫面上的項目。", "", []),
            ProviderResponse("", "", [{"id": "observe2", "name": "get_screen_ui", "arguments": {}}]),
            ProviderResponse("找到啦，五小時還剩 37%。", "", []),
        ]
    )
    runtime = YueYueRuntimeV3(runtime_root, provider)
    runtime.planner = StaticPlanner(_planned_workflow())
    runtime.tools = FakeTools()
    runtime.chat("幫我查看五小時剩餘用量", response_policy=type("Policy", (), {"route": "tool_task"})())

    final = runtime.chat("繼續", response_policy=type("Policy", (), {"route": "task_continuation"})())

    assert runtime.state.workflow.verification.goal_satisfied is True
    assert "37%" in final["content"]


def test_pending_action_and_workflow_survive_runtime_restart(runtime_root) -> None:
    provider = ScriptedProvider(
        [
            ProviderResponse("", "", [{"id": "observe", "name": "get_screen_ui", "arguments": {}}]),
            ProviderResponse("", "", [{"id": "click", "name": "click_ui_element", "arguments": {"element_id": "9"}}]),
        ]
    )
    runtime = YueYueRuntimeV3(runtime_root, provider)
    runtime.planner = StaticPlanner(_planned_workflow())
    runtime.tools = FakeTools()
    runtime.chat("幫我查看五小時剩餘用量", response_policy=type("Policy", (), {"route": "tool_task"})())

    restored = YueYueRuntimeV3(runtime_root, ScriptedProvider([]))

    assert restored.state.workflow.workflow_id == runtime.state.workflow.workflow_id
    assert restored.state.workflow.current_step().step_id == "open"
    assert restored.state.permission.pending_action.tool_name == "click_ui_element"
    assert restored.state.permission.pending_action.arguments == {"element_id": "9"}


def test_telegram_renderer_deduplicates_the_same_event_after_retry(runtime_root) -> None:
    class FakeBot:
        def __init__(self):
            self.messages = []

        def send_chat_action(self, *_args, **_kwargs):
            return None

        def send_message(self, chat_id, text, **_kwargs):
            self.messages.append((chat_id, text))

    gateway = main.TelegramGateway.__new__(main.TelegramGateway)
    gateway.bot = FakeBot()
    gateway.render_ledger = RenderLedger(runtime_root / "workspace" / "project_cache" / "v3" / "render.json")

    payload = {"content": "同一個結果只應該發一次。"}
    gateway.send_reply_with_stickers("owner", payload, render_event_id="turn-1")
    gateway.send_reply_with_stickers("owner", payload, render_event_id="turn-1")

    # Each bubble drops a bare trailing full stop (real chat messages just stop or end with
    # ~/!/?), so the sent text loses the period even though the input payload still has one.
    assert gateway.bot.messages == [("owner", "同一個結果只應該發一次")]


def test_owner_reply_never_exposes_internal_control_plane_terms(runtime_root) -> None:
    provider = ScriptedProvider([ProviderResponse("runtime 的 TaskGraph 把 tool 卡住了。", "", [])])
    runtime = YueYueRuntimeV3(runtime_root, provider)
    reply = runtime._compose_owner_voice("task_blocked", {"reason": "internal"}, "我還沒拿到結果，先停一下。")
    assert reply == "我還沒拿到結果，先停一下。"


def test_v3_tool_notifier_narrates_significant_tools_with_dedupe_and_honest_errors(runtime_root) -> None:
    class FakeBot:
        def __init__(self):
            self.actions = []
            self.messages = []

        def send_chat_action(self, chat_id, action):
            self.actions.append((chat_id, action))

        def send_message(self, chat_id, text, **_kwargs):
            self.messages.append((chat_id, text))

    gateway = main.TelegramGateway.__new__(main.TelegramGateway)
    gateway.bot = FakeBot()
    gateway.agent = type("V3", (), {"runtime_version": "v3"})()
    message = type("Message", (), {"chat": type("Chat", (), {"id": "owner"})()})()

    notify = gateway._tool_notifier_for(message)
    notify("click_ui_element", {"element_id": "9"}, "start", None)
    # An immediate duplicate of the same action stays silent instead of spamming.
    notify("click_ui_element", {"element_id": "9"}, "start", None)
    notify("capture_screen", {}, "start", None)
    # A tool without an owner-facing label stays typing-only.
    notify("update_profile", {}, "start", None)
    # Retryable/transient errors stay quiet (the runtime retries them); real failures are reported.
    notify("click_ui_element", {}, "end", V3ToolResult("error", "failed", error="transient blip", retryable=True))
    notify("click_ui_element", {}, "end", V3ToolResult("error", "failed", error="element gone"))

    texts = [text for _chat, text in gateway.bot.messages]
    assert texts[0] == "_正在點擊介面..._"
    assert texts[1] == "_正在截取畫面..._"
    assert len(texts) == 3 and "element gone" in texts[2]
    assert ("owner", "typing") in gateway.bot.actions


class FakeObserveTools:
    def __init__(self, capture_ok: bool = True, analyze_ok: bool = True):
        self.capture_ok = capture_ok
        self.analyze_ok = analyze_ok
        self.calls: list[tuple[str, dict]] = []

    def execute(self, name, arguments, callback=None):
        self.calls.append((name, dict(arguments or {})))
        if name == "capture_screen":
            if not self.capture_ok:
                return V3ToolResult("error", "capture failed", error="no display")
            return V3ToolResult(
                "ok",
                "Screen captured for internal observation.",
                data={"path": "C:/fake/shot.png", "window_title": "Google Chrome"},
                facts={"screenshot_id": "s1"},
            )
        if name == "analyze_media":
            if not self.analyze_ok:
                return V3ToolResult("error", "vision failed", error="model down")
            return V3ToolResult(
                "ok", "Image analyzed.", data={"summary": "Chrome 開著 YouTube 首頁，正在播放視頻"}
            )
        return V3ToolResult("error", f"unexpected tool {name}")


def _observe_policy():
    return type("Policy", (), {"route": "screen_observe"})()


def test_screen_observe_route_forces_capture_then_analyze_and_reply_carries_content(runtime_root) -> None:
    # The owner-voice model answers with a contentless "looked at it" line on purpose -
    # the runtime must refuse it and fall back to a reply that carries the actual content.
    provider = ScriptedProvider([ProviderResponse("看完了，沒什麼特別的", "", [])])
    runtime = YueYueRuntimeV3(runtime_root, provider)
    runtime.tools = FakeObserveTools()

    reply = runtime.chat("看下我屏幕現在顯示什麼", response_policy=_observe_policy())["content"]

    assert [name for name, _ in runtime.tools.calls] == ["capture_screen", "analyze_media"]
    assert runtime.tools.calls[1][1]["file_path"] == "C:/fake/shot.png"
    assert "YouTube" in reply


def test_screen_observe_analyze_failure_is_reported_honestly(runtime_root) -> None:
    provider = ScriptedProvider([])
    runtime = YueYueRuntimeV3(runtime_root, provider)
    runtime.tools = FakeObserveTools(analyze_ok=False)

    reply = runtime.chat("屏幕上是什麼", response_policy=_observe_policy())["content"]

    assert [name for name, _ in runtime.tools.calls] == ["capture_screen", "analyze_media"]
    assert "截图" in reply or "截圖" in reply
    assert "YouTube" not in reply


def test_screen_observe_capture_failure_is_reported_honestly(runtime_root) -> None:
    runtime = YueYueRuntimeV3(runtime_root, ScriptedProvider([]))
    runtime.tools = FakeObserveTools(capture_ok=False)

    reply = runtime.chat("看看屏幕", response_policy=_observe_policy())["content"]

    assert [name for name, _ in runtime.tools.calls] == ["capture_screen"]
    assert "失败" in reply or "失敗" in reply


def test_reply_bubbles_follow_model_newlines_not_every_sentence() -> None:
    text = "這兩句要留在同一個泡泡。真的。\n這是第二個泡泡"
    assert main._split_reply_bubbles(text) == ["這兩句要留在同一個泡泡。真的", "這是第二個泡泡"]


def test_overlong_single_line_still_splits_into_readable_bubbles() -> None:
    sentence = "這一句話大概有三十個字這麼長用來測試泡泡的軟上限行為！"
    parts = main._split_reply_bubbles(sentence * 3)
    assert len(parts) >= 2
    assert all(len(part) <= main._BUBBLE_SOFT_LIMIT for part in parts)


def test_afterthought_final_bubble_gets_a_hold_delay() -> None:
    assert main._afterthought_hold("不過別熬夜", 1, 2) > 0
    assert main._afterthought_hold("不過別熬夜", 0, 2) == 0.0
    assert main._afterthought_hold("正文內容不是補刀", 1, 2) == 0.0


def test_analyze_media_reuses_latest_internal_screenshot_when_model_path_is_invalid(runtime_root) -> None:
    runtime = YueYueRuntimeV3(runtime_root, ScriptedProvider([]))
    screenshot = runtime.state_dir / "observations" / "latest.png"
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    screenshot.write_bytes(b"image")
    workflow = runtime.workflow_engine.create(
        GoalContract("Inspect screen", [RequestedOutput("screen", "screen summary")], []),
        [StepContract("observe", "Inspect", "observe", "screen is understood", ["analyze_media"])],
    )
    workflow.evidence.append(
        ExecutionEvidence("observe", "capture_screen", "ok", "captured", artifacts=[str(screenshot)])
    )

    prepared = runtime._prepare_tool_arguments("analyze_media", {"file_path": "missing-relative.png"}, workflow)

    assert prepared["file_path"] == str(screenshot.resolve())


def test_timer_thread_can_process_a_telegram_turn(runtime_root) -> None:
    runtime = YueYueRuntimeV3(
        runtime_root,
        ScriptedProvider([ProviderResponse("背景回覆成功", "", [])]),
    )
    replies: list[str] = []
    errors: list[Exception] = []

    def invoke() -> None:
        try:
            replies.append(runtime.chat("你好")["content"])
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=invoke)
    thread.start()
    thread.join(timeout=5)

    assert not errors
    assert replies == ["背景回覆成功"]


def test_message_coalescer_timer_can_process_a_v3_turn(runtime_root) -> None:
    runtime = YueYueRuntimeV3(
        runtime_root,
        ScriptedProvider([ProviderResponse("timer reply", "", [])]),
    )
    coalescer = MessageCoalescer(debounce_seconds=0.02)
    finished = threading.Event()
    replies: list[str] = []
    errors: list[Exception] = []

    def flush(turn) -> None:
        try:
            replies.append(runtime.chat(turn.primary_text)["content"])
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    coalescer.add(
        InboundMessagePart(chat_id="owner", message_id=1, kind="text", text="timer turn"),
        flush,
    )

    assert finished.wait(timeout=5)
    assert not errors
    assert replies == ["timer reply"]


def test_concurrent_telegram_turns_are_serialized_by_one_runtime_writer(runtime_root) -> None:
    class MeasuringProvider:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def chat(self, _messages, _tools, model="", tool_choice="auto", reasoning_effort=""):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.05)
            with self.lock:
                self.active -= 1
            return ProviderResponse("完成", "", [])

        def close(self):
            return None

    provider = MeasuringProvider()
    runtime = YueYueRuntimeV3(runtime_root, provider)
    replies: list[str] = []
    errors: list[Exception] = []

    def invoke(text: str) -> None:
        try:
            replies.append(runtime.chat(text)["content"])
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=invoke, args=(f"聊天 {index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert sorted(replies) == ["完成", "完成"]
    assert provider.max_active == 1


def test_telegram_turn_failure_message_never_exposes_internal_exception() -> None:
    message = main.TelegramGateway._owner_safe_turn_error(
        RuntimeError("Only the runtime writer thread may drain events.")
    )

    assert "runtime writer" not in message
    assert "drain events" not in message
    assert "剛剛" in message


def test_desktop_observation_contract_advances_to_focus_instead_of_repeating_screenshots(runtime_root) -> None:
    planned = PlannedWorkflow(
        GoalContract(
            "Read a value from Codex",
            [RequestedOutput("value", "the requested Codex value")],
            ["The value is verified"],
        ),
        [
            StepContract(
                "observe",
                "Inspect desktop",
                "observe",
                "Desktop and windows inspected",
                ["capture_screen", "list_windows"],
                required_sources=["capture_screen", "list_windows"],
                completion_mode="all_sources",
            ),
            StepContract(
                "focus",
                "Focus Codex",
                "act",
                "Codex is focused",
                ["focus_window", "capture_screen"],
                risk_level="guarded",
            ),
        ],
    )

    class DesktopTools:
        def __init__(self):
            self.calls: list[str] = []

        def names(self):
            return ["capture_screen", "list_windows", "focus_window"]

        def list(self, allowed=None):
            return [
                type("Tool", (), {"name": name, "description": name, "parameters": {"type": "object"}})()
                for name in (allowed or self.names())
            ]

        def execute(self, name, arguments, callback=None):
            self.calls.append(name)
            if name == "capture_screen":
                return V3ToolResult(
                    "ok",
                    "captured",
                    facts={"width": 2560, "height": 1440, "window_title": "SiliconFlow - Chrome"},
                )
            if name == "list_windows":
                return V3ToolResult(
                    "ok",
                    "listed",
                    facts={"windows": [{"title": "SiliconFlow - Chrome"}, {"title": "Codex", "window_id": "9"}]},
                )
            return V3ToolResult("ok", "focused", facts={"window_title": "Codex", "observe_needed": True})

    provider = ScriptedProvider(
        [
            ProviderResponse("", "", [{"id": "capture", "name": "capture_screen", "arguments": {}}]),
            ProviderResponse("", "", [{"id": "windows", "name": "list_windows", "arguments": {}}]),
            ProviderResponse("", "", [{"id": "focus", "name": "focus_window", "arguments": {"window_id": "9"}}]),
        ]
    )
    runtime = YueYueRuntimeV3(runtime_root, provider)
    runtime.planner = StaticPlanner(planned)
    runtime.tools = DesktopTools()

    runtime.chat("Read the Codex value", response_policy=type("Policy", (), {"route": "tool_task"})())

    execution_calls = [call for call in provider.calls if call["tools"] and call["tools"] != ["submit_step_verification"]]
    assert len(execution_calls) == 3
    assert runtime.tools.calls == ["capture_screen", "list_windows"]
    assert runtime.state.permission.pending_action.tool_name == "focus_window"


def test_three_duplicate_observations_stop_without_spending_full_iteration_budget(runtime_root) -> None:
    planned = PlannedWorkflow(
        GoalContract("Find Codex", [RequestedOutput("result", "Codex result")], ["Codex is found"]),
        [
            StepContract(
                "observe",
                "Find Codex",
                "observe",
                "Codex is visible",
                ["capture_screen"],
                required_facts=["Codex"],
                completion_mode="facts",
            )
        ],
    )

    class DuplicateScreenTools:
        def __init__(self):
            self.calls = 0

        def names(self):
            return ["capture_screen"]

        def list(self, allowed=None):
            return [
                type("Tool", (), {"name": "capture_screen", "description": "capture", "parameters": {"type": "object"}})()
            ]

        def execute(self, name, arguments, callback=None):
            self.calls += 1
            return V3ToolResult(
                "ok",
                "captured",
                facts={
                    "revision": f"revision-{self.calls}",
                    "screenshot_id": f"screen-{self.calls}",
                    "window_title": "SiliconFlow - Chrome",
                    "width": 2560,
                    "height": 1440,
                },
            )

    provider = ScriptedProvider(
        [
            ProviderResponse("", "", [{"id": f"capture-{index}", "name": "capture_screen", "arguments": {}}])
            for index in range(8)
        ]
    )
    runtime = YueYueRuntimeV3(runtime_root, provider)
    runtime.planner = StaticPlanner(planned)
    runtime.tools = DuplicateScreenTools()

    runtime.chat("Find Codex", response_policy=type("Policy", (), {"route": "tool_task"})())

    assert runtime.tools.calls == 3
    assert runtime.state.workflow.status.value == "blocked"
    assert "No state change" in runtime.state.workflow.verification.reason


def test_invalid_planner_contract_is_replanned_once_before_any_tool_runs(runtime_root) -> None:
    invalid = PlannedWorkflow(
        GoalContract("Find Codex", [RequestedOutput("result", "Codex result")], ["Codex is found"]),
        [
            StepContract(
                "observe",
                "Inspect",
                "observe",
                "Windows collected",
                ["capture_screen"],
                required_sources=["list_windows"],
                completion_mode="all_sources",
            )
        ],
    )
    repaired = PlannedWorkflow(
        invalid.goal,
        [
            StepContract(
                "observe",
                "Inspect",
                "observe",
                "Windows collected",
                ["list_windows"],
                required_sources=["list_windows"],
                completion_mode="all_sources",
            )
        ],
    )

    class RepairingPlanner:
        def __init__(self):
            self.replan_calls = 0

        def plan(self, _objective):
            return invalid

        def replan(self, _objective, issues):
            self.replan_calls += 1
            assert issues
            return repaired

    class WindowTools:
        def __init__(self):
            self.calls: list[str] = []

        def names(self):
            return ["capture_screen", "list_windows"]

        def list(self, allowed=None):
            return [
                type("Tool", (), {"name": name, "description": name, "parameters": {"type": "object"}})()
                for name in (allowed or self.names())
            ]

        def execute(self, name, arguments, callback=None):
            self.calls.append(name)
            return V3ToolResult("ok", "listed", facts={"windows": [{"title": "Codex"}]})

    runtime = YueYueRuntimeV3(
        runtime_root,
        ScriptedProvider([ProviderResponse("", "", [{"id": "windows", "name": "list_windows", "arguments": {}}])]),
    )
    planner = RepairingPlanner()
    runtime.planner = planner
    runtime.tools = WindowTools()

    runtime.chat("Find Codex", response_policy=type("Policy", (), {"route": "tool_task"})())

    assert planner.replan_calls == 1
    assert runtime.tools.calls == ["list_windows"]
