from __future__ import annotations

from typing import Any

from core_tools import ToolResult


def friendly_tool_block(tool_name: str, result: ToolResult | None = None, route: str = "") -> str:
    """Translate internal tool routing blocks into user-facing language."""
    message = (result.message if result else "") or ""
    retry_hint = ""
    if result and isinstance(result.data, dict):
        retry_hint = str(result.data.get("retry_hint") or "")

    if tool_name in {"execute_command", "execute_python", "execute_async_command"}:
        return (
            f"我先沒直接跑 `{tool_name}`。這看起來像剛剛那個任務的下一步，"
            "但現在這一輪更像是在聊天追問，所以我不想自己亂接著動手。\n"
            "你回「繼續」我就接回原任務；如果是高風險操作，我會只問一次確認。"
        )

    if tool_name == "analyze_media":
        return "我先不把這個當成看圖任務處理喵。你如果是要我認真看圖，直接說「幫我分析這張圖」就好。"

    if retry_hint:
        return f"我先停在 `{tool_name}` 這一步，避免走錯方向。{retry_hint}"
    if message and "policy" not in message.casefold() and "route" not in message.casefold():
        return f"我先停在 `{tool_name}` 這一步：{message}"
    return f"我先停在 `{tool_name}` 這一步，避免把剛剛的任務和現在的聊天混在一起。"


def repeated_tool_stop_reply(tool_name: str, replay_name: str = "") -> str:
    suffix = f"\n我已經存了最小重現：{replay_name}" if replay_name else ""
    return f"主人，我發現 `{tool_name}` 在同一步重複打轉，所以先停下來了。這種時候硬跑只會越跑越亂。{suffix}"


def failsafe_reply(tag: str = "") -> str:
    return f"主人，我觸發了 fail-safe，已經立刻停止所有操作。{tag}".strip()


def failure_replay_reply(tool_name: str, replay_name: str = "", trace_file: str = "") -> str:
    lines = [f"`{tool_name}` 連續失敗，我先停住，避免一直重試。"]
    if replay_name:
        lines.append(f"已保存 replay case：{replay_name}")
    if trace_file:
        lines.append(f"Trace：{trace_file}")
    return "\n".join(lines)


def tool_loop_timeout_reply() -> str:
    return "主人，我卡在工具迴圈裡了，已停止本輪操作。這次不再繼續重試，避免把同一個工具刷屏。"


def empty_reply_fallback() -> str:
    return "主人，我處理好了喵。"


def permission_request_reply(tool_name: str, arguments: dict[str, Any] | None = None) -> str:
    return f"`{tool_name}` 需要你確認一下。你回「可以」我就只執行剛剛那一步；回「本輪允許」才會放行同一輪工具鏈。"
