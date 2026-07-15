from __future__ import annotations

import json

from yueyue_v3.models import V3ToolResult
from yueyue_v3.runtime import _tool_content


def test_tool_payload_preserves_target_element_beyond_long_ui_prefix() -> None:
    elements = [
        {"element_id": str(index), "name": f"無關控件 {index}", "control_type": "Text"} for index in range(1000)
    ]
    elements[900] = {"element_id": "900", "name": "剩餘用量 1%", "control_type": "MenuItem"}
    result = V3ToolResult(
        "ok",
        "observed",
        facts={
            "snapshot_id": "snap-1",
            "revision": "rev-1",
            "ui_elements": elements,
            "visible_text": [item["name"] for item in elements],
        },
    )

    payload = _tool_content(result, ["剩餘用量"])
    decoded = json.loads(payload)

    assert "剩餘用量 1%" in json.dumps(decoded, ensure_ascii=False)
    assert "900" in json.dumps(decoded, ensure_ascii=False)
    assert len(payload) <= 20_000
