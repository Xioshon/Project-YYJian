from __future__ import annotations

import pytest

from agent_latency import InteractionMode, classification_is_grey, classify_interaction
from route_llm import llm_route, llm_routing_enabled


@pytest.mark.parametrize(
    ("text", "expect_grey"),
    [
        # Grey: weak signals present, decided CHAT (the hallucinated-Python-version class).
        ("我的設定檔好像有點怪", True),
        ("這個檔案的名字取得不錯", True),
        # Grey: TOOL_TASK reached only via weak verb+target co-occurrence (the 設定精簡 class).
        ("幫我看一下這個設定漂不漂亮", True),
        # Not grey: pure chat with zero task-ish words.
        ("今天好想吃拉麵", False),
        ("月月你真可愛", False),
        # Not grey: strong markers are certain.
        ("debug this", False),
        ("用指令查一下 Python 版本", False),
        # Not grey: media/sticker attachments are certain signals.
    ],
)
def test_grey_zone_detection(text: str, expect_grey: bool) -> None:
    mode = classify_interaction(text)
    assert classification_is_grey(text, mode) is expect_grey


def test_media_is_never_grey() -> None:
    mode = classify_interaction("看看這張", has_media=True, media_kind="photo")
    assert classification_is_grey("看看這張", mode, has_media=True, media_kind="photo") is False


def test_llm_route_disabled_returns_keyword_mode(monkeypatch) -> None:
    # Explicit opt-in: without YUEYUE_LLM_ROUTING=1 the LLM is never consulted, so the test
    # suite and offline runs stay deterministic and network-free.
    monkeypatch.setenv("YUEYUE_LLM_ROUTING", "0")
    assert not llm_routing_enabled()
    assert llm_route("我的設定檔好像有點怪", InteractionMode.CHAT) is InteractionMode.CHAT


def test_llm_route_failure_keeps_keyword_mode(monkeypatch) -> None:
    # With routing enabled but no reachable API (bogus key + unroutable base), any failure must
    # silently keep the keyword decision - the LLM layer may refine, never break.
    monkeypatch.setenv("YUEYUE_LLM_ROUTING", "1")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "")
    assert llm_route("我的設定檔好像有點怪", InteractionMode.CHAT) is InteractionMode.CHAT
