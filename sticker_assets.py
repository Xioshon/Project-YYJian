
from __future__ import annotations

import contextlib
import json
import os
import random
from collections.abc import Iterable
from pathlib import Path

from agent_protocol import STICKER_MARKER_LABEL
from agent_social import DEFAULT_SOCIAL_STICKER_INDEX

ROOT = Path(__file__).resolve().parent
PROJECT_CACHE_DIR = ROOT / "workspace" / "project_cache"

TELEGRAM_REJECTED_ASSETS = {
    "73aeb6631cb30fbfcc0b820316f8f1ef.gif",
}


def sticker_asset_dirs(extra_dirs: Iterable[str] | None = None) -> list[str]:
    candidates: list[str] = []

    for item in extra_dirs or []:
        if item:
            candidates.append(str(item))

    candidates.extend([
        str(ROOT / "workspace" / "assets" / "stickers"),
    ])

    out: list[str] = []
    for item in candidates:
        if item and os.path.isdir(item) and item not in out:
            out.append(item)

    return out


def safe_sticker_filename(name: str) -> bool:
    low = str(name or "").casefold()
    if os.path.basename(low) in TELEGRAM_REJECTED_ASSETS:
        return False

    # Avoid adult/flirty assets in normal sticker replies.
    # Note: heart/affection wording (愛心/爱心/戀) is intentionally NOT blocked here -
    # cute/heart/affection stickers are a documented allowed persona behavior (see
    # ARCHITECTURE.md / RUNBOOK.md), and this list must stay consistent with
    # agent_social.py's MATURE_MARKERS, which also does not block them.
    blocked = [
        "裸", "色", "sex", "nsfw", "kiss",
    ]

    return not any(marker in low for marker in blocked)


def find_sticker_asset(name: str, extra_dirs: Iterable[str] | None = None) -> str:
    base = os.path.basename(str(name or "").strip())

    if not base or not safe_sticker_filename(base):
        return ""

    for folder in sticker_asset_dirs(extra_dirs):
        path = os.path.join(folder, base)
        # Zero-byte files pass isfile() but Telegram rejects them with "Bad Request: file
        # must be non-empty" (3 such failures sat in the live outbox). Treat empty as absent.
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path

    return ""


def _valid_recent_stickers(extra_dirs: Iterable[str] | None = None) -> tuple[list[str], list[str]]:
    valid: list[str] = []
    removed: list[str] = []
    seen: set[str] = set()

    for item in load_recent_stickers():
        raw = str(item or "").strip()
        name = os.path.basename(raw)
        if name and name not in seen and find_sticker_asset(name, extra_dirs):
            valid.append(name)
            seen.add(name)
        elif raw:
            removed.append(raw)

    return valid, removed


def prune_invalid_recent_stickers(extra_dirs: Iterable[str] | None = None) -> list[str]:
    valid, removed = _valid_recent_stickers(extra_dirs)
    if removed:
        save_recent_stickers(valid)
    return removed


def _recent_stickers_path() -> Path:
    return PROJECT_CACHE_DIR / "recent_stickers.json"


def load_recent_stickers() -> list[str]:
    try:
        data = json.loads(_recent_stickers_path().read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        pass

    return []


def save_recent_stickers(items: list[str]) -> None:
    try:
        PROJECT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _recent_stickers_path().write_text(
            json.dumps(items[:8], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def pick_sticker(
    prompt: str,
    suggested_stickers: list[str] | None = None,
    extra_dirs: Iterable[str] | None = None,
) -> str:
    candidates: list[str] = list(suggested_stickers or [])

    preferred = [
        "Acting cute.png",
        "laugh.gif",
        "雜魚.jpg",
        "cat.jpg",
        "cute.png",
    ]

    with contextlib.suppress(Exception):
        candidates.extend(DEFAULT_SOCIAL_STICKER_INDEX.choose(prompt or "cute", limit=12) or [])

    candidates.extend(preferred)

    # Directory scan is a LAST-RESORT fallback only. Previously every file on disk was always
    # appended to the pool, so random.choice() kept sending unindexed junk (hash-named GIFs,
    # crude/off-persona images) that no tag or suggestion ever selected - the owner noticed and
    # disliked exactly those sends. Curated/suggested candidates must own the pool when they exist.
    if not any(find_sticker_asset(os.path.basename(str(item or "").strip()), extra_dirs) for item in candidates):
        for folder in sticker_asset_dirs(extra_dirs):
            try:
                for name in os.listdir(folder):
                    if name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                        candidates.append(name)
            except Exception:
                pass

    seen: set[str] = set()
    available: list[str] = []

    for item in candidates:
        name = os.path.basename(str(item or "").strip())
        if not name or name in seen:
            continue
        seen.add(name)

        if find_sticker_asset(name, extra_dirs):
            available.append(name)

    if not available:
        return ""

    recent, _removed = _valid_recent_stickers(extra_dirs)
    fresh = [name for name in available if name not in recent]
    pool = fresh or available

    chosen = random.choice(pool)

    new_recent = [chosen] + [x for x in recent if x != chosen]
    save_recent_stickers(new_recent)

    return chosen


def direct_sticker_reply(prompt: str, suggested_stickers: list[str] | None = None) -> dict:
    sticker = pick_sticker(prompt, suggested_stickers)

    if sticker:
        return {
            "content": f"給主人丟一張表情包喵～\n[{STICKER_MARKER_LABEL}: {sticker}]"
        }

    return {
        "content": "我現在找不到可用的表情包檔案，沒有假裝發出去喵。"
    }
