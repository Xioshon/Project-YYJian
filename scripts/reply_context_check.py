
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reply_context import reply_summary_for_context  # noqa: E402

normal = reply_summary_for_context({"content": "正常回覆", "_send_report": {"sticker_failed": 0}})
assert normal == "正常回覆", normal

failed = reply_summary_for_context({
    "content": "給你\n[表情包: test.png]",
    "_send_report": {"sticker_failed": 1},
})
assert "[表情包:" not in failed, failed
assert "未記作成功發送" in failed, failed

print("PASS reply_context_check")
