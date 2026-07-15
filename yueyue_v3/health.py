from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .models import SCHEMA_VERSION, RuntimeState, runtime_state_from_dict
from .observations import ObservationService
from .replay import build_regression_corpus, run_replay_corpus
from .storage import AtomicJsonStore, JsonlEventStore
from .tools import ToolCatalogV3


def build_v3_health_report(root: str | Path) -> dict[str, Any]:
    root_path = Path(root)
    state_dir = root_path / "workspace" / "project_cache" / "v3"
    state = AtomicJsonStore(state_dir / "runtime_state.json", RuntimeState, runtime_state_from_dict).load()
    events = JsonlEventStore(state_dir / "runtime_events.jsonl").read(5000)
    tools = ToolCatalogV3(ObservationService(state_dir / "health_observations"))
    replay_results = run_replay_corpus(build_regression_corpus())
    event_counts = Counter(str(item.get("kind") or "unknown") for item in events)
    tool_results = [item for item in events if item.get("kind") == "tool.result"]
    tool_failures = [item for item in tool_results if str((item.get("payload") or {}).get("status")) == "error"]
    provider_calls = [item for item in events if item.get("kind") == "provider.call"]
    provider_successes = [
        item for item in provider_calls if str((item.get("payload") or {}).get("status")) == "ok"
    ]
    provider_errors = [
        item for item in provider_calls if str((item.get("payload") or {}).get("status")) == "error"
    ]
    latest_provider = (provider_calls[-1].get("payload") or {}) if provider_calls else {}
    blockers: list[str] = []
    if len(tools.names()) != 30:
        blockers.append("public_tool_count")
    if state.schema_version != SCHEMA_VERSION:
        blockers.append("state_schema")
    if any(not item.passed for item in replay_results):
        blockers.append("replay_failures")
    # A blocked workflow is normal, expected, resumable state - it's the "no fake success"
    # design working as intended (see ARCHITECTURE.md), not a structural health problem.
    # It must stay informational only: gating startup on it would mean any legitimately
    # blocked task (the owner hasn't said "繼續" yet) permanently prevents the bot from
    # restarting, including via the watchdog's own self-heal restart - the opposite of
    # what a health gate should do.
    return {
        "schema_version": SCHEMA_VERSION,
        "state_schema_version": state.schema_version,
        "public_tool_count": len(tools.names()),
        "event_count": len(events),
        "event_counts": dict(sorted(event_counts.items())),
        "tool_results": len(tool_results),
        "tool_failures": len(tool_failures),
        "permission_replayed": event_counts.get("permission.replayed", 0),
        "workflow_completed": event_counts.get("workflow.completed", 0),
        "workflow_blocked": event_counts.get("workflow.blocked", 0),
        "active_workflow_status": state.workflow.status.value if state.workflow else "idle",
        "provider": {
            "last_status": str(latest_provider.get("status") or "unknown"),
            "last_category": str(latest_provider.get("category") or ""),
            "last_model": str(latest_provider.get("model") or ""),
            "last_latency_ms": int(latest_provider.get("latency_ms") or 0),
            "success_count": len(provider_successes),
            "error_count": len(provider_errors),
        },
        "replay": {
            "passed": sum(item.passed for item in replay_results),
            "failed": sum(not item.passed for item in replay_results),
            "results": [item.__dict__ for item in replay_results],
        },
        "blockers": blockers,
        "gate": "pass" if not blockers else "fail",
    }


def write_v3_health_report(root: str | Path, report: dict[str, Any]) -> Path:
    path = Path(root) / "workspace" / "project_cache" / "v3" / "health_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="YueYue Runtime v3 health gate")
    parser.add_argument("--root", default=str(Path(__file__).parents[1]))
    args = parser.parse_args()
    report = build_v3_health_report(args.root)
    path = write_v3_health_report(args.root, report)
    print("YueYue Runtime v3 Health")
    print(f"Public tools: {report['public_tool_count']}")
    print(f"Replay: {report['replay']['passed']} passed, {report['replay']['failed']} failed")
    print(f"Events: {report['event_count']}; tool failures: {report['tool_failures']}")
    print(f"Gate: {report['gate']}")
    print(f"Report: {path}")
    return 0 if report["gate"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
