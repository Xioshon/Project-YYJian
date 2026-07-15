from __future__ import annotations

import pytest

from agent_latency import InteractionMode, classify_interaction
from yueyue_v3.context import classify_turn_mode
from yueyue_v3.models import TurnMode


@pytest.mark.parametrize(
    "text",
    [
        # Live bug 2026-07-14: 設定/菜單/視窗 were listed as BOTH action and target markers, so a
        # single such noun self-satisfied the action+target gate and a casual mention routed to task.
        "欸月月，剛剛把你的設定精簡了一下，現在感覺有沒有清爽點",
        "把你的設定精簡了一下",
        "這個菜單設計得不錯",
        "那個視窗的顏色我不喜歡",
    ],
)
def test_casual_mention_of_setting_menu_window_stays_chat(text: str) -> None:
    assert classify_interaction(text) is InteractionMode.CHAT
    assert classify_turn_mode(text) is TurnMode.CHAT


@pytest.mark.parametrize(
    ("text", "expected_turn_mode"),
    [
        # classify_turn_mode must now agree with classify_interaction (it delegates to it).
        ("打開設定並點擊剩餘用量", TurnMode.TASK),
        ("幫我讀取一下 workspace 的 README", TurnMode.TASK),
        ("搜尋 Python 官方文件", TurnMode.TASK),
        ("debug this", TurnMode.TASK),
        ("把你的設定精簡了", TurnMode.CHAT),
        ("陪我聊聊今天發生的事", TurnMode.CHAT),
        ("只是測試", TurnMode.CHAT),
        # Gap-battery regression 2026-07-15: command-flavored lookups fell through to CHAT and the
        # chat model hallucinated the answer (a fake Python version) with no tool access at all.
        ("用指令查一下這台機器的 Python 版本是多少，告訴我版本號", TurnMode.TASK),
        ("執行一下 python --version", TurnMode.TASK),
        ("查一下 Python 版本", TurnMode.TASK),
        # and the guardrail: target nouns alone without an intent verb stay chat.
        ("這遊戲新版本超好玩", TurnMode.CHAT),
        ("我的機器人設定好可愛", TurnMode.CHAT),
    ],
)
def test_turn_mode_delegates_consistently_to_interaction(text: str, expected_turn_mode: TurnMode) -> None:
    assert classify_turn_mode(text) is expected_turn_mode


@pytest.mark.parametrize(
    "text",
    [
        # Live-test failures 2026-07-12: these routed to CHAT, so the model answered with an
        # empty promise ("行，我去看一眼") it could not keep - CHAT runs no file tools.
        "幫我查一下C:\\Agent這個資料夾裡現在有幾個Python檔案",
        "幫我讀取一下C:\\Agent資料夾，看看裡面現在有幾個Python檔案",
        "幫我按一下那個按鈕",
        "debug this",
    ],
)
def test_natural_task_requests_route_to_tool_task(text: str) -> None:
    assert classify_interaction(text) is InteractionMode.TOOL_TASK


@pytest.mark.parametrize(
    "text",
    [
        # Same verbs without a named target or help-request phrase are ordinary chat.
        "我查過了沒這回事",
        "你去读取一下我的心",
        "我統計學考砸了",
        "測試",
        "只是測試",
        "fastest way there",
        "我今天去考試，好緊張",
        # Live-test failure 2026-07-12: a casual 「有沒有」 triggered a real screenshot loop.
        "你還記得我們今天最早聊了什麼內容欸月月，今天過得怎樣，有沒有無聊嗎？盡量講具體一點",
        "有沒有覺得我很煩",
    ],
)
def test_casual_mentions_stay_chat(text: str) -> None:
    assert classify_interaction(text) is InteractionMode.CHAT


@pytest.mark.parametrize(
    "text",
    [
        # 有沒有/看看 still reach observe-mode when paired with real screen context.
        "看看螢幕現在顯示什麼",
        "幫我看一下屏幕",
        "有沒有程式還在跑",
    ],
)
def test_screen_intent_with_context_still_observes(text: str) -> None:
    assert classify_interaction(text) is InteractionMode.SCREEN_OBSERVE


def test_sticker_request_wins_over_casual_kankan() -> None:
    assert classify_interaction("發個表情包來看看") is InteractionMode.SOCIAL_STICKER
