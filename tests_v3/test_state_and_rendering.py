from __future__ import annotations

import json
import threading

from yueyue_v3.events import SingleWriterEventLoop
from yueyue_v3.models import SCHEMA_VERSION, RuntimeEvent, RuntimeState
from yueyue_v3.rendering import RenderLedger
from yueyue_v3.storage import AtomicJsonStore, JsonlEventStore


def test_atomic_store_recovers_last_known_good_after_primary_corruption(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = AtomicJsonStore(path, dict, lambda raw: dict(raw))
    store.save({"version": 1})
    store.save({"version": 2})
    path.write_text("{broken", encoding="utf-8")
    assert store.load() == {"version": 1}


def test_background_event_is_queued_until_main_writer_drains(tmp_path) -> None:
    state = RuntimeState()

    def reducer(current, event):
        current.render_keys.append(event.kind)
        return current

    loop = SingleWriterEventLoop(state, reducer, JsonlEventStore(tmp_path / "events.jsonl"))
    event = RuntimeEvent("worker.finished", state.session_id)
    thread = threading.Thread(target=lambda: loop.apply(event))
    thread.start()
    thread.join()

    assert state.render_keys == []
    loop.drain()
    assert state.render_keys == ["worker.finished"]


def test_external_evidence_event_cannot_replace_runtime_state(runtime_root) -> None:
    from yueyue_v3.providers import ScriptedProvider
    from yueyue_v3.runtime import YueYueRuntimeV3

    runtime = YueYueRuntimeV3(runtime_root, ScriptedProvider([]))
    original_session = runtime.state.session_id
    runtime.submit_external_event(
        "worker.finished",
        {"state": {"session_id": "forged", "schema_version": 3}, "evidence": {"status": "ok"}},
    )
    runtime.events.drain()
    assert runtime.state.session_id == original_session


def test_worker_evidence_is_assimilated_only_by_main_event_loop(runtime_root) -> None:
    import copy

    from yueyue_v3.models import GoalContract, RequestedOutput, StepContract
    from yueyue_v3.providers import ScriptedProvider
    from yueyue_v3.runtime import YueYueRuntimeV3

    runtime = YueYueRuntimeV3(runtime_root, ScriptedProvider([]))
    workflow = runtime.workflow_engine.create(
        GoalContract("驗證檔案", [RequestedOutput("verified", "驗證狀態", True, "status", ["passed"])], ["passed"]),
        [StepContract("verify", "執行驗證", "verify", "取得 passed", ["execute_command"], ["passed"])],
    )
    runtime.workflow_engine.approve(workflow)
    state = copy.deepcopy(runtime.state)
    state.workflow = workflow
    runtime._replace_state(state, "workflow.started", "turn-1")

    runtime.submit_external_event(
        "worker.evidence",
        {
            "workflow_id": workflow.workflow_id,
            "step_id": "verify",
            "source": "verifier_worker",
            "status": "ok",
            "summary": "passed",
            "facts": {"verified": "passed"},
        },
    )
    assert runtime.state.workflow.evidence == []

    runtime.events.drain()

    assert len(runtime.state.workflow.evidence) == 1
    assert runtime.state.workflow.evidence[0].source == "verifier_worker"


def test_render_ledger_claim_release_and_restart_are_idempotent(tmp_path) -> None:
    path = tmp_path / "render_ledger.json"
    ledger = RenderLedger(path)
    key = ledger.key("event-1", "text", "hello")
    assert ledger.claim(key) is True
    assert ledger.claim(key) is False

    reloaded = RenderLedger(path)
    assert reloaded.claim(key) is False
    reloaded.release(key)
    assert reloaded.claim(key) is True
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 3


def test_v3_context_and_event_files_include_schema_version(runtime_root) -> None:
    from yueyue_v3.models import TurnEnvelope, TurnMode
    from yueyue_v3.providers import ProviderResponse, ScriptedProvider
    from yueyue_v3.runtime import YueYueRuntimeV3

    runtime = YueYueRuntimeV3(runtime_root, ScriptedProvider([ProviderResponse("在呢。", "", [])]))
    runtime.process_turn(TurnEnvelope("owner", "嗨", TurnMode.CHAT))
    state_dir = runtime_root / "workspace" / "project_cache" / "v3"

    context = json.loads((state_dir / "short_context.json").read_text(encoding="utf-8"))
    first_event = json.loads((state_dir / "runtime_events.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert context["schema_version"] == 3
    assert first_event["schema_version"] == SCHEMA_VERSION


def test_persisted_transition_event_omits_full_runtime_state(runtime_root) -> None:
    import copy

    from yueyue_v3.models import GoalContract, RequestedOutput, StepContract
    from yueyue_v3.providers import ScriptedProvider
    from yueyue_v3.runtime import YueYueRuntimeV3

    runtime = YueYueRuntimeV3(runtime_root, ScriptedProvider([]))
    workflow = runtime.workflow_engine.create(
        GoalContract("Inspect", [RequestedOutput("result", "result")], []),
        [StepContract("observe", "Observe", "observe", "Observed", ["capture_screen"])],
    )
    state = copy.deepcopy(runtime.state)
    state.workflow = workflow
    runtime._replace_state(state, "workflow.started", "turn-compact")

    event = json.loads(
        (runtime_root / "workspace" / "project_cache" / "v3" / "runtime_events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )

    assert "state" not in event["payload"]
    assert event["state_transition"]["workflow_id"] == workflow.workflow_id
    assert len(json.dumps(event, ensure_ascii=False)) < 4000
