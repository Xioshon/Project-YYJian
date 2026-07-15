from __future__ import annotations

import pytest

from yueyue_v3.context import classify_turn_mode
from yueyue_v3.models import TurnMode


@pytest.mark.parametrize(
    "text",
    [
        "幫我想一個好聽的名字",
        "幫我解釋一下為什麼天空是藍色",
        "陪我聊聊今天發生的事",
        "你覺得這個想法怎麼樣",
    ],
)
def test_cognitive_and_social_requests_stay_in_chat(text: str) -> None:
    assert classify_turn_mode(text) is TurnMode.CHAT


@pytest.mark.parametrize(
    "text",
    [
        "幫我查看現在的電腦視窗",
        "請讀取 workspace 裡的 README",
        "打開設定並點擊剩餘用量",
        "搜尋 Python 官方文件",
        "幫我截圖目前螢幕",
    ],
)
def test_requests_with_concrete_external_actions_use_task_mode(text: str) -> None:
    assert classify_turn_mode(text) is TurnMode.TASK
