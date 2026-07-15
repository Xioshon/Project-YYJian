from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from openai import OpenAI


@dataclass
class ProviderResponse:
    content: str
    reasoning: str
    tool_calls: list[dict[str, Any]]


class ProviderFailure(RuntimeError):
    def __init__(self, category: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.category = category
        self.retryable = retryable


def classify_provider_error(error: Exception) -> ProviderFailure:
    message = str(error or "Unknown provider error")
    lowered = message.casefold()
    if "30001" in lowered or "balance is insufficient" in lowered or "insufficient balance" in lowered:
        return ProviderFailure("insufficient_balance", message, retryable=False)
    if "401" in lowered or "authentication" in lowered or "invalid api key" in lowered:
        return ProviderFailure("authentication", message, retryable=False)
    if "429" in lowered or "rate limit" in lowered or "too many requests" in lowered:
        return ProviderFailure("rate_limit", message, retryable=True)
    if isinstance(error, (TimeoutError, httpx.TimeoutException)) or "timeout" in lowered:
        return ProviderFailure("timeout", message, retryable=True)
    if isinstance(error, httpx.NetworkError) or any(
        marker in lowered for marker in ("connection reset", "connection aborted", "network", "temporarily unavailable")
    ):
        return ProviderFailure("network", message, retryable=True)
    if any(code in lowered for code in ("500", "502", "503", "504")) or "server error" in lowered:
        return ProviderFailure("server_error", message, retryable=True)
    if any(code in lowered for code in ("400", "403", "404", "422")):
        return ProviderFailure("request_rejected", message, retryable=False)
    return ProviderFailure("unknown", message, retryable=False)


class SiliconFlowProvider:
    """Persistent OpenAI-compatible provider with bounded retries and circuit breaking."""

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-ai/DeepSeek-V4-Pro",
        base_url: str = "https://api.siliconflow.cn/v1",
        timeout_seconds: float = 75.0,
        max_retries: int = 2,
    ):
        self.model = model
        self.max_retries = max(1, min(int(max_retries), 3))
        self._http = httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=15.0))
        self._client = OpenAI(api_key=api_key, base_url=base_url, http_client=self._http)
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._event_sink: Callable[[dict[str, Any]], None] | None = None

    def set_event_sink(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        self._event_sink = sink

    def _notify(self, payload: dict[str, Any]) -> None:
        if not self._event_sink:
            return
        try:
            self._event_sink(dict(payload))
        except Exception:
            return

    def close(self) -> None:
        self._http.close()

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        model: str = "",
        tool_choice: str = "auto",
        reasoning_effort: str = "",
    ) -> ProviderResponse:
        if time.time() < self._circuit_open_until:
            raise RuntimeError("LLM provider circuit is temporarily open after repeated failures.")
        formatted_tools = []
        for tool in tools or []:
            formatted_tools.append(
                {
                    "type": "function",
                    "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
                }
            )
        kwargs: dict[str, Any] = {"model": model or self.model, "messages": messages}
        if formatted_tools:
            kwargs.update({"tools": formatted_tools, "tool_choice": tool_choice})
        # reasoning_effort is only sent for task/planning/verification calls (see runtime), never
        # for chat/persona turns - the chat voice model may not accept it. Live probe 2026-07-12:
        # SiliconFlow DeepSeek honors OpenAI-standard low/medium/high; "high" is the real ceiling
        # (an out-of-range value like "max" is accepted but silently ignored, not deeper).
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            started = time.perf_counter()
            try:
                response = self._client.chat.completions.create(**kwargs)
                self._consecutive_failures = 0
                message = response.choices[0].message
                calls = []
                for call in getattr(message, "tool_calls", None) or []:
                    try:
                        arguments = json.loads(call.function.arguments or "{}")
                    except (ValueError, TypeError):
                        arguments = {}
                    calls.append(
                        {
                            "id": str(call.id),
                            "name": str(call.function.name),
                            "arguments": arguments,
                            "raw_arguments": str(call.function.arguments or "{}"),
                        }
                    )
                result = ProviderResponse(
                    str(message.content or ""), str(getattr(message, "reasoning_content", "") or ""), calls
                )
                self._notify(
                    {
                        "status": "ok",
                        "model": kwargs["model"],
                        "attempt": attempt + 1,
                        "latency_ms": round((time.perf_counter() - started) * 1000),
                    }
                )
                return result
            except Exception as exc:
                failure = classify_provider_error(exc)
                last_error = failure
                if failure.retryable:
                    self._consecutive_failures += 1
                else:
                    self._consecutive_failures = 0
                self._notify(
                    {
                        "status": "error",
                        "model": kwargs["model"],
                        "attempt": attempt + 1,
                        "latency_ms": round((time.perf_counter() - started) * 1000),
                        "category": failure.category,
                        "retryable": failure.retryable,
                    }
                )
                if failure.retryable and attempt + 1 < self.max_retries:
                    time.sleep(min(0.75 * (attempt + 1), 2.0))
                    continue
                break
        if self._consecutive_failures >= 3:
            self._circuit_open_until = time.time() + 30.0
        if isinstance(last_error, ProviderFailure):
            raise last_error
        raise ProviderFailure("unknown", f"LLM request failed: {last_error}") from last_error


class ScriptedProvider:
    """Deterministic provider used by replay and integration tests."""

    def __init__(self, responses: list[ProviderResponse]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        model: str = "",
        tool_choice: str = "auto",
        reasoning_effort: str = "",
    ) -> ProviderResponse:
        self.calls.append(
            {
                "messages": messages,
                "tools": [item.name for item in tools or []],
                "model": model,
                "tool_choice": tool_choice,
                "reasoning_effort": reasoning_effort,
            }
        )
        if not self.responses:
            return ProviderResponse("", "", [])
        return self.responses.pop(0)

    def close(self) -> None:
        return None
