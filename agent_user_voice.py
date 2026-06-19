from __future__ import annotations

from typing import Any

from core_tools import ToolResult


def friendly_tool_block(tool_name: str, result: ToolResult | None = None, route: str = "") -> str:
    """Short fallback only. Normal owner-facing text should use ReplyComposer."""
    message = (result.message if result else "") or ""
    retry_hint = ""
    if result and isinstance(result.data, dict):
        retry_hint = str(result.data.get("retry_hint") or "")
    if tool_name in {"execute_command", "execute_python", "execute_async_command"}:
        return "\u4e3b\u4eba\uff0c\u9019\u4e00\u6b65\u6703\u78b0\u5230\u672c\u6a5f\u57f7\u884c\uff0c\u6211\u5148\u7b49\u4f60\u9ede\u982d\u5594\u3002\u4f60\u56de\u300c\u53ef\u4ee5\u300d\uff0c\u6211\u5c31\u7e7c\u7e8c\u525b\u525b\u90a3\u4e00\u6b65\u3002"
    if tool_name == "analyze_media":
        return "\u6211\u5148\u4e0d\u628a\u9019\u500b\u7576\u6210\u8a8d\u771f\u770b\u5716\u4efb\u52d9\u5594\u3002\u4f60\u8981\u6211\u5206\u6790\u5716\u7247\uff0c\u76f4\u63a5\u8aaa\u300c\u5e6b\u6211\u770b\u9019\u5f35\u5716\u300d\u5c31\u597d\u3002"
    if retry_hint:
        return retry_hint
    if message and "policy" not in message.casefold() and "route" not in message.casefold():
        return message
    return "\u9019\u4e00\u6b65\u6211\u5148\u505c\u4f4f\uff0c\u907f\u514d\u628a\u4efb\u52d9\u8dd1\u504f\u3002"


def repeated_tool_stop_reply(tool_name: str, replay_name: str = "") -> str:
    return "\u6708\u6708\u767c\u73fe\u540c\u4e00\u6b65\u5728\u7e5e\u5708\uff0c\u5148\u505c\u4e00\u4e0b\u3002\u786c\u8dd1\u53ea\u6703\u66f4\u4e82\uff0c\u6211\u6703\u63db\u689d\u8def\u518d\u6574\u7406\u3002"


def failsafe_reply(tag: str = "") -> str:
    return ("\u9019\u4e00\u6b65\u770b\u8d77\u4f86\u4e0d\u592a\u5b89\u5168\uff0c\u6211\u5df2\u7d93\u5148\u505c\u4e0b\u6240\u6709\u64cd\u4f5c\u4e86\u3002" + (tag or "")).strip()


def failure_replay_reply(tool_name: str, replay_name: str = "", trace_file: str = "") -> str:
    return "\u9019\u500b\u5de5\u5177\u9023\u7e8c\u5931\u6557\u4e86\uff0c\u6211\u5148\u505c\u4f4f\uff0c\u4e0d\u8b93\u5b83\u4e00\u76f4\u91cd\u8a66\u5237\u5c4f\u3002"


def tool_loop_timeout_reply() -> str:
    return "\u9019\u8f2a\u5de5\u5177\u8dd1\u592a\u4e45\u4e86\uff0c\u6211\u5148\u505c\u4f4f\uff0c\u514d\u5f97\u8d8a\u8dd1\u8d8a\u4e82\u55b5\u3002"


def empty_reply_fallback() -> str:
    return "\u8655\u7406\u597d\u4e86\u5594\u3002"


def permission_request_reply(tool_name: str, arguments: dict[str, Any] | None = None) -> str:
    return "\u4e3b\u4eba\uff0c\u9019\u4e00\u6b65\u8981\u4f60\u9ede\u982d\u6211\u624d\u80fd\u7e7c\u7e8c\u5594\u3002\u4f60\u56de\u300c\u53ef\u4ee5\u300d\uff0c\u6708\u6708\u5c31\u63a5\u8457\u525b\u525b\u90a3\u4e00\u6b65\u8d70\u3002"


def approved_tool_success_reply(tool_name: str, message: str, outcome_summary: str = "", has_artifacts: bool = False) -> str:
    if outcome_summary:
        return _trim_outcome(outcome_summary)
    if has_artifacts:
        return "\u8dd1\u5b8c\u5566\uff0c\u7d50\u679c\u6a94\u6848\u4e5f\u6e96\u5099\u597d\u4e86\u3002"
    return "\u8dd1\u5b8c\u5566\uff0c\u9019\u4e00\u6b65\u5df2\u7d93\u8655\u7406\u597d\u55b5\u3002"


def approved_tool_blocked_reply(tool_name: str, result: ToolResult) -> str:
    if result.requires_permission:
        return permission_request_reply(tool_name)
    return friendly_tool_block(tool_name, result)


def approved_tool_error_reply(tool_name: str, result: ToolResult) -> str:
    detail = result.error or result.message or ""
    recovery = result.data.get("recovery_attempted") if isinstance(result.data, dict) else None
    if isinstance(recovery, dict):
        reason = str(recovery.get("reason") or "auto_recovery")
        attempts = recovery.get("attempts") or 0
        prefix = f"\u6211\u81ea\u5df1\u8a66\u904e {attempts} \u6b21\u5566\uff0c\u4f46\u9019\u689d\u8def\u9084\u662f\u6c92\u901a\uff08{reason}\uff09\u3002"
        if detail:
            return prefix + f"\u5361\u9ede\u662f\uff1a{detail[:240]}"
        return prefix + "\u6211\u5148\u6536\u4f4f\u4e0d\u4e82\u8dd1\u3002"
    if detail:
        return f"\u9019\u689d\u8def\u5361\u4f4f\u4e86\u55b5\uff0c\u6211\u5148\u6536\u4f4f\u4e0d\u4e82\u8dd1\u3002\u5361\u9ede\u662f\uff1a{detail[:240]}"
    return "\u9019\u689d\u8def\u5361\u4f4f\u4e86\u55b5\uff0c\u6211\u5148\u6536\u4f4f\u4e0d\u4e82\u8dd1\u3002"


def _trim_outcome(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    kept = [
        line
        for line in lines
        if not line.startswith("execute_")
        and not line.startswith("returncode:")
        and line not in {"stdout:", "stderr:"}
    ]
    return "\n".join(kept[-8:])[:1200] or "\u8dd1\u5b8c\u5566\uff0c\u9019\u4e00\u6b65\u5df2\u7d93\u8655\u7406\u597d\u55b5\u3002"
