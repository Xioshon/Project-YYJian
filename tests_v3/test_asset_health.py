from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "workspace" / "assets"


def test_every_static_visual_asset_is_decodable() -> None:
    checked = 0
    for path in ASSETS.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            continue
        with Image.open(path) as image:
            image.verify()
        checked += 1
    assert checked >= 100


def test_asset_indexes_are_utf8_json_and_reference_existing_files() -> None:
    for name in ("stickers_index.json", "social_sticker_index.json"):
        payload = json.loads((ASSETS / name).read_text(encoding="utf-8-sig"))
        assert isinstance(payload, dict)
        for filename, item in payload.items():
            # original_path values are absolute paths recorded on the machine that imported
            # the sticker, so they break when the repo moves. Accept the asset if ANY known
            # location has it - same candidate pattern scripts/system_audit.py uses.
            candidates = [ASSETS / "stickers" / filename]
            if isinstance(item, dict) and item.get("original_path"):
                candidates.append(Path(item["original_path"]))
            assert any(candidate.is_file() for candidate in candidates), (name, filename)


def test_webm_assets_have_ebml_headers() -> None:
    files = list(ASSETS.rglob("*.webm"))
    assert files
    for path in files:
        assert path.stat().st_size > 16
        assert path.read_bytes()[:4] == b"\x1aE\xdf\xa3"


def test_runtime_dependencies_cover_asset_and_url_pipeline() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").casefold()
    assert "pillow" in requirements
    assert "playwright" in requirements
    assert "yt-dlp" in requirements
