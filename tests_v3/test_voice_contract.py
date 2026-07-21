from __future__ import annotations

import pytest

from voice_contract import (
    VOICE_REGISTER_EN,
    VOICE_REGISTER_ZH,
    owner_text_is_simplified,
    voice_register_violation,
)


@pytest.mark.parametrize(
    "text",
    [
        # Live leak 2026-07-12: spoken Cantonese in a task reply
        "主人，C:\\Agent 嘅 file list 掃完，順利完成",
        "Python 檔案數量喺列表上面可以直接睇到㗎",
        "真係冇你符，發多次俾你",
        # Live leak 2026-07-13: MiniMax permission reply with Cantonese the single-char set missed
        "我想在 C:\\Agent 度撳下有幾多個 .py 檔案",
        "呢個要唔要幫你搞掂",
        "邊個檔案先？",
        # Taiwan-flavored final particles
        "今天是星期日喔",
        "這麼想要喔，再補一張",
        "好可愛耶！",
        # Raw Simplified where Traditional was requested
        "我这边刚刚断了一下，不过还在",
    ],
)
def test_register_violations_are_caught(text: str) -> None:
    assert voice_register_violation(text) != ""


@pytest.mark.parametrize(
    "text",
    [
        # Clean written-HK register with mainland internet rhythm
        "行吧，看你態度不錯就原諒你",
        "笑死，明明是你自己遲鈍",
        "屏幕上開著網易雲在播歌，要暫停我可以幫你按",
        "再煩也別熬夜，聽到沒，笨蛋主人要再熬月月可不理你",
        "也不是不行啦……看你今日表現怎麼樣咯",
        # 喲 sentence-initial (mainland exclamation) is allowed - only FINAL particles are gated
        "喲不是，這個點才想起月月？",
        # 耶 inside a word is not a particle
        "耶穌誕生的故事我倒是知道一點",
        # Owner-mirroring: Simplified allowed when flagged
    ],
)
def test_clean_register_passes(text: str) -> None:
    assert voice_register_violation(text) == ""


def test_cyrillic_leak_is_a_violation() -> None:
    # Live gap-battery leak 2026-07-15: DeepSeek emitted 「我想跑一段 Python код 來數一下」in an
    # owner-facing permission request.
    assert voice_register_violation("我想跑一段 Python код 來數一下").startswith("cyrillic:")


def test_new_simplified_tripwire_chars_from_live_leaks() -> None:
    # 「数一数」/「快记好啦」 reached the owner in the gap battery - none of their chars were gated.
    assert voice_register_violation("帮你数一数").startswith("simplified_chars:")
    assert voice_register_violation("笨笨的主人快记好啦").startswith("simplified_chars:")


def test_japanese_shinjitai_leak_is_a_violation() -> None:
    # Live leak 2026-07-15: 「確認一下你想我実行嗎」- 実 is Japanese, neither Trad nor Simplified.
    assert voice_register_violation("確認一下你想我実行嗎").startswith("japanese_chars:")
    assert voice_register_violation("転送給你").startswith("japanese_chars:")


def test_new_cantonese_task_reply_bigrams() -> None:
    # Live leak 2026-07-15: 「確認既話我就開始郁手」/「讀返確認內容」 in a task permission reply.
    assert voice_register_violation("確認既話我就開始郁手").startswith("cantonese_phrase:")
    assert voice_register_violation("我讀返確認內容").startswith("cantonese_phrase:")
    # 郁/既/返 alone stay legitimate written Chinese.
    assert voice_register_violation("香氣濃郁，既然如此就返回主頁") == ""


def test_simplified_allowed_when_owner_writes_simplified() -> None:
    reply = "我这就来陪你聊"
    assert voice_register_violation(reply) != ""
    assert voice_register_violation(reply, allow_simplified=True) == ""
    assert owner_text_is_simplified("你还在吗")
    assert not owner_text_is_simplified("你還在嗎")


