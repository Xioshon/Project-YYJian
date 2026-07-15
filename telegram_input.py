
from __future__ import annotations


def mojibake_score(text: str) -> int:
    text = str(text or "")

    bad_codepoints = (
        0x6D93, 0x93AC, 0x9360, 0x951B, 0x941C,
        0x953A, 0xFFFD,
    )

    score = 0

    for cp in bad_codepoints:
        score -= text.count(chr(cp)) * 5

    good_markers = (
        "主人", "訊息", "消息", "現在", "表情包", "貼圖", "月月",
        "幾點", "几点", "不要", "再發", "聊天",
    )

    for marker in good_markers:
        if marker in text:
            score += 8

    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    score += min(cjk, 20)

    return score


def repair_mojibake(text: str) -> str:
    original = str(text or "")
    if not original:
        return original

    best = original
    best_score = mojibake_score(best)

    # Common case: UTF-8 text was decoded as GBK/CP936.
    for enc in ("gb18030", "gbk", "cp936"):
        try:
            candidate = original.encode(enc, errors="ignore").decode("utf-8", errors="ignore")
        except Exception:
            continue

        score = mojibake_score(candidate)
        if score > best_score:
            best = candidate
            best_score = score

    return best


def owner_text_from_message(message, prompt: str) -> str:
    raw = ""

    for attr in ("text", "caption"):
        value = getattr(message, attr, None)
        if value:
            raw = str(value)
            break

    if raw:
        return repair_mojibake(raw).strip()

    text = repair_mojibake(prompt)

    markers = [
        "主人主要訊息：",
        "主人主要讯息：",
        "主人訊息：",
        "主人消息：",
        "User:",
        "user:",
    ]

    for marker in markers:
        if marker in text:
            return text.split(marker, 1)[1].strip()

    return text.strip()
