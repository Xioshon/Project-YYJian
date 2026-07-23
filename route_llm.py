"""Grey-zone LLM routing (ROADMAP Phase 1).

Keyword routing stays the fast path for certain signals (media, strong markers) and the fallback
for everything. When `agent_latency.classification_is_grey` marks a keyword decision as resting on
weak signals - the historical misroute zone in both directions - this module asks the cheap chat
model (MiniMax, ~100 prompt tokens, temperature 0) to re-judge. Any failure, timeout, or
unparseable answer keeps the keyword result, so this can only refine, never break, routing.

Opt-in via YUEYUE_LLM_ROUTING=1 (set in .env). Deliberately explicit so the test suite and
offline runs never touch the network.
"""

from __future__ import annotations

import httpx
from openai import OpenAI

from agent_hooks import emit_trace
from agent_latency import InteractionMode
from core_tools import env_value

_LABELS = {
    "chat": InteractionMode.CHAT,
    "tool_task": InteractionMode.TOOL_TASK,
    "screen_observe": InteractionMode.SCREEN_OBSERVE,
    "vision_task": InteractionMode.VISION_TASK,
}

_PROMPT = (
    "你是訊息意圖分類器。主人發給陪伴 agent 一句話，判斷 agent 應該怎麼接：\n"
    "chat = 閒聊/情緒/評論/提到系統或檔案但只是聊起它，不需要動手\n"
    "tool_task = 要 agent 實際動手：查/跑/數/建檔/寫檔/搜尋/執行指令，"
    "或操作磁碟上的檔案與資料夾（列出、找出、讀取下載夾/桌面/某路徑裡的檔案）。\n"
    "screen_observe = 要 agent 看『螢幕/畫面現在顯示什麼』——目標是當下畫面，不是磁碟上的檔案。\n"
    "vision_task = 要 agent 分析圖片/影像內容\n"
    "重點區別：講到『資料夾/路徑/下載夾/檔名/編輯過的檔案』是操作磁碟 = tool_task，"
    "不是 screen_observe；『看一下』後面接檔案/路徑也是 tool_task。\n"
    "只回一個標籤，不要解釋。\n訊息：{text}"
)


def llm_routing_enabled() -> bool:
    return env_value("YUEYUE_LLM_ROUTING") == "1"


def llm_route(text: str, keyword_mode: InteractionMode) -> InteractionMode:
    """Re-judge a grey-zone message. Returns the keyword mode on any failure."""
    if not llm_routing_enabled():
        return keyword_mode
    api_key = env_value("SILICONFLOW_API_KEY")
    model = env_value("YUEYUE_CHAT_MODEL") or "Pro/MiniMaxAI/MiniMax-M2.5"
    if not api_key:
        return keyword_mode
    try:
        with httpx.Client(timeout=4.0) as http_client:
            client = OpenAI(api_key=api_key, base_url="https://api.siliconflow.cn/v1", http_client=http_client)
            response = client.chat.completions.create(
                model=model,
                max_tokens=8,
                temperature=0,
                messages=[{"role": "user", "content": _PROMPT.format(text=str(text or "")[:500])}],
            )
        label = (response.choices[0].message.content or "").strip().casefold()
        for key, mode in _LABELS.items():
            if key in label:
                if mode != keyword_mode:
                    emit_trace(
                        "route.llm_override",
                        keyword=keyword_mode.value,
                        llm=mode.value,
                        text=str(text or "")[:120],
                    )
                return mode
    except Exception:
        pass
    return keyword_mode
