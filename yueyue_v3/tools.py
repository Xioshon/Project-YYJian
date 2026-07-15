from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import core_tools
from core_tools import AgentTool

from .models import V3ToolResult
from .observations import ObservationService


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    requires_confirm: bool
    handler: Callable[..., V3ToolResult]

    def provider_tool(self) -> AgentTool:
        return AgentTool(self.name, self.description, self.handler, self.parameters, self.requires_confirm)


class ToolCatalogV3:
    """Typed compatibility catalog for all public v2 tool names."""

    # A universal, side-effect-free "submit your derived answer" capability. Tasks whose answer
    # is COMPUTED from observations (a count, sum, extracted field) rather than returned directly
    # by a tool had no way to complete: the model would re-run the same observation, hit the
    # no-progress guard, and block (observed 2026-07-13 on a "count Python files" task). This lets
    # the model record the value as a named fact, which the output extractor then finds.
    REPORT_RESULT = "report_result"

    def __init__(self, observations: ObservationService):
        self.observations = observations
        self._tools: dict[str, ToolSpec] = {}
        self._register_legacy_tools()
        self._override_observation_tools()
        self._register_report_result()

    def _register_report_result(self) -> None:
        self._tools[self.REPORT_RESULT] = ToolSpec(
            self.REPORT_RESULT,
            "Submit a value you DERIVED from what you already observed (a count, a sum, an "
            "extracted field) to complete the task. Only use values grounded in prior tool "
            "evidence - never invent one. Each result name should match a requested output.",
            {
                "type": "object",
                "properties": {
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                            "required": ["name", "value"],
                        },
                    }
                },
                "required": ["results"],
            },
            False,
            self._report_result,
        )

    @staticmethod
    def _report_result(results: Any = None, **_: Any) -> V3ToolResult:
        facts: dict[str, Any] = {}
        for item in results if isinstance(results, list) else []:
            if isinstance(item, dict) and str(item.get("name") or "").strip():
                facts[str(item["name"]).strip()] = item.get("value")
        if not facts:
            return V3ToolResult(
                "error",
                "report_result needs a non-empty results list of {name, value}.",
                error_category="invalid_arguments",
            )
        summary = "; ".join(f"{key}={value}" for key, value in facts.items())
        return V3ToolResult("ok", f"Recorded derived result: {summary}", facts=facts)

    def names(self) -> list[str]:
        # report_result is an internal workflow-completion mechanism injected into a step's
        # allowed tools at execution time, not a user-facing capability - it is deliberately
        # excluded from the public catalog count (health/audit invariants stay at the real
        # public tool total) but stays executable and listable when explicitly allowed.
        return sorted(name for name in self._tools if name != self.REPORT_RESULT)

    def list(self, allowed: list[str] | None = None) -> list[AgentTool]:
        names = self.names() if allowed is None else [name for name in allowed if name in self._tools]
        return [self._tools[name].provider_tool() for name in names]

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def execute(self, name: str, arguments: dict[str, Any], callback: Callable | None = None) -> V3ToolResult:
        spec = self.get(name)
        if not spec:
            return V3ToolResult("error", f"Unknown tool: {name}.", error_category="unknown_tool")
        arguments = arguments if isinstance(arguments, dict) else {}
        if callback:
            _notify(callback, name, arguments, "start", None)
        try:
            result = spec.handler(**arguments)
            if not isinstance(result, V3ToolResult):
                result = _legacy_result(result)
        except TypeError as exc:
            result = V3ToolResult(
                "error", "Tool arguments are invalid.", error=str(exc), error_category="invalid_arguments"
            )
        except Exception as exc:
            result = V3ToolResult(
                "error", "Tool raised an exception.", error=str(exc), error_category="tool_exception", retryable=False
            )
        if callback:
            _notify(callback, name, arguments, "end", result)
        return result

    def _register_legacy_tools(self) -> None:
        for tool in core_tools.ALL_TOOLS:
            self._tools[tool.name] = ToolSpec(
                tool.name,
                tool.description,
                dict(tool.parameters),
                bool(tool.requires_confirm),
                _wrap_legacy(tool.func),
            )

    def _override_observation_tools(self) -> None:
        self._replace("get_screen_ui", self.observations.inspect_ui, {"type": "object", "properties": {}})
        self._replace("capture_screen", self.observations.capture_screen, {"type": "object", "properties": {}})
        self._replace(
            "list_windows",
            self.observations.list_windows,
            {"type": "object", "properties": {"max_results": {"type": "integer", "minimum": 1, "maximum": 100}}},
        )
        self._replace(
            "focus_window",
            self.observations.focus_window,
            {"type": "object", "properties": {"window_id": {"type": "string"}}, "required": ["window_id"]},
        )
        self._replace(
            "click_ui_element",
            self.observations.click_element,
            {
                "type": "object",
                "properties": {
                    "element_id": {"type": "string"},
                    "snapshot_id": {"type": "string"},
                    "double_click": {"type": "boolean"},
                },
                "required": ["element_id"],
            },
        )
        self._replace(
            "click_screen",
            self.observations.click_screen,
            {
                "type": "object",
                "properties": {
                    "screenshot_id": {"type": "string"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "double_click": {"type": "boolean"},
                },
                "required": ["screenshot_id", "x", "y"],
            },
        )

    def _replace(self, name: str, handler: Callable, parameters: dict[str, Any]) -> None:
        old = self._tools.get(name)
        description = old.description if old else name
        requires_confirm = old.requires_confirm if old else name in {"focus_window", "click_ui_element", "click_screen"}
        self._tools[name] = ToolSpec(name, description, parameters, requires_confirm, handler)


def _wrap_legacy(handler: Callable) -> Callable[..., V3ToolResult]:
    def invoke(**kwargs: Any) -> V3ToolResult:
        return _legacy_result(handler(**kwargs))

    return invoke


def _legacy_result(raw: Any) -> V3ToolResult:
    if isinstance(raw, V3ToolResult):
        return raw
    if isinstance(raw, core_tools.ToolResult):
        data = raw.data
        facts = dict(data) if isinstance(data, dict) else ({"text": data} if isinstance(data, str) else {})
        artifacts = _artifact_paths(data)
        category, retryable = _classify_error(raw.error or raw.message)
        return V3ToolResult(
            raw.status,
            raw.message,
            data=data,
            facts=facts,
            artifacts=artifacts,
            error=raw.error,
            error_category=category if raw.status == "error" else "",
            retryable=retryable if raw.status == "error" else False,
            requires_permission=raw.requires_permission,
        )
    return V3ToolResult("ok", str(raw))


def _artifact_paths(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    values = []
    for key in ("path", "file", "file_path", "screenshot_path", "thumbnail_path"):
        value = data.get(key)
        if isinstance(value, str) and (os.path.isabs(value) or os.path.exists(value)):
            values.append(value)
    return list(dict.fromkeys(values))[:8]


def _classify_error(text: str) -> tuple[str, bool]:
    value = str(text or "").casefold()
    if any(
        marker in value
        for marker in (
            "timeout",
            "timed out",
            "10054",
            "connection reset",
            "temporarily unavailable",
            "502",
            "503",
            "504",
        )
    ):
        return "transient", True
    if "not found" in value or "no such file" in value:
        return "not_found", False
    if "permission" in value or "access denied" in value:
        return "permission_denied", False
    if re.search(r"missing .*depend|no module named", value):
        return "dependency_missing", False
    return "tool_error", False


def _notify(callback: Callable, name: str, arguments: dict[str, Any], state: str, result: V3ToolResult | None) -> None:
    try:
        callback(name, arguments, state, result)
    except TypeError:
        try:
            callback(name, arguments, state)
        except Exception:
            return
    except Exception:
        return
