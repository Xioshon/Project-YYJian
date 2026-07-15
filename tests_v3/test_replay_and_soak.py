from __future__ import annotations

from yueyue_v3.models import RuntimeEvent, TurnEnvelope, TurnMode
from yueyue_v3.providers import ProviderResponse, ScriptedProvider
from yueyue_v3.replay import build_regression_corpus, run_replay_corpus
from yueyue_v3.runtime import YueYueRuntimeV3


def test_known_bug_replay_corpus_is_fully_deterministic() -> None:
    results = run_replay_corpus(build_regression_corpus())
    assert results
    assert all(item.passed for item in results), [item.message for item in results if not item.passed]


def test_one_hundred_chat_turn_soak_stays_bounded_and_never_creates_workflow(runtime_root) -> None:
    provider = ScriptedProvider([ProviderResponse(f"回覆 {index}", "", []) for index in range(100)])
    runtime = YueYueRuntimeV3(runtime_root, provider)

    for index in range(100):
        reply = runtime.process_turn(TurnEnvelope("owner", f"聊天 {index}", TurnMode.CHAT))
        assert reply == f"回覆 {index}"
        assert runtime.state.workflow is None

    assert len(runtime.short_context.recent("owner", 1000)) == 20
    assert len(runtime.state.processed_event_ids) == 200
    assert len(runtime.event_store.read(1000)) == 200


def test_processed_event_ids_are_capped_at_two_thousand(runtime_root) -> None:
    provider = ScriptedProvider([ProviderResponse("ok", "", [])])
    runtime = YueYueRuntimeV3(runtime_root, provider)

    with runtime.events.writer_scope():
        for index in range(2500):
            runtime.events.apply(RuntimeEvent("test.synthetic", runtime.state.session_id, payload={"index": index}))

    assert len(runtime.state.processed_event_ids) == 2000
    assert runtime.state.processed_event_ids[-1] != runtime.state.processed_event_ids[0]
