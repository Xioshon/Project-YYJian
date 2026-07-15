from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ASSETS = ROOT / "workspace" / "assets"
REPORT = ROOT / "workspace" / "project_cache" / "system_audit.json"
TEXT_SUFFIXES = {".py", ".md", ".json", ".jsonl", ".toml", ".txt", ".ps1", ".bat"}
MOJIBAKE_MARKERS = ("鍙", "鐨", "浠", "妯", "銆", "闄", "瑕", "绔", "锛", "浜斿")
EXCLUDED_PARTS = {".git", "__pycache__", "Agent-Backups", "project_cache", "logs", "chat_history"}
DEPENDENCIES = ("PIL", "playwright", "yt_dlp", "telebot", "win32gui", "pywinauto", "pyautogui")


def _is_runtime_text(path: Path) -> bool:
    if any(part in EXCLUDED_PARTS for part in path.parts):
        return False
    if path.name == ".env":
        return False
    return path.suffix.casefold() in TEXT_SUFFIXES or path.name == ".env.example"


def _user_facing_lines(path: Path, text: str) -> str:
    if path.name.startswith("test_"):
        return ""
    ignored = ("MOJIBAKE_MARKERS", "mojibake_hits", "looks_mojibake")
    return "\n".join(line for line in text.splitlines() if not any(marker in line for marker in ignored))


def _check_text() -> tuple[int, list[str]]:
    checked = 0
    failures: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or not _is_runtime_text(path):
            continue
        checked += 1
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            failures.append(f"text_decode:{path.relative_to(ROOT)}:{type(exc).__name__}")
            continue
        visible = _user_facing_lines(path, text)
        marker = next((item for item in MOJIBAKE_MARKERS if item in visible), "")
        if marker:
            failures.append(f"mojibake:{path.relative_to(ROOT)}:{marker}")
    return checked, failures


def _check_assets() -> tuple[int, list[str]]:
    checked = 0
    failures: list[str] = []
    for path in ASSETS.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.casefold()
        try:
            if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                with Image.open(path) as image:
                    image.verify()
                checked += 1
            elif suffix == ".webm":
                if path.stat().st_size <= 16 or path.read_bytes()[:4] != b"\x1aE\xdf\xa3":
                    raise ValueError("invalid EBML header")
                checked += 1
            elif suffix == ".json":
                json.loads(path.read_text(encoding="utf-8-sig"))
                checked += 1
        except (OSError, UnicodeError, ValueError) as exc:
            failures.append(f"asset:{path.relative_to(ROOT)}:{type(exc).__name__}:{exc}")
    return checked, failures


def _check_indexes() -> tuple[int, list[str]]:
    checked = 0
    failures: list[str] = []
    for name in ("stickers_index.json", "social_sticker_index.json"):
        path = ASSETS / name
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError) as exc:
            failures.append(f"index:{name}:{type(exc).__name__}:{exc}")
            continue
        if not isinstance(payload, dict):
            failures.append(f"index:{name}:not_an_object")
            continue
        for filename, item in payload.items():
            checked += 1
            candidates = [ASSETS / "stickers" / filename, ASSETS / "tg_images" / filename]
            if isinstance(item, dict) and item.get("original_path"):
                candidates.insert(0, Path(str(item["original_path"])))
            if not any(candidate.is_file() for candidate in candidates):
                failures.append(f"index_missing:{name}:{filename}")
    return checked, failures


def _check_dependencies() -> tuple[int, list[str]]:
    failures: list[str] = []
    for name in DEPENDENCIES:
        try:
            importlib.import_module(name)
        except Exception as exc:
            failures.append(f"dependency:{name}:{type(exc).__name__}:{exc}")
    return len(DEPENDENCIES), failures


def _check_tools() -> tuple[int, list[str]]:
    import core_tools

    names = [tool.name for tool in core_tools.ALL_TOOLS]
    failures: list[str] = []
    if len(names) != 30:
        failures.append(f"tools:expected_30:found_{len(names)}")
    if len(set(names)) != len(names):
        failures.append("tools:duplicate_names")
    for tool in core_tools.ALL_TOOLS:
        if not isinstance(tool.parameters, dict) or tool.parameters.get("type") != "object":
            failures.append(f"tools:invalid_schema:{tool.name}")
    return len(names), failures


def build_report() -> dict[str, Any]:
    checks = {
        "text": _check_text(),
        "assets": _check_assets(),
        "indexes": _check_indexes(),
        "dependencies": _check_dependencies(),
        "tools": _check_tools(),
    }
    failures = [failure for _count, rows in checks.values() for failure in rows]
    return {
        "status": "pass" if not failures else "fail",
        "counts": {name: count for name, (count, _rows) in checks.items()},
        "failures": failures,
    }


def write_report(report: dict[str, Any], path: Path = REPORT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only YueYue source, asset and dependency audit")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    report = build_report()
    if not args.no_write:
        write_report(report)
    print("YueYue System Audit")
    print(f"Status: {report['status']}")
    for name, count in report["counts"].items():
        print(f"{name}: {count}")
    for failure in report["failures"]:
        print(f"FAIL: {failure}")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
