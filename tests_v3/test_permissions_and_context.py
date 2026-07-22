from __future__ import annotations

from yueyue_v3.context import (
    ContextCompiler,
    ContextTurn,
    ShortContextStore,
    owner_script_is_simplified,
    owner_script_is_simplified_with_history,
    to_simplified_script,
)
from yueyue_v3.models import ActionEnvelope, PermissionState, TurnEnvelope, TurnMode
from yueyue_v3.permissions import PermissionController


def test_natural_single_approval_unlocks_computer_bundle() -> None:
    controller = PermissionController()
    state = PermissionState()
    controller.request(state, ActionEnvelope("focus_window", {"window_id": "1"}, "step-1", "guarded"))
    assert controller.apply_reply(state, "嗯，繼續吧") == "turn"
    assert state.scope == "workflow"
    assert controller.permits(state, ActionEnvelope("click_ui_element", {"element_id": "2"}, "step-2", "guarded"))


def test_task_context_excludes_social_history_and_notes(runtime_root) -> None:
    store = ShortContextStore(runtime_root / "workspace" / "project_cache" / "short.json")
    compiler = ContextCompiler(runtime_root, store)
    chat = TurnEnvelope("owner", "來鬥圖", TurnMode.SOCIAL)
    compiler.remember(chat, "好呀，來嘛")

    task_messages = compiler.compile_turn(
        TurnEnvelope("owner", "幫我查看視窗", TurnMode.TASK), social_note="不要使用工具，只要鬥圖"
    )
    joined = "\n".join(str(item.get("content", "")) for item in task_messages)
    assert "不要使用工具，只要鬥圖" not in joined
    assert "來鬥圖" not in joined


def test_chat_context_requires_capability_honesty_and_concise_defaults(runtime_root) -> None:
    store = ShortContextStore(runtime_root / "workspace" / "project_cache" / "short.json")
    prompt = ContextCompiler(runtime_root, store).system_prompt(TurnMode.CHAT)

    # The CHAT contract was simplified to lean Chinese (the wall of English "don't" rules
    # overloaded the model); the intent - concise default + no faked-capability promises - is
    # preserved, so assert the current phrasing carries it.
    assert "預設一句話就夠" in prompt
    assert "做不到的承諾" in prompt


def test_owner_simplified_script_is_detected_and_mirrored_deterministically() -> None:
    assert owner_script_is_simplified("今天工作好累，晚上想吃点好吃的犒劳自己") is True
    assert owner_script_is_simplified("你今天心情點呀") is False
    assert owner_script_is_simplified("可以陰我打機嗎") is False
    assert owner_script_is_simplified("hi") is False

    traditional_reply = "喂，月月湊近一點，你那句還有小尾音喔"
    assert to_simplified_script(traditional_reply) == "喂，月月凑近一点，你那句还有小尾音喔"


def test_ambiguous_script_falls_back_to_recent_history(runtime_root) -> None:
    store = ShortContextStore(runtime_root / "workspace" / "project_cache" / "script_history.json")
    assert owner_script_is_simplified_with_history("hi", store, "chat1") is False

    store.append("chat1", ContextTurn("user", "今天工作好累，想吃点好吃的"))
    assert owner_script_is_simplified_with_history("嗯", store, "chat1") is True
    # the current message's own script always takes priority over history
    assert owner_script_is_simplified_with_history("你今天心情點呀", store, "chat1") is False


def test_write_file_approval_covers_the_workflow():
    # Gap-battery regression 2026-07-15: a create-append-verify task burned 8 approvals because
    # every write_file consumed the single grant. One approval now covers the same workflow's
    # remaining workspace writes.
    from yueyue_v3.models import ActionEnvelope, PermissionState
    from yueyue_v3.permissions import PermissionController

    controller = PermissionController()
    state = PermissionState()
    first = ActionEnvelope("write_file", {"filename": "project/a.txt", "content": "step1"}, "step_1", "low")
    controller.request(state, first)
    assert controller.apply_reply(state, "可以") == "turn"
    assert controller.permits(state, first)
    second = ActionEnvelope("write_file", {"filename": "project/a.txt", "content": "step2"}, "step_2", "low")
    assert controller.permits(state, second)  # same workflow, no re-ask


