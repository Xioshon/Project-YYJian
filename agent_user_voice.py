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
            f"這一步要跑 `{tool_name}`，會真的動到程式或系統，我先等你點頭喔。\n"
            "你回「可以」我就只跑剛剛那一步；如果是要接回前面的任務，也可以直接說「繼續」。"
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
    return f"主人，這一步看起來不太安全，我已經立刻停下所有操作了。{tag}".strip()


def failure_replay_reply(tool_name: str, replay_name: str = "", trace_file: str = "") -> str:
    lines = [f"`{tool_name}` 連續失敗，我先停住，避免一直重試把狀態弄亂。"]
    if replay_name:
        lines.append(f"已保存 replay case：{replay_name}")
    if trace_file:
        lines.append(f"Trace：{trace_file}")
    return "\n".join(lines)


def tool_loop_timeout_reply() -> str:
    return "主人，我剛剛試了幾輪還沒走通，先乖乖停下來整理狀態。這次不再硬重試，免得同一個工具一直刷屏。"


def empty_reply_fallback() -> str:
    return "主人，我處理好了喔。"


def permission_request_reply(tool_name: str, arguments: dict[str, Any] | None = None) -> str:
    return f"`{tool_name}` 這一步需要你確認一下喔。你回「可以」我就只執行剛剛那一步；回「本輪允許」才會放行同一輪工具。"


def approved_tool_success_reply(tool_name: str, message: str, outcome_summary: str = "", has_artifacts: bool = False) -> str:
    lines = [f"我跑完你剛剛點頭的 `{tool_name}` 了：{message}"]
    if outcome_summary:
        lines.append(outcome_summary)
    if has_artifacts:
        lines.append("如果你要我發送或分析這些產物，直接說「發給我」或「分析一下」就好。")
    return "\n".join(lines)


def approved_tool_blocked_reply(tool_name: str, result: ToolResult) -> str:
    if result.requires_permission:
        return f"`{tool_name}` 這一步還需要你再確認一次喔：{result.message}"
    return friendly_tool_block(tool_name, result)


def approved_tool_error_reply(tool_name: str, result: ToolResult) -> str:
    lines = [f"`{tool_name}` 這次沒跑通，我先把錯誤記下來，避免悶頭亂試。"]
    if result.message:
        lines.append(result.message)
    if result.error:
        lines.append(result.error[:800])
    return "\n".join(lines)
