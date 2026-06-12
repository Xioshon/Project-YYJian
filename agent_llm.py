from __future__ import annotations

import json
import os
import time
from typing import Any, Protocol

import httpx
from openai import OpenAI

from core_tools import API_KEY, AgentTool


DEFAULT_STRONG_MODEL = "deepseek-ai/DeepSeek-V4-Pro"
DEFAULT_CHAT_MODEL = DEFAULT_STRONG_MODEL
DEFAULT_VISION_MODEL = DEFAULT_STRONG_MODEL


class LLMAdapter(Protocol):
    def chat_with_tools(self, messages: list[dict[str, Any]], tools: list[AgentTool]) -> dict[str, Any]:
        ...


def format_tools_for_openai(tools: list[AgentTool]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters}}
        for tool in tools
    ]


def format_messages_for_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    formatted_messages = []
    for message in messages:
        item = {"role": message["role"], "content": message.get("content", "")}
        for key in ("name", "tool_calls", "tool_call_id"):
            if key in message:
                item[key] = message[key]
        formatted_messages.append(item)
    return formatted_messages


def add_runtime_guardrail(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    guardrail = (
        "Reply naturally in Traditional Chinese unless the user asks otherwise. "
        "Do not expose hidden reasoning. Use tools only when useful. "
        "Sticker replies may include [表情包: filename] or [sticker: filename] when emotionally appropriate."
    )
    formatted = [dict(item) for item in messages]
    if formatted and formatted[0]["role"] == "system":
        formatted[0]["content"] = formatted[0].get("content", "") + "\n\n" + guardrail
    else:
        formatted.insert(0, {"role": "system", "content": guardrail})
    return formatted


class SiliconFlowAdapter:
    def __init__(
        self,
        model: str = "deepseek-ai/DeepSeek-V4-Pro",
        thinking_level: str = "auto",
        *,
        api_key: str | None = None,
        base_url: str = "https://api.siliconflow.cn/v1",
        timeout: float = 60.0,
        max_retries: int = 2,
    ):
        self.model = model
        self.thinking_level = thinking_level
        self.api_key = api_key if api_key is not None else API_KEY
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max(1, min(int(max_retries), 4))

    def chat_with_tools(self, messages: list[dict[str, Any]], tools: list[AgentTool]) -> dict[str, Any]:
        if not self.api_key or len(self.api_key) < 10:
            raise ValueError("SILICONFLOW_API_KEY is not configured.")

        openai_tools = format_tools_for_openai(tools)
        formatted_messages = add_runtime_guardrail(format_messages_for_openai(messages))
        kwargs: dict[str, Any] = {"model": self.model, "messages": formatted_messages}
        if openai_tools:
            kwargs["tools"] = openai_tools
            kwargs["tool_choice"] = "auto"

        last_error = ""
        for attempt in range(self.max_retries):
            try:
                client = OpenAI(api_key=self.api_key, base_url=self.base_url, http_client=httpx.Client(timeout=self.timeout))
                response = client.chat.completions.create(**kwargs)
                return self._parse_response(response)
            except Exception as exc:
                last_error = str(exc)
                if attempt + 1 < self.max_retries:
                    time.sleep(min(0.6 * (attempt + 1), 1.5))
        return {"role": "assistant", "content": f"[LLM API error] {last_error}", "reasoning": ""}

    def _parse_response(self, response: Any) -> dict[str, Any]:
        choice = response.choices[0]
        message = choice.message
        result: dict[str, Any] = {
            "role": "assistant",
            "content": message.content or "",
            "reasoning": getattr(message, "reasoning_content", "") or "",
        }
        if getattr(message, "tool_calls", None):
            result["tool_calls"] = []
            for tool_call in message.tool_calls:
                try:
                    args = json.loads(tool_call.function.arguments or "{}")
                except Exception:
                    args = {}
                result["tool_calls"].append(
                    {
                        "id": tool_call.id,
                        "name": tool_call.function.name,
                        "arguments": args,
                        "raw_arguments": tool_call.function.arguments or "{}",
                    }
                )
        return result


def _env_model(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


class RoutedLLMAdapter:
    """Route lightweight chat to a fast model while keeping tasks on a stronger model."""

    def __init__(
        self,
        *,
        chat_model: str | None = None,
        task_model: str | None = None,
        vision_model: str | None = None,
        thinking_level: str = "auto",
        api_key: str | None = None,
        base_url: str = "https://api.siliconflow.cn/v1",
    ):
        self.chat_model = chat_model or _env_model("YUEYUE_CHAT_MODEL", DEFAULT_CHAT_MODEL)
        self.task_model = task_model or _env_model("YUEYUE_TASK_MODEL", _env_model("YUEYUE_STRONG_MODEL", DEFAULT_STRONG_MODEL))
        self.vision_model = vision_model or _env_model("YUEYUE_VISION_MODEL", self.task_model)
        self.thinking_level = thinking_level
        self.api_key = api_key
        self.base_url = base_url
        self._adapters: dict[str, SiliconFlowAdapter] = {}
        self.last_route = ""
        self.last_model = ""

    def chat_with_tools(self, messages: list[dict[str, Any]], tools: list[AgentTool]) -> dict[str, Any]:
        route = infer_route_from_messages(messages)
        model = self.model_for_route(route)
        self.last_route = route
        self.last_model = model
        return self._adapter_for(model).chat_with_tools(messages, tools)

    def model_for_route(self, route: str) -> str:
        normalized = (route or "").casefold()
        if normalized in {"chat", "social_sticker", "idle"}:
            return self.chat_model
        if normalized in {"vision_task", "vision", "screen_observe", "screen"}:
            return self.vision_model
        return self.task_model

    def _adapter_for(self, model: str) -> SiliconFlowAdapter:
        if model not in self._adapters:
            self._adapters[model] = SiliconFlowAdapter(
                model=model,
                thinking_level=self.thinking_level,
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._adapters[model]


def infer_route_from_messages(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages or []):
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "")
        marker = "turn_intent:"
        if marker in content:
            tail = content.rsplit(marker, 1)[-1].strip().splitlines()[0].strip()
            return tail or "chat"
        if "[SessionBrain]" not in content and "[TaskGraph]" not in content:
            return "chat"
    return "task"
