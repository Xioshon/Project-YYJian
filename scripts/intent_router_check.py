from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from intent_router import classify_owner_intent  # noqa: E402

date_time_cases = [
    ("\u73fe\u5728\u662f\u5e7e\u865f\uff1f", "time_query"),
    ("\u73b0\u5728\u662f\u51e0\u53f7\uff1f", "time_query"),
    ("\u73fe\u5728\u5e7e\u865f\uff1f", "time_query"),
    ("\u73b0\u5728\u51e0\u53f7\uff1f", "time_query"),
    ("\u800c\u5bb6\u5e7e\u865f\uff1f", "time_query"),
    ("\u4eca\u65e5\u5e7e\u865f\uff1f", "time_query"),
    ("\u73fe\u5728\u662f\u4ec0\u9ebc\u65e5\u671f\uff1f", "time_query"),
    ("\u4eca\u5929\u5e7e\u865f\uff1f", "time_query"),
    ("\u4eca\u5929\u661f\u671f\u5e7e\uff1f", "time_query"),
    ("\u73fe\u5728\u5e7e\u9ede\uff1f", "time_query"),
    ("\u73fe\u5728\u662f\u4ec0\u9ebc\u6642\u9593\uff1f", "time_query"),
    ("\u73fe\u5728\u662f\u665a\u4e0a\u55ce", "time_query"),
    ("\u73b0\u5728\u662f\u665a\u4e0a\u5417", "time_query"),
    ("\u73fe\u5728\u662f\u767d\u5929\u9084\u662f\u9ed1\u5929", "time_query"),
    ("\u5916\u9762\u662f\u9ed1\u5929\u9084\u662f\u767d\u5929", "time_query"),
    ("\u4eca\u5929\u662f\u767d\u5929\u55ce", "time_query"),
    ("\u73b0\u5728\u662f\u4ec0\u4e48\u65e5\u671f\uff1f", "time_query"),
    ("\u4eca\u5929\u51e0\u53f7\uff1f", "time_query"),
    ("\u4eca\u5929\u661f\u671f\u51e0\uff1f", "time_query"),
    ("\u73b0\u5728\u51e0\u70b9\uff1f", "time_query"),
    ("\u73b0\u5728\u662f\u4ec0\u4e48\u65f6\u95f4\uff1f", "time_query"),
]

cases = date_time_cases + [
    ("現在幾點", "time_query"),
    ("現在是幾點", "time_query"),
    ("你知道現在是多少點嗎？", "time_query"),
    ("所以现在是几点？", "time_query"),
    ("what is the time right now?", "time_query"),
    ("what’s the time now?", "time_query"),
    ("發個表情包", "sticker_send"),
    ("月月，發個表情包", "sticker_send"),
    ("發另外一個表情包", "sticker_send"),
    ("表情包", "sticker_send"),
    ("不要發這個表情包", "sticker_cancel"),
    ("先不要發貼圖", "sticker_cancel"),
    ("取消", "normal_chat"),
    ("幫我看看畫面", "normal_chat"),
    # Regression cases: commenting on/praising a sticker is not a request to send one.
    ("這個表情包好得意", "normal_chat"),
    ("呢個表情包幾得意", "normal_chat"),
    ("你的表情包好可愛", "normal_chat"),
    ("這個表情包點解咁得意", "normal_chat"),
]

failed = 0
for text, expected in cases:
    got = classify_owner_intent(text).kind
    ok = got == expected
    print(("PASS" if ok else "FAIL"), repr(text), "=>", got, "expected", expected)
    if not ok:
        failed += 1

raise SystemExit(failed)