def test_high_risk_grant_is_task_scoped_and_expires():
    # Updated 2026-07-20: a high-risk approval covers the SAME tool for the current task (owner
    # hit 可以 three times for one file creation). Bounds that must still hold: the grant expires,
    # and it never covers a different tool (see test_high_risk_grant_does_not_leak_to_a_different_tool).
    import time

    from yueyue_v3.models import ActionEnvelope, PermissionState
    from yueyue_v3.permissions import PermissionController

    controller = PermissionController()
    state = PermissionState()
    action = ActionEnvelope("execute_command", {"command": "dir"}, "step_1", "high")
    controller.request(state, action)
    assert controller.apply_reply(state, "可以") == "single"
    assert controller.permits(state, action)
    assert controller.permits(state, action)  # same tool, same task - no re-ask
    state.expires_at = time.time() - 1  # once expired, approval is required again
    assert not controller.permits(state, action)


def test_high_risk_grant_covers_same_tool_within_the_task():
    # Live 2026-07-20: one file-creation asked 可以 three times (path check -> python -> command)
    # and still didn't finish. Approving a high-risk tool now covers that tool for THIS task.
    from yueyue_v3.models import ActionEnvelope, PermissionState
    from yueyue_v3.permissions import PermissionController

    controller = PermissionController()
    state = PermissionState()
    first = ActionEnvelope("execute_command", {"command": "mkdir x"}, "step_1", "high")
    controller.request(state, first)
    assert controller.apply_reply(state, "可以") == "single"
    assert controller.permits(state, first)
    second = ActionEnvelope("execute_command", {"command": "echo hi > x/a.txt"}, "step_2", "high")
    assert controller.permits(state, second), "same tool, same task -> no re-ask"


def test_high_risk_grant_does_not_leak_to_a_different_tool():
    from yueyue_v3.models import ActionEnvelope, PermissionState
    from yueyue_v3.permissions import PermissionController

    controller = PermissionController()
    state = PermissionState()
    approved = ActionEnvelope("execute_command", {"command": "dir"}, "step_1", "high")
    controller.request(state, approved)
    controller.apply_reply(state, "可以")
    other = ActionEnvelope("delete_file", {"filename": "a.txt"}, "step_2", "high")
    assert not controller.permits(state, other), "a different high-risk tool still needs approval"


def test_pending_task_note_states_the_truth(tmp_path):
    # The chat turn must be told a task is awaiting approval, or the model invents an answer.
    from yueyue_v3.models import (
        ActionEnvelope,
        GoalContract,
        RequestedOutput,
        StepContract,
        WorkflowStatus,
    )
    from yueyue_v3.providers import ProviderResponse, ScriptedProvider
    from yueyue_v3.runtime import YueYueRuntimeV3

    (tmp_path / "workspace" / "brain").mkdir(parents=True)
    (tmp_path / "workspace" / "brain" / "personality.md").write_text("月月", encoding="utf-8")
    (tmp_path / "workspace" / "brain" / "rules.md").write_text("守規矩", encoding="utf-8")
    rt = YueYueRuntimeV3(tmp_path, ScriptedProvider([ProviderResponse("好", "", [])]), state_dir=tmp_path / "v3")
    # Nothing pending is itself a FACT and must be stated - injecting nothing let the model fill
    # the gap from stale context and claim finished work was 「還沒動手」 (live 2026-07-22).
    idle = rt._pending_task_note()
    assert "沒有任何任務" in idle and "排隊" in idle

    import copy
    goal = GoalContract("建立 Hello.txt", [RequestedOutput("c", "d", True, "text", [])], ["c"])
    wf = rt.workflow_engine.create(goal, [StepContract("s1", "s", "act", "d", ["execute_command"])])
    wf.status = WorkflowStatus.AWAITING_PERMISSION
    with rt.events.writer_scope():
        state = copy.deepcopy(rt.state)
        state.workflow = wf
        state.permission.pending_action = ActionEnvelope("execute_command", {}, "s1", "high")
        state.task_queue = ["建立自我介紹.txt"]
        rt._replace_state(state, "test.seed", "t0")
    note = rt._pending_task_note()
    assert "Hello.txt" in note and "execute_command" in note
    assert "排隊中" in note and "自我介紹" in note
