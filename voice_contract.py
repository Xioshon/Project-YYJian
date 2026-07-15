"""Single source of truth for YueYue's lexical register (字感).

Owner standard (2026-07-11, verbatim requirements): Hong Kong WRITTEN Traditional Chinese
(香港繁中, not 台灣繁中), with mainland-internet chat rhythm as the reference feel. Two failure
directions, both defects:
  1. Taiwan-flavored particles as habitual line endings (喔/喲/耶).
  2. SPOKEN Cantonese leaking in (嘅/喺/㗎/咗/唔/冇...) - 書面港繁 is not 口語粵語. This leak
     was observed live on 2026-07-12 in a task reply.
語氣詞 (啦/咯/嘛) are welcome but sparing - the owner explicitly corrected "太多咯了". Teasing
stays modest, never 浮誇. Canned/template lines are the owner's top pet hate - model-written
lines always, pools only as last-resort fallback.

Every prompt that generates an owner-facing YueYue line must include VOICE_REGISTER_ZH (for
Chinese prompts) or VOICE_REGISTER_EN (for English prompts), imported from here - never a
hand-copied paraphrase. Output-side, run replies through voice_register_violation() and
retry/fallback on a hit. ARCHITECTURE.md's sync-point list points at this module.
"""

from __future__ import annotations

import re

VOICE_REGISTER_ZH = (
    "字感：香港書面繁體+內地網聊語感——用「屏幕/網絡/軟件/視頻/信息」這一路的詞，"
    "不用台灣腔的「螢幕/網路/軟體」；語尾別用「喔/喲/耶」這類台味助詞，直接收句就好。"
    "書面繁體不等於粵語口語：不要寫「嘅/喺/㗎/咗/唔/冇/睇/嚟/俾/佢/撳/呢個/幾多/邊個/點解」"
    "這類粵語字詞，要用通用書面中文寫（這個/多少/哪個/為什麼）。語氣詞（啦/咯/嘛）可以用但要少，一句頂多一個；"
    "內地網絡梗（笑死/絕了/無語/emmm）可以自然出現但別堆砌。"
)

VOICE_REGISTER_EN = (
    "Lexical register: Hong Kong WRITTEN Traditional Chinese with mainland-internet chat "
    "rhythm - prefer 屏幕/網絡/軟件/視頻/信息 over Taiwan-style 螢幕/網路/軟體, and avoid "
    "Taiwan-flavored particles like 喔/喲/耶 as habitual line endings. Written register is "
    "NOT spoken Cantonese: never use Cantonese-only words such as 嘅/喺/㗎/咗/唔/冇/睇/嚟/俾/"
    "佢/撳/呢個/幾多/邊個/點解 - write standard written Chinese (這個/多少/哪個/為什麼). Tone "
    "particles (啦/咯/嘛) are welcome but "
    "sparing, at most one per line. Mainland internet expressions (笑死/絕了/無語/真的假的/"
    "？？？/哈哈哈哈/emmm) may appear naturally but at most one per line, never stacked."
)

# Cantonese-only characters that never appear in the written register YueYue should use.
# Deliberately high-precision: each of these is unambiguous spoken Cantonese on its own.
# (Ambiguous chars like 係/咁/點 are excluded - 關係/係數/點解 appear in ordinary written text
# or in the OWNER's own wording being quoted back.)
# Every char here is essentially zero-usage in written Mandarin. Deliberately excludes chars
# that look Cantonese but have legitimate written uses: 喇(喇叭), 掂(掂量), 郁(濃郁), 咁/幾/邊/
# 點/呢 (Mandarin particles or common chars) - those ambiguous ones are handled by bigrams below.
_CANTONESE_ONLY_CHARS = "嘅喺㗎咗嘢咩嗰嚟俾冇睇諗餸嗌攞噉佢乜撳畀嘥攰冚嚡唔"

# Cantonese phrases whose individual characters are ambiguous (呢 is a Mandarin sentence
# particle, 幾/邊/點/咁 are common Mandarin chars) but whose bigram is unambiguously spoken
# Cantonese. Live leak 2026-07-13: MiniMax produced 「度撳下有幾多個」/「呢個」 in a permission
# reply and the single-char set missed it. These are written 這個/多少/哪個/怎樣/為什麼 instead.
_CANTONESE_BIGRAMS = (
    "呢個", "呢啲", "呢度", "幾多", "邊個", "邊度", "咁樣", "點解", "乜嘢", "係咪",
    # Live gap-battery leak 2026-07-15: 「確認既話我就開始郁手」/「讀返確認」 reached the owner in
    # a task permission reply. 郁/既/返 are legitimate written chars alone, so bigrams only.
    "郁手", "既話", "讀返", "睇返", "做返",
    # Second wave, same day: 「入面寫」「就咁多嘞」in task replies. NOTE 入面/就咁 were tried and
    # removed - they false-positive on ordinary Mandarin (進入面試/成就咁高), and this list is
    # deliberately high-precision. Only the unambiguous ones stay.
    "咁多", "既檔案", "係咁",
)

