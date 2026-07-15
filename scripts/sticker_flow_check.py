
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sticker_assets  # noqa: E402
from sticker_assets import find_sticker_asset, save_recent_stickers, sticker_asset_dirs  # noqa: E402
from sticker_flow import (  # noqa: E402
    direct_sticker_resend_reply,
    is_sticker_resend,
    last_sticker_name,
)

BAD_TELEGRAM_ASSET = "73AEB6631CB30FBFCC0B820316F8F1EF.gif"

assert is_sticker_resend("\u518d\u767c\u4e00\u6b21", "")
assert is_sticker_resend("\u6211\u770b\u4e0d\u5230", "\u4e0a\u4e00\u5f35\u767c\u4e86\u8868\u60c5\u5305")
assert not is_sticker_resend("\u4e0d\u8981\u518d\u767c\u4e00\u6b21", "\u4e0a\u4e00\u5f35\u767c\u4e86\u8868\u60c5\u5305")
assert not is_sticker_resend("\u53d6\u6d88", "")

dirs = sticker_asset_dirs()
valid_assets: list[Path] = []
for folder in dirs:
    valid_assets.extend(
        item
        for item in sorted(Path(folder).iterdir())
        if item.is_file() and item.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    )

stale_name = "tg_photo_2022_1781693747.jpg"
original_cache = sticker_assets.PROJECT_CACHE_DIR
with tempfile.TemporaryDirectory() as tmp:
    sticker_assets.PROJECT_CACHE_DIR = Path(tmp)
    try:
        if valid_assets:
            valid_name = valid_assets[0].name
            if valid_name == BAD_TELEGRAM_ASSET and len(valid_assets) > 1:
                valid_name = valid_assets[1].name
            save_recent_stickers([BAD_TELEGRAM_ASSET, stale_name, valid_name])
            load_recent = sticker_assets.load_recent_stickers()
            assert last_sticker_name() == valid_name, load_recent

            reply = direct_sticker_resend_reply("\u518d\u767c\u4e00\u6b21", [])
            content = reply["content"]
            marker_match = re.search(r"\[(?:\u8868\u60c5\u5305|\u8cbc\u5716|\u8d34\u56fe|sticker):\s*([^\]]+)\]", content, re.I)
            assert marker_match, reply
            assert marker_match.group(1).strip() != stale_name, reply
            assert marker_match.group(1).strip() != BAD_TELEGRAM_ASSET, reply
            assert find_sticker_asset(marker_match.group(1).strip()), reply
        else:
            save_recent_stickers([BAD_TELEGRAM_ASSET, stale_name])
            assert last_sticker_name() == ""
    finally:
        sticker_assets.PROJECT_CACHE_DIR = original_cache

name = last_sticker_name()
assert isinstance(name, str)

reply = direct_sticker_resend_reply("\u518d\u767c\u4e00\u6b21", [])
assert isinstance(reply, dict), reply
assert "content" in reply, reply

print("PASS sticker_flow_check")
