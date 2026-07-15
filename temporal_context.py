
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CACHE_FILE = ROOT / "workspace" / "project_cache" / "temporal_awareness.json"


def _load_cache() -> dict[str, Any]:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_cache(data: dict[str, Any]) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _period_for_hour(hour: int) -> str:
    if 0 <= hour < 5:
        return "凌晨"
    if 5 <= hour < 11:
        return "早上"
    if 11 <= hour < 14:
        return "中午"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    return "深夜"


def _gap_text(seconds: float | None) -> str:
    if seconds is None:
        return "這是目前可見的第一輪對話"

    if seconds < 60:
        return "上一輪剛剛才發生"
    if seconds < 3600:
        return f"距離上一輪約 {int(seconds // 60)} 分鐘"
    if seconds < 86400:
        return f"距離上一輪約 {int(seconds // 3600)} 小時"
    return f"距離上一輪約 {int(seconds // 86400)} 天"


def _period_bucket(period: str) -> str:
    text = str(period or "").casefold()
    if text in {"凌晨", "深夜", "night", "late night"}:
        return "night"
    if text in {"早上", "morning"}:
        return "morning"
    if text in {"中午", "noon"}:
        return "noon"
    if text in {"下午", "afternoon"}:
        return "afternoon"
    if text in {"晚上", "evening"}:
        return "night"
    return ""


def detect_temporal_contradiction(owner_prompt: str, current_period: str) -> str:
    text = str(owner_prompt or "").casefold()
    period = _period_bucket(current_period)
    if not text or not period:
        return ""

    markers = [
        ("morning", ["good morning", "早安", "早上好"]),
        ("afternoon", ["good afternoon", "下午好"]),
        ("night", ["good evening", "good night", "晚安", "晚上好"]),
    ]
    for claimed_period, phrases in markers:
        matched = next((phrase for phrase in phrases if phrase in text), "")
        if matched and claimed_period != period:
            return (
                "temporal contradiction: user greeting suggests "
                f"{matched!r}, but current period is {current_period!r}. "
                "Answer using the real current period, naturally and briefly."
            )
    just_woke_markers = ["\u525b\u9192", "\u521a\u9192", "\u8d77\u5e8a", "just woke"]
    matched_woke = next((phrase for phrase in just_woke_markers if phrase in text), "")
    if matched_woke and period == "night":
        return (
            "temporal contradiction: user says they just woke, but current period is "
            f"{current_period!r}. Do not treat it as morning; say the real current period "
            "and acknowledge that they just woke."
        )
    return ""


def build_temporal_snapshot(owner_prompt: str = "", now: datetime | None = None) -> dict[str, str]:
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    period = _period_for_hour(current.hour)
    contradiction = detect_temporal_contradiction(owner_prompt, period)
    return {
        "current_time": current.isoformat(timespec="seconds"),
        "display_time": current.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": str(current.tzinfo or ""),
        "period": period,
        "contradiction": contradiction,
    }


def _weekday_name(current: datetime) -> str:
    weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return weekday_names[current.weekday()]


def build_temporal_context(
    chat_id: int | str = "default",
    owner_prompt: str = "",
    now: datetime | None = None,
) -> str:
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    key = str(chat_id)

    cache = _load_cache()
    previous_ts = None

    try:
        previous_ts = float(cache.get(key, {}).get("last_turn_ts"))
    except Exception:
        previous_ts = None

    now_ts = current.timestamp()
    gap = None if previous_ts is None else max(0.0, now_ts - previous_ts)

    cache[key] = {
        "last_turn_ts": now_ts,
        "last_seen": current.isoformat(timespec="seconds"),
    }
    _save_cache(cache)

    weekday = _weekday_name(current)
    snapshot = build_temporal_snapshot(owner_prompt, current)
    period = snapshot["period"]
    contradiction_note = f"時間矛盾提示：{snapshot['contradiction']}\n" if snapshot["contradiction"] else ""

    return (
        "[內部時間感知]\n"
        f"目前真實時間：{current.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"時區 timezone：{current.tzinfo or ''}\n"
        f"星期：{weekday}\n"
        f"時間段：{period}\n"
        f"對話間隔：{_gap_text(gap)}\n"
        f"{contradiction_note}"
        "權威規則：目前這一輪的真實時間最優先；歷史上下文裡的時間、舊摘要、舊對話時間只能視為過去內容，不能覆蓋本輪時間段。"
        "使用規則：這是背景感知，不是使用者要求報時。不要每次主動說時間；只有語境需要時才自然使用。"
        "時間優先級：高於使用者隨口打招呼。若使用者說晚上好、晚安、早安等，但目前時間段不一致，必須以目前真實時間為準自然修正。"
        "禁止輸出與目前時間段矛盾的問候或場景描寫，例如下午不要說晚上好、晚風、晚安、深夜。"
        "不得編造與目前真實時間矛盾的日期或時間。\n"
        "[/內部時間感知]"
    )


def attach_temporal_context(
    prompt: str,
    chat_id: int | str = "default",
    owner_prompt: str = "",
    now: datetime | None = None,
) -> str:
    prompt = str(prompt or "")
    context = build_temporal_context(chat_id, owner_prompt=owner_prompt, now=now)

    if "[內部時間感知]" in prompt:
        return prompt

    return context + "\n\n" + prompt


def build_time_query_reply(now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    period = _period_for_hour(now.hour)
    weekday = _weekday_name(now)
    return {
        "content": f"現在是 {now.strftime('%Y-%m-%d %H:%M:%S')} {now.tzinfo or ''}，今天是{weekday}。",
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "time_with_seconds": now.strftime("%H:%M:%S"),
        "timezone": str(now.tzinfo or ""),
        "weekday": weekday,
        "period": period,
    }
