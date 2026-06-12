import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from agent_hooks import TRACE_LOG_FILE


@dataclass
class TraceSummary:
    total_events: int = 0
    event_counts: dict[str, int] = field(default_factory=dict)
    tool_calls: dict[str, int] = field(default_factory=dict)
    tool_errors: dict[str, int] = field(default_factory=dict)
    interaction_modes: dict[str, int] = field(default_factory=dict)
    social_events: dict[str, int] = field(default_factory=dict)
    permission_replay: dict[str, int] = field(default_factory=dict)
    permission_bundles: dict[str, int] = field(default_factory=dict)
    action_verification: dict[str, int] = field(default_factory=dict)
    failure_replays: int = 0
    latency_buckets: dict[str, int] = field(default_factory=dict)
    recent_errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tool_success_rate(self) -> float:
        total = sum(self.tool_calls.values())
        if total <= 0:
            return 1.0
        errors = sum(self.tool_errors.values())
        return max(0.0, min(1.0, (total - errors) / total))

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_events": self.total_events,
            "event_counts": self.event_counts,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "tool_success_rate": round(self.tool_success_rate, 4),
            "interaction_modes": self.interaction_modes,
            "social_events": self.social_events,
            "permission_replay": self.permission_replay,
            "permission_bundles": self.permission_bundles,
            "action_verification": self.action_verification,
            "failure_replays": self.failure_replays,
            "latency_buckets": self.latency_buckets,
            "recent_errors": self.recent_errors,
        }

    def to_text(self) -> str:
        lines = [
            f"Trace events: {self.total_events}",
            f"Tool success rate: {self.tool_success_rate:.1%}",
        ]
        if self.tool_calls:
            lines.append("Tool calls: " + ", ".join(f"{name}={count}" for name, count in sorted(self.tool_calls.items())))
        if self.tool_errors:
            lines.append("Tool errors: " + ", ".join(f"{name}={count}" for name, count in sorted(self.tool_errors.items())))
        if self.interaction_modes:
            lines.append("Interaction modes: " + ", ".join(f"{name}={count}" for name, count in sorted(self.interaction_modes.items())))
        if self.social_events:
            lines.append("Social events: " + ", ".join(f"{name}={count}" for name, count in sorted(self.social_events.items())))
        if self.permission_replay:
            lines.append("Permission replay: " + ", ".join(f"{name}={count}" for name, count in sorted(self.permission_replay.items())))
        if self.permission_bundles:
            lines.append("Permission bundles: " + ", ".join(f"{name}={count}" for name, count in sorted(self.permission_bundles.items())))
        if self.action_verification:
            lines.append("Action verification: " + ", ".join(f"{name}={count}" for name, count in sorted(self.action_verification.items())))
        if self.failure_replays:
            lines.append(f"Failure replay cases: {self.failure_replays}")
        if self.latency_buckets:
            lines.append("Latency buckets: " + ", ".join(f"{name}={count}" for name, count in sorted(self.latency_buckets.items())))
        if self.recent_errors:
            lines.append("Recent errors:")
            for item in self.recent_errors[-5:]:
                lines.append(f"- {item.get('event', 'error')}: {item.get('tool', '')} {item.get('error') or item.get('result') or ''}".strip())
        return "\n".join(lines)


def load_trace_events(path: str = TRACE_LOG_FILE, limit: int | None = None) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    events: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as file:
        lines = file.readlines()
    selected = lines[-limit:] if limit else lines
    for line in selected:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"event": "trace.decode_error", "raw": line[:300]})
    return events


def summarize_trace(path: str = TRACE_LOG_FILE, limit: int | None = 1000) -> TraceSummary:
    events = load_trace_events(path, limit=limit)
    event_counts: Counter[str] = Counter()
    tool_calls: Counter[str] = Counter()
    tool_errors: Counter[str] = Counter()
    interaction_modes: Counter[str] = Counter()
    social_events: Counter[str] = Counter()
    permission_replay: Counter[str] = Counter()
    permission_bundles: Counter[str] = Counter()
    action_verification: Counter[str] = Counter()
    latency_buckets: Counter[str] = Counter()
    failure_replays = 0
    recent_errors: list[dict[str, Any]] = []
    tool_status_by_turn: dict[tuple[str, int, str], str] = defaultdict(str)

    for event in events:
        name = str(event.get("event") or "unknown")
        event_counts[name] += 1
        if name.startswith("social_"):
            social_events[name] += 1
        if name == "turn.flush" and event.get("mode"):
            interaction_modes[str(event.get("mode"))] += 1
            duration_ms = event.get("duration_ms") or event.get("latency_ms")
            bucket = _latency_bucket(duration_ms)
            if bucket:
                latency_buckets[bucket] += 1
        if name == "PermissionReplayResult":
            permission_replay[str(event.get("status") or "unknown")] += 1
        if name in {"PermissionBundleGranted", "PermissionBundleConsumed", "PermissionBundleDenied"}:
            permission_bundles[name.removeprefix("PermissionBundle")] += 1
        if name == "ActionVerification":
            action_verification[str(event.get("status") or "unknown")] += 1
        if name == "FailureReplayCreated":
            failure_replays += 1
        if name == "PostToolUse":
            tool = str(event.get("tool") or "unknown")
            tool_calls[tool] += 1
            key = (str(event.get("session_id") or ""), int(event.get("turn_id") or 0), tool)
            tool_status_by_turn[key] = str(event.get("status") or "")
            if event.get("status") == "error":
                tool_errors[tool] += 1
                recent_errors.append(_error_snapshot(event))
        elif name == "ToolError":
            tool = str(event.get("tool") or "unknown")
            key = (str(event.get("session_id") or ""), int(event.get("turn_id") or 0), tool)
            if tool_status_by_turn.get(key) != "error":
                tool_errors[tool] += 1
            recent_errors.append(_error_snapshot(event))
        elif name.endswith("failed") or name.endswith("load_failed") or name.endswith("save_failed") or name == "HookError" or name == "trace.decode_error":
            recent_errors.append(_error_snapshot(event))

    return TraceSummary(
        total_events=len(events),
        event_counts=dict(sorted(event_counts.items())),
        tool_calls=dict(sorted(tool_calls.items())),
        tool_errors=dict(sorted(tool_errors.items())),
        interaction_modes=dict(sorted(interaction_modes.items())),
        social_events=dict(sorted(social_events.items())),
        permission_replay=dict(sorted(permission_replay.items())),
        permission_bundles=dict(sorted(permission_bundles.items())),
        action_verification=dict(sorted(action_verification.items())),
        failure_replays=failure_replays,
        latency_buckets=dict(sorted(latency_buckets.items())),
        recent_errors=recent_errors[-10:],
    )


def _error_snapshot(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": event.get("ts", ""),
        "event": event.get("event", ""),
        "tool": event.get("tool", ""),
        "error": event.get("error", ""),
        "result": str(event.get("result", ""))[:300],
    }


def _latency_bucket(value: Any) -> str:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return ""
    if duration < 1000:
        return "<1s"
    if duration < 3000:
        return "1-3s"
    if duration < 6000:
        return "3-6s"
    return ">=6s"


if __name__ == "__main__":
    print(summarize_trace().to_text())
