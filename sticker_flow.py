
from __future__ import annotations

from agent_protocol import STICKER_MARKER_LABEL
from telegram_input import repair_mojibake
from sticker_assets import (
    load_recent_stickers,
    find_sticker_asset,
    prune_invalid_recent_stickers,
    pick_sticker,
)


def is_sticker_resend(owner_prompt: str, compiled_prompt: str = "") -> bool:
    text = repair_mojibake(owner_prompt).casefold().strip()
    context = repair_mojibake(compiled_prompt).casefold()

    negative = [
        "不要", "不用", "唔好", "別", "别", "取消", "stop",
        "do not", "don't", "dont",
    ]
    if any(marker in text for marker in negative):
        return False

    resend_markers = [
        "再發一次", "再发一次", "重發", "重发", "補發", "补发",
        "重新發", "重新发", "再來一張", "再来一张",
        "看不到", "睇唔到", "沒收到", "没收到",
        "圖沒出", "图没出", "圖片沒出", "图片没出",
        "send again", "resend",
    ]

    sticker_targets = [
        "表情包", "貼圖", "贴图", "sticker", "圖", "图", "圖片", "图片",
    ]

    if any(marker in text for marker in resend_markers):
        return True

    visibility_markers = [
        "看不到", "睇唔到", "沒看到", "没看到", "沒收到", "没收到",
        "圖沒出", "图没出",
    ]

    recent_sticker_context = any(target in context for target in sticker_targets)

    return any(marker in text for marker in visibility_markers) and recent_sticker_context


def last_sticker_name() -> str:
    prune_invalid_recent_stickers()
    for item in load_recent_stickers():
        name = str(item or "").strip().split("\\")[-1].split("/")[-1]
        if name and find_sticker_asset(name):
            return name

    return ""


def direct_sticker_resend_reply(
    owner_prompt: str,
    suggested_stickers: list[str] | None = None,
) -> dict:
    name = last_sticker_name()

    if not name:
        name = pick_sticker(owner_prompt, suggested_stickers or [])

    if name:
        return {
            "content": f"我再補發一次。\n[{STICKER_MARKER_LABEL}: {name}]"
        }

    return {
        "content": "我現在找不到可重發的表情包檔案，先不假裝發出。"
    }
