from __future__ import annotations

from pathlib import Path

MOJIBAKE_MARKERS = ("鍙", "鐨", "浠", "妯", "銆", "闄", "瑕", "绔", "锛", "浜斿")


def test_v3_source_has_no_mojibake_protocol_or_owner_text() -> None:
    root = Path(__file__).parents[1] / "yueyue_v3"
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in MOJIBAKE_MARKERS):
            offenders.append(path.name)
    assert offenders == []
