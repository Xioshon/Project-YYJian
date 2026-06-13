from __future__ import annotations

from typing import Any

from core_tools import ToolResult


def friendly_tool_block(tool_name: str, result: ToolResult | None = None, route: str = "") -> str:
    """Translate internal tool routing blocks into YueYue's own voice."""
    message = (result.message if result else "") or ""
    retry_hint = ""
    if result and isinstance(result.data, dict):
        retry_hint = str(result.data.get("retry_hint") or "")

    if tool_name in {"execute_command", "execute_python", "execute_async_command"}:
        return (
            f"我先不直接跑 `{tool_name}` 喔。這一步會真的動到程式或系統，"
            "我需要你確認一下才安心。\n"
            "你回「可以」我就只執行剛剛那一步；如果是要接回前面的任務，也可以直接說「繼續」。"
        )

    if tool_name == "analyze_media":
        return "我先不把這個當成看圖任務喔。你要我認真看圖的話，直接說「幫我分析這張圖」就好。"

    if retry_hint:
        return f"我先停在 `{tool_name}` 這一步，避免往錯的方向跑。{retry_hint}"
    if message and "policy" not in message.casefold() and "route" not in message.casefold():
        return f"我先停在 `{tool_name}` 這一步：{message}"
    return f"我先停在 `{tool_name}` 這一步，避免把剛剛的任務和現在的聊天混在一起。"


def repeated_tool_stop_reply(tool_name: str, replay_name: str = "") -> str:
    suffix = f"\n我已經存了最小重現：{replay_name}" if replay_name else ""
    return f"主人，我發現 `{tool_name}` 在同一步重複繞圈，所以先停一下。硬跑只會越跑越亂，我乖乖剎車～{suffix}"


def failsafe_reply(tag: str = "") -> str:
    return f"主人，我碰到 fail-safe，已經立刻停下所有操作了。{tag}".strip()


def failure_replay_reply(tool_name: str, replay_name: str = "", trace_file: str = "") -> str:
    lines = [f"`{tool_name}` 連續失敗，我先停住，避免一直重試。"]
    if replay_name:
        lines.append(f"已保存 replay case：{replay_name}")
    if trace_file:
        lines.append(f"Trace：{trace_file}")
    return "\n".join(lines)


def tool_loop_timeout_reply() -> str:
    return "主人，我卡在工具循環裡了，先停下這輪操作。這次不再繼續重試，免得同一個工具一直刷屏。"


def empty_reply_fallback() -> str:
    return "主人，我處理好了喵。"


def permission_request_reply(tool_name: str, arguments: dict[str, Any] | None = None) -> str:
    return f"`{tool_name}` 需要你確認一下喔。你回「可以」我就只執行剛剛那一步；回「本輪允許」才會放行同一輪工具。"
