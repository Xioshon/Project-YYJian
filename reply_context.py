
from __future__ import annotations

import re


def reply_summary_for_context(reply_data: dict) -> str:
    content = str((reply_data or {}).get("content", "") or "")
    report = (reply_data or {}).get("_send_report") or {}

    if not isinstance(report, dict):
        return content

    sticker_failed = int(report.get("sticker_failed", 0) or 0)
    screenshot_failed = int(report.get("screenshot_failed", 0) or 0)

    if sticker_failed <= 0 and screenshot_failed <= 0:
        return content

    cleaned = content

    # Remove media markers that were not confirmed as delivered.
    cleaned = re.sub(r"\[[^\[\]\n]{0,20}表情包\s*:\s*[^\]\n]+\]", "", cleaned)
    cleaned = re.sub(r"\[[^\[\]\n]{0,20}sticker\s*:\s*[^\]\n]+\]", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\[[^\[\]\n]{0,20}screenshot\s*:\s*[^\]\n]+\]", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    notes: list[str] = []
    if sticker_failed:
        notes.append("表情包沒有確認送達，未記作成功發送。")
    if screenshot_failed:
        notes.append("圖片沒有確認送達，未記作成功發送。")

    note = " ".join(notes)

    if not cleaned:
        return note

    return (cleaned + "\n" + note).strip()
