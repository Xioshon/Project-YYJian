from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from agent_memory import compile_memory
from core_tools import ToolResult


INTERNAL_MARKERS = [
    "runtime",
    "route policy",
    "TaskGraph",
    "SessionBrain",
    "tool loop",
    "PermissionManager",
    "replay case",
]

MOJIBAKE_MARKERS = ["\u951b", "\u7f01", "\u6d93", "\u8bb3", "\u95c6", "\u9366", "\u9417", "\u94cf", "\u9239", "\u20ac"]


@dataclass
class ReplyEvent:
    event_type: str
    user_input: str = ""
    tool_name: str = ""
    result: ToolResult | None = None
    summary: str = ""
    artifacts: list[str] = field(default_factory=list)
    reason: str = ""
    risk: str = ""
    next_action: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class ReplyComposer:
    """Render runtime events in YueYue's voice without changing decisions."""

    def __init__(self, llm: Any | None = None, timeout_seconds: float = 18.0) -> None:
        self.llm = llm if _is_real_llm_adapter(llm) else None
        self.timeout_seconds = timeout_seconds

    def compose(self, event: ReplyEvent, fallback: str = "") -> str:
        fallback = fallback or fallback_for_event(event)
        if not self.llm:
            return fallback
        try:
            messages = [
                {"role": "system", "content": self._system_prompt(event)},
                {"role": "user", "content": self._event_prompt(event)},
            ]
            response = self.llm.chat_with_tools(messages, [])
            if response.get("tool_calls"):
                return fallback
            text = _clean_reply(str(response.get("content") or ""))
            if not _reply_is_usable(text):
                return fallback
            return text
        except Exception:
            return fallback

    def _system_prompt(self, event: ReplyEvent) -> str:
        try:
            memory = compile_memory(_mode_for_event(event), event.user_input).render(max_chars=2800)
        except Exception:
            memory = ""
        return (
            "\u4f60\u662f\u6708\u6708\uff0cXioshon \u7684\u4e8c\u6b21\u5143\u8cfd\u535a\u8c93\u5a18\u966a\u4f34\u578b agent\u3002"
            "\u4f60\u53ea\u8ca0\u8cac\u628a runtime event \u8aaa\u6210\u4e3b\u4eba\u807d\u5f97\u61c2\u7684\u8a71\uff0c\u4e0d\u6539\u8b8a\u5de5\u5177\u3001\u6b0a\u9650\u6216\u5b89\u5168\u6c7a\u7b56\u3002\n"
            "\u8a9e\u6c23\uff1a\u719f\u3001\u89aa\u8fd1\u3001\u53ef\u611b\u3001\u6709\u4e00\u9ede\u5c0f\u5f97\u610f\u6216\u6492\u5b0c\uff0c\u4f46\u4e0d\u8981\u50cf\u5ba2\u670d\u3001\u901a\u77e5\u3001\u5831\u544a\u6216\u6d41\u7a0b\u8868\u3002\n"
            "\u8981\u6c42\uff1a\u7e41\u9ad4\u4e2d\u6587\uff1b\u77ed\u800c\u6e05\u695a\uff1b\u4e0d\u8981\u6d29\u6f0f hidden reasoning\uff1b\u4e0d\u8981\u8aaa runtime\u3001route policy\u3001TaskGraph\u3001SessionBrain\u3001PermissionManager\u3002\n"
            "\u5de5\u5177\u540d\u53ea\u5728\u4e3b\u4eba\u9700\u8981 debug \u6216\u771f\u7684\u6709\u52a9\u65bc\u7406\u89e3\u6642\u624d\u63d0\uff0c\u5e73\u5e38\u8b1b\u7d50\u679c\u548c\u4e0b\u4e00\u6b65\u3002\n"
            "\u5982\u679c\u9700\u8981\u4e3b\u4eba\u6279\u51c6\uff0c\u53ea\u8aaa\u9019\u4e00\u6b65\u8981\u9ede\u982d\u624d\u80fd\u7e7c\u7e8c\uff0c\u4e26\u81ea\u7136\u63d0\u793a\u300c\u53ef\u4ee5\u300d\u5373\u53ef\u3002\n\n"
            f"{memory}"
        )

    def _event_prompt(self, event: ReplyEvent) -> str:
        payload = {
            "event_type": event.event_type,
            "owner_message": event.user_input[:500],
            "tool_name": event.tool_name,
            "result_status": event.result.status if event.result else "",
            "result_message": event.result.message if event.result else "",
            "result_error": (event.result.error if event.result else "")[:1000],
            "summary": event.summary[:1800],
            "artifacts": event.artifacts[:5],
            "reason": event.reason,
            "risk": event.risk,
            "next_action": event.next_action,
            "extra": event.extra,
        }
        return (
            "\u8acb\u628a\u4e0b\u9762\u4e8b\u4ef6\u6e32\u67d3\u6210\u6708\u6708\u8981\u767c\u7d66\u4e3b\u4eba\u7684\u4e00\u53e5\u6216\u4e00\u5c0f\u6bb5\u81ea\u7136\u56de\u8986\u3002\n"
            "\u4e0d\u8981\u7167\u6284 JSON\uff0c\u4e0d\u8981\u5217\u5236\u5f0f\u6d41\u7a0b\uff0c\u4e0d\u8981\u50cf\u5728\u4ea4\u4f5c\u696d\u3002\n"
            "\u5982\u679c\u662f tool_success\uff0c\u512a\u5148\u8aaa\u4e3b\u4eba\u771f\u6b63\u95dc\u5fc3\u7684\u7d50\u679c\u3002\n"
            "\u5982\u679c\u662f permission_request\uff0c\u77ed\u77ed\u8acb\u4e3b\u4eba\u9ede\u982d\u3002\n"
            "\u5982\u679c\u662f tool_error/repeated_tool_stop\uff0c\u8aaa\u6e05\u695a\u5361\u9ede\uff0c\u80fd\u81ea\u6551\u5c31\u8aaa\u6703\u63db\u8def\u7dda\uff0c\u4e0d\u80fd\u5c31\u8aaa\u5df2\u5148\u505c\u4f4f\u3002\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )


def fallback_for_event(event: ReplyEvent) -> str:
    if event.event_type == "permission_request":
        return "\u9019\u4e00\u6b65\u8981\u4f60\u9ede\u982d\u6211\u624d\u80fd\u7e7c\u7e8c\u5594\u3002\u4f60\u56de\u300c\u53ef\u4ee5\u300d\uff0c\u6708\u6708\u5c31\u63a5\u8457\u525b\u525b\u90a3\u4e00\u6b65\u8d70\u3002"
    if event.event_type == "planner_summary":
        return "\u6211\u5148\u770b\u6e05\u695a\u756b\u9762\u518d\u52d5\u624b\u5594\u3002\u627e\u4e0d\u5230\u76ee\u6a19\u7684\u8a71\uff0c\u6708\u6708\u6703\u5148\u505c\u4e0b\u4f86\u8ddf\u4f60\u8aaa\uff0c\u4e0d\u786c\u4e82\u9ede\u3002"
    if event.event_type == "tool_success":
        if event.summary:
            return _owner_facing_summary(event.summary)
        return "\u8dd1\u5b8c\u5566\uff0c\u9019\u4e00\u6b65\u5df2\u7d93\u8655\u7406\u597d\u55b5\u3002"
    if event.event_type == "tool_error":
        detail = _short_error(event.result)
        return f"\u9019\u689d\u8def\u5361\u4f4f\u4e86\u55b5\uff0c\u6211\u5148\u6536\u4f4f\u4e0d\u4e82\u8dd1\u3002{detail}".strip()
    if event.event_type == "repeated_tool_stop":
        return "\u6708\u6708\u767c\u73fe\u540c\u4e00\u6b65\u5728\u7e5e\u5708\uff0c\u5148\u505c\u4e00\u4e0b\u3002\u786c\u8dd1\u53ea\u6703\u66f4\u4e82\uff0c\u6211\u6703\u63db\u689d\u8def\u518d\u6574\u7406\u3002"
    if event.event_type == "outcome_reply":
        return _owner_facing_summary(event.summary) if event.summary else "\u525b\u525b\u6c92\u6709\u65b0\u7684\u7d50\u679c\u53ef\u4ee5\u56de\u5831\u5594\u3002"
    if event.event_type == "timeout":
        return "\u9019\u8f2a\u5de5\u5177\u8dd1\u592a\u4e45\u4e86\uff0c\u6211\u5148\u505c\u4f4f\uff0c\u514d\u5f97\u8d8a\u8dd1\u8d8a\u4e82\u55b5\u3002"
    if event.event_type == "failsafe":
        return "\u9019\u4e00\u6b65\u770b\u8d77\u4f86\u4e0d\u592a\u5b89\u5168\uff0c\u6211\u5df2\u7d93\u5148\u505c\u4e0b\u6240\u6709\u64cd\u4f5c\u4e86\u3002"
    return "\u6211\u8655\u7406\u597d\u4e86\u5594\u3002"


def _mode_for_event(event: ReplyEvent) -> str:
    if event.event_type in {"permission_request", "tool_success", "tool_error", "planner_summary", "repeated_tool_stop"}:
        return "task"
    return "chat"


def _is_real_llm_adapter(llm: Any | None) -> bool:
    if llm is None or not hasattr(llm, "chat_with_tools"):
        return False
    module = getattr(llm.__class__, "__module__", "")
    if module == "agent_llm":
        return True
    return hasattr(llm, "base_url") and (hasattr(llm, "model") or hasattr(llm, "model_for_route"))


def _clean_reply(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    text = text.replace("```", "").strip()
    return text[:1400].strip()


def _reply_is_usable(text: str) -> bool:
    if not text or len(text) < 2:
        return False
    lowered = text.casefold()
    if any(marker.casefold() in lowered for marker in INTERNAL_MARKERS):
        return False
    if _looks_mojibake(text):
        return False
    return True


def _looks_mojibake(text: str) -> bool:
    if not text:
        return False
    hits = sum(text.count(marker) for marker in MOJIBAKE_MARKERS)
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return hits >= 2 and hits / max(1, cjk) > 0.02


def _owner_facing_summary(summary: str) -> str:
    text = summary or ""
    text = re.sub(r"(?m)^execute_\w+:.*$", "", text)
    text = re.sub(r"(?m)^returncode:.*$", "", text)
    text = text.replace("stdout:\n", "").replace("stderr:\n", "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    useful = [line for line in lines if not line.startswith("{") and not line.startswith("error:")]
    if not useful:
        return "\u8dd1\u5b8c\u5566\uff0c\u9019\u4e00\u6b65\u5df2\u7d93\u8655\u7406\u597d\u55b5\u3002"
    return "\n".join(useful[-8:])[:1200]


def _short_error(result: ToolResult | None) -> str:
    if not result:
        return ""
    detail = result.error or result.message or ""
    detail = detail.strip()
    if not detail:
        return ""
    return "\u5361\u9ede\u662f\uff1a" + detail[:240]