def test_prompt_sites_all_import_the_shared_contract() -> None:
    """The register instruction must come from voice_contract, never a hand-copied paraphrase.
    Guard: every known prompt site references the shared constant."""
    import agent_presence
    import response_composer

    assert VOICE_REGISTER_ZH in agent_presence.PRESENCE_COMPOSER_SYSTEM_PROMPT
    assert VOICE_REGISTER_EN  # imported successfully - the EN constant exists and is non-empty
    # Source-level check: no file may keep its own hand-written copy of the register line.
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    for name in ("response_composer.py", "agent_presence.py", "yueyue_v3/context.py", "yueyue_v3/runtime.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "VOICE_REGISTER" in source, f"{name} must import the shared voice contract"
        assert "字感：香港書面繁體" not in source, f"{name} still carries a hand-copied register line"
    assert response_composer.voice_register_violation is voice_register_violation


def test_fallback_pools_pass_their_own_gate() -> None:
    """Every hardcoded fallback line shipped in response_composer must itself satisfy the
    register gate - a fallback that trips the gate would loop or leak."""
    import response_composer as rc

    for line in rc._sticker_send_options() + rc._sticker_resend_options():
        assert voice_register_violation(line) == "", line
    for options in (rc._plain_greeting_options("hi"), rc._plain_greeting_options("你好")):
        for line in options:
            assert voice_register_violation(line) == "", line


def test_social_chat_fallback_is_register_clean_on_every_branch() -> None:
    """The last-resort social fallback must itself pass the register gate. Eval finding 2026-07-15:
    the DEFAULT branch ended in 喔 (Taiwan particle) - the fallback that backstops register
    violations was itself a register violation, so a genuinely un-fixable model reply leaked 喔 to
    the owner. Drive every branch (plus the default) and assert clean."""
    from yueyue_v3.runtime import _social_chat_fallback

    branch_triggers = [
        "你像機器人",
        "陪我聊一下",
        "只是測試",
        "最近一直調你真的好累",
        "有點煩",
        "今天好累",
        "隨便講點什麼都不匹配觸發預設分支",  # default
    ]
    for owner_text in branch_triggers:
        line = _social_chat_fallback(owner_text)
        assert voice_register_violation(line) == "", f"{owner_text!r} -> {line!r}"


def test_simplified_leaks_are_repaired_not_rejected() -> None:
    """Owner requirement 2026-07-16: the canned permission line kept appearing because Simplified
    leaks burned both generation attempts. Simplified is the one register violation that is
    mechanically fixable - repair converts it, the gate then passes, no canned fallback."""
    from voice_contract import repair_simplified_script

    repaired = repair_simplified_script("我先处理一下，写个东西给你，条理很清楚")
    assert repaired == "我先處理一下，寫個東西給你，條理很清楚"
    assert voice_register_violation(repaired) == ""
    # non-Chinese and already-Traditional text pass through untouched
    assert repair_simplified_script("hello 月月") == "hello 月月"
    assert repair_simplified_script("我先處理一下") == "我先處理一下"


def test_quick_ack_pools_are_varied_and_register_clean() -> None:
    """Owner 2026-07-21: 「我先處理一下」was a service-desk catchphrase repeated on every task.
    Pools give variety with zero latency - and every line must pass the register gate (the gate
    caught 「等我一下下喔」 during this very change)."""
    from agent_latency import QUICK_ACK_POOLS, InteractionMode, quick_ack_for

    for mode, pool in QUICK_ACK_POOLS.items():
        assert len(pool) >= 2, mode
        for line in pool:
            assert voice_register_violation(line) == "", f"{mode}: {line}"
    seen = {quick_ack_for(InteractionMode.TOOL_TASK) for _ in range(40)}
    assert len(seen) >= 2, "the ack must actually vary"


def test_cantonese_grammar_shapes_from_task_reply() -> None:
    """Live 2026-07-22 task reply: 「驗證既時候…可能係檔案…幫到你」. 既 as the possessive 嘅 and
    係 as the copula are the two shapes the single-char set cannot catch (既然/關係 are ordinary
    Mandarin), so they are gated as high-precision bigrams instead."""
    assert voice_register_violation("驗證既時候讀取失敗").startswith("cantonese_phrase:")
    assert voice_register_violation("可能係檔案的問題").startswith("cantonese_phrase:")
    assert voice_register_violation("有其他可以幫到你").startswith("cantonese_phrase:")
    # Ordinary written Chinese using the same characters must stay clean.
    for clean in ("關係很好", "係數是二", "既然如此就這樣", "既定方針", "這個問題不大", "應該是這樣"):
        assert voice_register_violation(clean) == "", clean
