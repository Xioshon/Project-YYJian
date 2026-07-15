
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from temporal_context import (  # noqa: E402
    attach_temporal_context,
    build_temporal_context,
    build_time_query_reply,
    detect_temporal_contradiction,
)

ctx = build_temporal_context("test_chat")
assert "目前真實時間：" in ctx, ctx
assert "時間段：" in ctx, ctx
assert "使用規則：" in ctx, ctx

prompt = attach_temporal_context("主人主要訊息：測試", "test_chat")
assert "[內部時間感知]" in prompt, prompt
assert "主人主要訊息：測試" in prompt, prompt

prompt2 = attach_temporal_context(prompt, "test_chat")
assert prompt2.count("[內部時間感知]") == 1, prompt2

time_reply = build_time_query_reply()
assert isinstance(time_reply, dict), time_reply
assert "現在是" in time_reply.get("content", ""), time_reply

contradiction = detect_temporal_contradiction("good morning", "night")
assert contradiction, contradiction
assert "good morning" in contradiction, contradiction
assert "night" in contradiction, contradiction

aligned = detect_temporal_contradiction("good evening", "night")
assert aligned == "", aligned

evening_woke = detect_temporal_contradiction(
    "\u65e9\u4e0a\u597d\uff0c\u525b\u9192",
    "\u665a\u4e0a",
)
assert evening_woke, evening_woke
assert "\u65e9\u4e0a" in evening_woke, evening_woke
assert "\u665a\u4e0a" in evening_woke, evening_woke

fixed_afternoon = datetime(2026, 6, 28, 14, 3, 51, tzinfo=timezone(timedelta(hours=8)))
fixed_reply = build_time_query_reply(now=fixed_afternoon)
fixed_content = fixed_reply.get("content", "")
assert "2026-06-28 14:03:51" in fixed_content, fixed_reply
assert "2026-06-25 20:14:18" not in fixed_content, fixed_reply

stale_history_prompt = (
    "主人主要訊息：好的，早上好月月\n\n"
    "[短期上下文]\n"
    "上一輪摘要：現在是 2026-06-25 20:14:18 中國標準時間。晚上好。\n"
    "[/短期上下文]"
)
current_owner_text = "好的，早上好月月"

contaminated_context = build_temporal_context(
    "test_chat_stale_history",
    owner_prompt=current_owner_text,
    now=fixed_afternoon,
)
assert "目前真實時間：2026-06-28 14:03:51" in contaminated_context, contaminated_context
assert "時間段：下午" in contaminated_context, contaminated_context
assert "時間矛盾提示：" in contaminated_context, contaminated_context
assert "早上好" in contaminated_context, contaminated_context
assert "20:14" not in contaminated_context, contaminated_context

attached_stale = attach_temporal_context(
    stale_history_prompt,
    "test_chat_stale_history_attach",
    owner_prompt=current_owner_text,
    now=fixed_afternoon,
)
temporal_block = attached_stale.split("[/內部時間感知]", 1)[0]
assert "時間段：下午" in temporal_block, attached_stale
assert "目前這一輪的真實時間最優先" in temporal_block, attached_stale
assert "歷史上下文裡的時間" in temporal_block, attached_stale
assert "20:14" not in temporal_block, attached_stale
assert "timezone" in temporal_block.casefold() or "鏅傚尯" in temporal_block or "時區" in temporal_block, attached_stale

fixed_evening = datetime(2026, 6, 28, 19, 41, 0, tzinfo=timezone(timedelta(hours=8)))
evening_context = build_temporal_context(
    "test_chat_evening_woke",
    owner_prompt="\u65e9\u4e0a\u597d\uff0c\u525b\u9192",
    now=fixed_evening,
)
assert "2026-06-28 19:41:00" in evening_context, evening_context
assert "\u665a\u4e0a" in evening_context, evening_context
assert "temporal contradiction" in evening_context, evening_context

main_source = (ROOT / "main.py").read_text(encoding="utf-8")
assert "owner_prompt=owner_prompt" in main_source, "main.py must pass current owner text into temporal context"
assert "build_temporal_snapshot(owner_prompt" in main_source, "main.py must debug the current owner-text snapshot"
assert 'kind="wake_greeting"' in main_source, "main.py must keep wake greeting on the pre-v3 fast path"
assert "facts=temporal_snapshot" in main_source, "main.py must pass current temporal snapshot into wake greeting fast reply"

fixed_late_night = datetime(2026, 6, 28, 23, 30, 0, tzinfo=timezone(timedelta(hours=8)))
contradicted_prompt = attach_temporal_context(
    "good morning", "test_chat", owner_prompt="good morning", now=fixed_late_night
)
assert '時間段：深夜' in contradicted_prompt, contradicted_prompt
assert "temporal contradiction" in contradicted_prompt, contradicted_prompt

print("PASS temporal_context_check")
