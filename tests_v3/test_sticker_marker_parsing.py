from __future__ import annotations

import pytest

from agent_protocol import screenshot_pattern, sticker_pattern


@pytest.mark.parametrize(
    ("raw", "expected_name", "expected_clean"),
    [
        ("[表情包: happy.tgs]", "happy.tgs", ""),
        ("[sticker: 月月傲嬌]", "月月傲嬌", ""),
        # The model stochastically doubles the brackets; the parser must still extract the name AND
        # leave NO stray bracket artifact in the user-facing text (regression: used to leak "[]").
        ("[[sticker: 月月傲嬌]]", "月月傲嬌", ""),
        ("好哇～\n但主人樂成這樣？[[sticker: 月月傲嬌]]", "月月傲嬌", "好哇～\n但主人樂成這樣？"),
    ],
)
def test_sticker_marker_tolerates_double_brackets(raw, expected_name, expected_clean):
    pattern = sticker_pattern()
    assert pattern.findall(raw) == [expected_name]
    clean = pattern.sub("", raw).strip()
    assert clean == expected_clean
    assert "[" not in clean and "]" not in clean


def test_screenshot_marker_tolerates_double_brackets():
    pattern = screenshot_pattern()
    assert pattern.findall("[[screenshot: main]]") == ["main"]
    clean = pattern.sub("", "看這個[[screenshot: main]]").strip()
    assert clean == "看這個"
    assert "[" not in clean and "]" not in clean
