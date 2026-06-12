import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from core_tools import PROJECT_CACHE_DIR


TRACE_LOG_FILE = os.path.join(PROJECT_CACHE_DIR, "agent_trace.jsonl")


@dataclass
class HookEvent:
    name: str
    session_id: str
    turn_id: int
    tool_name: str = ""
    arguments: dict = field(default_factory=dict)
    result: Any = None
    context: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))

    def to_dict(self) -> dict:
        payload = {
            "ts": self.timestamp,
            "event": self.name,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
        }
        if self.tool_name:
            payload["tool"] = self.tool_name
        if self.arguments:
            payload["arguments"] = self.arguments
        if self.result is not None:
            payload["result"] = self.result
        if self.context:
            payload.update(self.context)
        return payload


@dataclass
class HookDecision:
    allow: bool = True
    block: bool = False
    annotate: str = ""
    replace_args: dict | None = None
    append_context: str = ""
    message: str = ""

    @classmethod
    def allow_decision(cls) -> "HookDecision":
        return cls(allow=True)

    @classmethod
    def block_decision(cls, message: str) -> "HookDecision":
        return cls(allow=False, block=True, message=message)


class HookManager:
    def __init__(self, trace_file: str = TRACE_LOG_FILE):
        self.trace_file = trace_file
        self._hooks: dict[str, list[Callable[[HookEvent], HookDecision | None]]] = {}
        self.events: list[HookEvent] = []

    def register(self, event_name: str, handler: Callable[[HookEvent], HookDecision | None]) -> None:
        self._hooks.setdefault(event_name, []).append(handler)

    def clear(self) -> None:
        self._hooks.clear()

    def emit(self, event_name: str, session_id: str = "", turn_id: int = 0, **data: Any) -> HookDecision:
        event = HookEvent(
            name=event_name,
            session_id=session_id,
            turn_id=turn_id,
            tool_name=data.pop("tool", data.pop("tool_name", "")),
            arguments=data.pop("arguments", {}) or {},
            result=data.pop("result", None),
            context=data,
        )
        self.events.append(event)
        self._write_trace(event)

        merged = HookDecision.allow_decision()
        for handler in self._hooks.get(event_name, []):
            try:
                decision = handler(event)
            except Exception as exc:
                self._write_trace(
                    HookEvent(
                        name="HookError",
                        session_id=session_id,
                        turn_id=turn_id,
                        context={"hook_event": event_name, "error": str(exc)},
                    )
                )
                continue
            if not decision:
                continue
            if decision.block:
                merged.block = True
                merged.allow = False
                merged.message = decision.message or merged.message
            if decision.annotate:
                merged.annotate += decision.annotate
            if decision.replace_args is not None:
                merged.replace_args = decision.replace_args
            if decision.append_context:
                merged.append_context += decision.append_context
        return merged

    def _write_trace(self, event: HookEvent) -> None:
        try:
            os.makedirs(os.path.dirname(self.trace_file), exist_ok=True)
            with open(self.trace_file, "a", encoding="utf-8") as file:
                file.write(json.dumps(event.to_dict(), ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass


DEFAULT_HOOK_MANAGER = HookManager()


def emit_trace(event: str, **data: Any) -> None:
    DEFAULT_HOOK_MANAGER.emit(event, **data)