# Taiwan-flavored particles rejected only in the sentence/line-final position (possibly
# followed by punctuation) - mid-word occurrences like 耶誕/喲呵 quoting are not the target.
_TAIWAN_FINAL_PARTICLE_RE = re.compile(r"[喔喲耶](?=[\s。！？!?~～…，,]|$)")

# A small set of high-frequency, unambiguous Simplified-only characters (each one's
# Traditional form differs, and the char itself has no common Traditional usage - e.g. 后
# and 体 are deliberately excluded because 皇后/影后 are legitimate Traditional). This is a
# tripwire, not a full script detector - yueyue_v3.context owns proper script mirroring;
# this only catches raw Simplified leaking out of a model that was asked for Traditional.
# 数/记/个/为/过/让/点/写 added from gap-battery live leaks 2026-07-15 (「数一数」「快记好啦」
# reached the owner-facing reply because none of their chars were in the tripwire).
# 实/么/边/远 added 2026-07-16: a live P2 test reply contained 「什么的」「实在不行」 and none of
# those chars were gated.
# 听/条/难 added same day: 「听到了哦」 passed the Tier-2 register check unflagged.
# 处/办/务/开/关 added from live Telegram task-voice leak 2026-07-16 (「我先处理一下」).
_SIMPLIFIED_ONLY_CHARS = "这来对时说话现发见还没头买东习级练热压历数记个为过让点写实么边远听条难处办务开关"

# DeepSeek/MiniMax occasionally emit stray Cyrillic mid-sentence when writing Chinese (live
# gap-battery leak 2026-07-15: 「我想跑一段 Python код 來數一下…」 in a permission request).
# Any Cyrillic at all in an owner-facing line is a defect.
_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")

# Japanese shinjitai forms that are neither Traditional nor Simplified Chinese - each has a
# distinct codepoint from both (実/實/实, 気/氣/气...). Live leak 2026-07-15: 「你想我実行嗎」.
_JAPANESE_SHINJITAI_CHARS = "実気転読図経済駅様"


def voice_register_violation(text: str, allow_simplified: bool = False) -> str:
    """Return a short reason string if `text` violates the owner's register, else "".

    Callers gate model OUTPUT with this, before any owner-script mirroring. Pass
    allow_simplified=True when the owner's own current message is Simplified - the reply
    deliberately mirrors their script then, so Simplified is not a defect in that turn.
    """
    value = str(text or "")
    if not value:
        return ""
    hits = [ch for ch in _CANTONESE_ONLY_CHARS if ch in value]
    if hits:
        return f"cantonese_chars:{''.join(hits[:5])}"
    bigram_hits = [phrase for phrase in _CANTONESE_BIGRAMS if phrase in value]
    if bigram_hits:
        return f"cantonese_phrase:{'/'.join(bigram_hits[:3])}"
    match = _TAIWAN_FINAL_PARTICLE_RE.search(value)
    if match:
        return f"taiwan_particle:{match.group(0)}"
    cyrillic = _CYRILLIC_RE.search(value)
    if cyrillic:
        return f"cyrillic:{cyrillic.group(0)}"
    japanese = [ch for ch in _JAPANESE_SHINJITAI_CHARS if ch in value]
    if japanese:
        return f"japanese_chars:{''.join(japanese[:5])}"
    if not allow_simplified:
        simplified = [ch for ch in _SIMPLIFIED_ONLY_CHARS if ch in value]
        if simplified:
            return f"simplified_chars:{''.join(simplified[:5])}"
    return ""


def repair_simplified_script(text: str) -> str:
    """Deterministic Simplified→Traditional repair for output-side use, BEFORE gating.

    Simplified leaks are the most common register violation and the only mechanically fixable
    one - repairing instead of rejecting saves a regeneration round and avoids the canned
    fallback. Lazy import: voice_contract is imported by yueyue_v3.context, so the character
    table is pulled at call time, not import time."""
    try:
        from yueyue_v3.context import to_traditional_script

        return to_traditional_script(str(text or ""))
    except Exception:
        return str(text or "")


def owner_text_is_simplified(owner_text: str) -> bool:
    """Cheap same-turn check used to decide allow_simplified for the gate."""
    value = str(owner_text or "")
    return any(ch in value for ch in _SIMPLIFIED_ONLY_CHARS)
