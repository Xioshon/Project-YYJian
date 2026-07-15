from __future__ import annotations

from yueyue_v3.runtime import _chat_reply_violates_social_policy, _estimated_telegram_bubbles


def test_owner_preferred_soft_cadence_passes_the_gate():
    # 2026-07-14: the owner gave these as their ideal replies; the CHAT gate was wrongly
    # rejecting them (a soft 。。。 and a multi-clause single line were mis-counted as several
    # bubbles / too much punctuation). They must pass so the persona can actually be this soft.
    assert not _chat_reply_violates_social_policy(
        "是嘛。。。辛苦了 主人喵。。。快過來讓月月抱一下", "今天好累啊"
    )
    assert not _chat_reply_violates_social_policy(
        "欸欸！？怎麼可能，今天主人你根本沒給月月任務吧，還是我記錯了喵？", "你今天是不是又偷懶沒做事"
    )


def test_soft_ellipsis_line_counts_as_one_bubble():
    # A single spoken line with a soft trailing ellipsis or a few sentence-enders is ONE
    # Telegram message (delivery splits on newlines), not several.
    assert len(_estimated_telegram_bubbles("是嘛。。。辛苦了 主人喵。。。")) == 1
    assert len(_estimated_telegram_bubbles("欸欸！？怎麼可能？還是我記錯了喵？")) == 1


def test_genuine_multi_newline_monologue_still_flagged():
    # The real intent - don't dump a wall of separate messages - is enforced via newlines.
    assert _chat_reply_violates_social_policy("第一句話。\n第二句話。\n第三句話。\n第四句話。", "hi")


def test_genuinely_choppy_over_punctuation_still_flagged():
    # Collapsing only merges *consecutive identical* enders, so a truly choppy reply with many
    # distinct sentence-enders is still caught.
    assert _chat_reply_violates_social_policy("好！真的？是嗎！為何？怎樣！行嗎？", "hi")
