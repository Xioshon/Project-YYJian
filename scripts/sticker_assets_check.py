
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sticker_assets  # noqa: E402
from sticker_assets import (  # noqa: E402
    direct_sticker_reply,
    find_sticker_asset,
    load_recent_stickers,
    pick_sticker,
    prune_invalid_recent_stickers,
    safe_sticker_filename,
    save_recent_stickers,
    sticker_asset_dirs,
)

BAD_TELEGRAM_ASSET = "73AEB6631CB30FBFCC0B820316F8F1EF.gif"

dirs = sticker_asset_dirs()
assert isinstance(dirs, list), dirs
assert any("stickers" in d for d in dirs), dirs

assert safe_sticker_filename("safe_sticker.jpg")
assert not safe_sticker_filename("nsfw_test.png")
assert not safe_sticker_filename(BAD_TELEGRAM_ASSET)
assert not find_sticker_asset(BAD_TELEGRAM_ASSET)

valid_assets: list[Path] = []
for folder in dirs:
    valid_assets.extend(
        item
        for item in sorted(Path(folder).iterdir())
        if item.is_file() and item.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    )

stale_name = "tg_photo_2022_1781693747.jpg"
if valid_assets:
    valid_name = valid_assets[0].name
    assert find_sticker_asset(valid_name), valid_name
    assert not find_sticker_asset(stale_name), stale_name

    original_cache = sticker_assets.PROJECT_CACHE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        sticker_assets.PROJECT_CACHE_DIR = Path(tmp)
        try:
            save_recent_stickers([BAD_TELEGRAM_ASSET, stale_name, valid_name])
            pruned = prune_invalid_recent_stickers()
            assert stale_name in pruned, pruned
            assert BAD_TELEGRAM_ASSET in pruned, pruned
            assert stale_name not in load_recent_stickers(), load_recent_stickers()
            assert BAD_TELEGRAM_ASSET not in load_recent_stickers(), load_recent_stickers()

            original_choice = sticker_assets.random.choice
            sticker_assets.random.choice = lambda pool: pool[0]
            try:
                picked = pick_sticker("cute", [BAD_TELEGRAM_ASSET, stale_name, valid_name])
                assert picked != stale_name, picked
                assert picked != BAD_TELEGRAM_ASSET, picked
                assert find_sticker_asset(picked), picked
            finally:
                sticker_assets.random.choice = original_choice
        finally:
            sticker_assets.PROJECT_CACHE_DIR = original_cache

    original_cache = sticker_assets.PROJECT_CACHE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        sticker_assets.PROJECT_CACHE_DIR = Path(tmp)
        try:
            reply = direct_sticker_reply("\u767c\u500b\u8868\u60c5\u5305", [stale_name, valid_name])
        finally:
            sticker_assets.PROJECT_CACHE_DIR = original_cache
else:
    original_cache = sticker_assets.PROJECT_CACHE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        sticker_assets.PROJECT_CACHE_DIR = Path(tmp)
        try:
            reply = direct_sticker_reply("\u767c\u500b\u8868\u60c5\u5305", [])
        finally:
            sticker_assets.PROJECT_CACHE_DIR = original_cache

assert isinstance(reply, dict), reply
assert "content" in reply, reply
assert stale_name not in reply["content"], reply

print("PASS sticker_assets_check")
