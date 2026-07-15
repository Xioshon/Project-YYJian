
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from telegram_input import owner_text_from_message, repair_mojibake  # noqa: E402

assert repair_mojibake("主人主要訊息：現在幾點").startswith("主人主要訊息"), "normal text changed"

# Real mojibake: UTF-8 bytes wrongly decoded as GBK, the actual corruption pattern
# Telegram clients on Windows can produce. Built at runtime, not a static literal,
# so this test cannot silently degrade into testing a clean-text no-op.
_original_text = "現在幾點"
_garbled_text = _original_text.encode("utf-8").decode("gbk", errors="strict")
assert _garbled_text != _original_text, "test setup did not actually produce mojibake"
assert repair_mojibake(_garbled_text) == _original_text, "repair_mojibake failed to recover real mojibake"

msg = SimpleNamespace(text="發個表情包", caption=None)
assert owner_text_from_message(msg, "compiled prompt") == "發個表情包"

msg2 = SimpleNamespace(text=None, caption="圖片說明")
assert owner_text_from_message(msg2, "compiled prompt") == "圖片說明"

msg3 = SimpleNamespace(text=None, caption=None)
assert owner_text_from_message(msg3, "系統內容\n主人主要訊息：我好累") == "我好累"

print("PASS telegram_input_check")
