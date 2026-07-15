from __future__ import annotations

from yueyue_v3.health import build_v3_health_report
from yueyue_v3.models import (
    SCHEMA_VERSION,
    GoalContract,
    RuntimeState,
    StepContract,
    WorkflowStatus,
    runtime_state_from_dict,
)
from yueyue_v3.storage import AtomicJsonStore, JsonlEventStore
from yueyue_v3.workflow import WorkflowEngine


def test_v3_health_report_passes_on_clean_empty_runtime(runtime_root) -> None:
    report = build_v3_health_report(runtime_root)
    assert report["schema_version"] == SCHEMA_VERSION
    assert report["public_tool_count"] == 30
    assert report["replay"]["failed"] == 0
    assert report["gate"] == "pass"


def test_health_uses_latest_provider_success_instead_of_stale_balance_error(runtime_root) -> None:
    events = JsonlEventStore(runtime_root / "workspace" / "project_cache" / "v3" / "runtime_events.jsonl")
    events.append(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "provider.call",
            "created_at": 1.0,
            "payload": {
                "status": "error",
                "category": "insufficient_balance",
                "model": "deepseek-ai/DeepSeek-V4-Pro",
                "latency_ms": 50,
            },
        }
    )
    events.append(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "provider.call",
            "created_at": 2.0,
            "payload": {
                "status": "ok",
                "model": "deepseek-ai/DeepSeek-V4-Pro",
                "latency_ms": 120,
            },
        }
    )

    report = build_v3_health_report(runtime_root)

    assert report["provider"]["last_status"] == "ok"
    assert report["provider"]["success_count"] == 1
    assert report["provider"]["error_count"] == 1
    assert report["provider"]["last_category"] == ""


def test_blocked_workflow_is_reported_but_does_not_fail_the_health_gate(runtime_root) -> None:
    # A blocked workflow is the "no fake success" design working as intended (an
    # unresolved task the owner can resume with "繼續") - it must stay informational,
    # not a startup-blocking gate failure. Gating on it would mean any legitimately
    # blocked task permanently prevents the bot from restarting, including via the
    # watchdog's own self-heal restart.
    state_dir = runtime_root / "workspace" / "project_cache" / "v3"
    workflow = WorkflowEngine().create(
        GoalContract("test blocked workflow", [], ["done"], "low"),
        [StepContract("step_1", "test", "observe", "done", ["list_windows"])],
    )
    workflow.status = WorkflowStatus.BLOCKED
    state = RuntimeState(workflow=workflow)
    AtomicJsonStore(state_dir / "runtime_state.json", RuntimeState, runtime_state_from_dict).save(state)

    report = build_v3_health_report(runtime_root)

    assert report["gate"] == "pass"
    assert "active_workflow_blocked" not in report["blockers"]
    assert report["active_workflow_status"] == "blocked"
