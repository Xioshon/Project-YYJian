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


def test_execute_command_still_needs_per_call_approval():
    # The file-bundle loosening must NOT leak to high-risk tools.
    from yueyue_v3.models import ActionEnvelope, PermissionState
    from yueyue_v3.permissions import PermissionController

    controller = PermissionController()
    state = PermissionState()
    action = ActionEnvelope("execute_command", {"command": "dir"}, "step_1", "high")
    controller.request(state, action)
    assert controller.apply_reply(state, "可以") == "single"
    assert controller.permits(state, action)  # consumes the single grant
    assert not controller.permits(state, action)  # second call must re-ask
