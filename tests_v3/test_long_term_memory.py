from __future__ import annotations

import tempfile
from pathlib import Path

from yueyue_v3.memory import (
    LongTermMemoryStore,
    MemoryDistiller,
    MemoryEntry,
    render_memory_note,
)


class FakeEmbedder:
    """Deterministic offline embedder: bag-of-character vector over a tiny vocab."""

    VOCAB = "辣咖哩貓生日七月遊戲考試媽媽拉麵天氣累"

    def embed(self, texts):
        return [[float(text.count(ch)) for ch in self.VOCAB] for text in texts]


class DeadEmbedder:
    def embed(self, texts):
        return []


def _store(embedder=None) -> LongTermMemoryStore:
    path = Path(tempfile.mkdtemp()) / "ltm.jsonl"
    return LongTermMemoryStore(path, embedder or FakeEmbedder())


def test_store_roundtrip_persists_across_reload():
    store = _store()
    store.add(MemoryEntry("fact", "主人喜歡吃辣", "2026-07-10", source="我超愛吃辣"))
    reloaded = LongTermMemoryStore(store.path, FakeEmbedder())
    assert len(reloaded.entries) == 1
    assert reloaded.entries[0].text == "主人喜歡吃辣"
    assert reloaded.entries[0].embedding  # embedding persisted inline


def test_retrieve_ranks_relevant_memory_first():
    store = _store()
    store.add(MemoryEntry("fact", "主人喜歡吃辣的咖哩", "2026-07-10"))
    store.add(MemoryEntry("episode", "主人說七月要考試", "2026-07-12"))
    hits = store.retrieve("今晚想吃辣的東西", k=1, min_similarity=0.1)
    assert hits and "辣" in hits[0].text


def test_retrieve_degrades_to_empty_on_embedding_failure():
    store = _store(DeadEmbedder())
    store.add(MemoryEntry("fact", "主人喜歡吃辣", "2026-07-10", embedding=[1.0, 0.0]))
    assert store.retrieve("吃什麼好") == []


def test_zero_recall_renders_no_note():
    assert render_memory_note([]) == ""


def test_note_is_dated_and_bounded():
    note = render_memory_note(
        [MemoryEntry("fact", "主人喜歡吃辣", "2026-07-10"), MemoryEntry("episode", "考試週很累", "2026-07-12")]
    )
    assert "2026-07-10" in note and "2026-07-12" in note
    assert len(note) <= 700
    assert "記不得就老實說" in note  # honesty hedge always present


def test_forget_removes_entry_and_rewrites_disk():
    store = _store()
    kept = store.add(MemoryEntry("fact", "主人喜歡吃辣", "2026-07-10"))
    wrong = store.add(MemoryEntry("fact", "主人生日是七月", "2026-07-11"))
    assert store.forget(wrong.entry_id)
    reloaded = LongTermMemoryStore(store.path, FakeEmbedder())
    assert [entry.entry_id for entry in reloaded.entries] == [kept.entry_id]


class ScriptedDistillProvider:
    def __init__(self, content: str):
        self.content = content

    def chat(self, messages, tools, **kwargs):
        class R:
            pass

        response = R()
        response.content = self.content
        return response


def test_distiller_keeps_grounded_facts_and_drops_fabrications():
    provider = ScriptedDistillProvider(
        '{"episode": "主人聊了晚餐想吃辣", "mood": "放鬆", "facts": ['
        '{"text": "主人喜歡吃辣", "source": "我超愛吃辣"},'
        '{"text": "主人養了三隻貓", "source": "我養了三隻貓"}]}'
    )
    distiller = MemoryDistiller(provider)
    turns = [
        ("user", "今晚想吃辣的，我超愛吃辣，越辣越有精神，改天帶你去吃四川火鍋"),
        ("assistant", "哦？主人口味很衝嘛，月月記住了"),
    ]
    entries = distiller.distill(turns, "2026-07-16")
    kinds = [(entry.kind, entry.text) for entry in entries]
    assert ("episode", "主人聊了晚餐想吃辣") in kinds
    assert any(entry.kind == "fact" and "辣" in entry.text for entry in entries)
    # "三隻貓" was never said by the owner - the grounding gate must drop it.
    assert not any("貓" in entry.text for entry in entries)


def test_distiller_survives_garbage_output():
    distiller = MemoryDistiller(ScriptedDistillProvider("完全不是 JSON 的回覆"))
    turns = [
        ("user", "隨便聊聊今天過得怎樣啊，說來話長，早上開會下午寫代碼晚上還要遛狗"),
        ("assistant", "聽起來主人今天行程排得滿滿的，辛苦了"),
    ]
    assert distiller.distill(turns, "2026-07-16") == []


def test_chat_turn_injects_memory_note_when_relevant():
    # End-to-end through ContextCompiler: a relevant memory shows up as a dated system note.
    from yueyue_v3.context import ContextCompiler, ShortContextStore
    from yueyue_v3.models import TurnEnvelope, TurnMode

    tmp = Path(tempfile.mkdtemp())
    (tmp / "workspace" / "brain").mkdir(parents=True)
    (tmp / "workspace" / "brain" / "personality.md").write_text("月月", encoding="utf-8")
    (tmp / "workspace" / "brain" / "rules.md").write_text("守規矩", encoding="utf-8")
    compiler = ContextCompiler(tmp, ShortContextStore(tmp / "sc.json"))
    store = _store()
    store.add(MemoryEntry("fact", "主人喜歡吃辣的咖哩", "2026-07-10"))
    compiler.memory = store
    messages = compiler.compile_turn(TurnEnvelope("owner", "今晚想吃辣的東西", TurnMode.CHAT))
    joined = "\n".join(m["content"] for m in messages if m["role"] == "system")
    assert "2026-07-10" in joined and "辣" in joined
    # and an unrelated message recalls nothing
    messages = compiler.compile_turn(TurnEnvelope("owner", "嗯。", TurnMode.CHAT))
    joined = "\n".join(m["content"] for m in messages if m["role"] == "system")
    assert "過往記憶" not in joined


def test_runtime_distill_trigger_gated_and_counted(monkeypatch, tmp_path):
    # The live-path trigger: off without opt-in; with opt-in, fires only on the Nth call.
    from yueyue_v3.providers import ProviderResponse, ScriptedProvider
    from yueyue_v3.runtime import YueYueRuntimeV3

    (tmp_path / "workspace" / "brain").mkdir(parents=True)
    (tmp_path / "workspace" / "brain" / "personality.md").write_text("月月", encoding="utf-8")
    (tmp_path / "workspace" / "brain" / "rules.md").write_text("守規矩", encoding="utf-8")
    provider = ScriptedProvider([ProviderResponse("好", "", []) for _ in range(5)])
    rt = YueYueRuntimeV3(tmp_path, provider, state_dir=tmp_path / "v3")

    monkeypatch.setenv("YUEYUE_LTM", "0")
    assert rt.maybe_distill_memory("owner", every=2) is False
    assert rt.maybe_distill_memory("owner", every=2) is False  # gated off: counter never fires

    monkeypatch.setenv("YUEYUE_LTM", "1")
    calls = []
    rt.memory_distiller.distill = lambda turns, date: calls.append(1) or []
    assert rt.maybe_distill_memory("owner", every=2) is False  # 1st counted call
    rt.maybe_distill_memory("owner", every=2)  # 2nd -> fires
    assert calls, "distiller must run on the Nth chat turn"


def test_commitment_extraction_and_due_resolution():
    provider = ScriptedDistillProvider(
        '{"episode": "主人明天要考試有點緊張", "mood": "緊張", "facts": [], "commitments": ['
        '{"text": "主人明天有考試", "source": "明天要考試了", "due": "明天"},'
        '{"text": "主人下週去旅行", "source": "編出來的話", "due": "明天"}]}'
    )
    distiller = MemoryDistiller(provider)
    turns = [
        ("user", "明天要考試了，好緊張啊，今晚得早點睡才行，不然明天肯定掛"),
        ("assistant", "主人加油，月月相信你"),
    ]
    entries = distiller.distill(turns, "2026-07-16")
    commitments = [entry for entry in entries if entry.kind == "commitment"]
    assert len(commitments) == 1  # the ungrounded one ("編出來的話") is dropped
    assert commitments[0].due_date == "2026-07-17"


def test_pop_due_commitments_consumes_once():
    store = _store()
    store.add(MemoryEntry("commitment", "主人明天有考試", "2026-07-16", due_date="2026-07-17"))
    store.add(MemoryEntry("commitment", "主人週五面試", "2026-07-16", due_date="2026-07-25"))
    due = store.pop_due_commitments("2026-07-17")
    assert [entry.text for entry in due] == ["主人明天有考試"]
    assert store.pop_due_commitments("2026-07-17") == []  # consumed - never nags twice
    # the future one survives on disk
    reloaded = LongTermMemoryStore(store.path, FakeEmbedder())
    assert [entry.text for entry in reloaded.entries] == ["主人週五面試"]


def test_presence_prioritizes_due_commitment():
    from agent_presence import PresenceEngine

    engine = PresenceEngine()
    engine.commitments_source = lambda: ["主人明天有考試"]
    kind, reason, confidence = engine._select_candidate({"turns": []})
    assert kind == "commitment_followup"
    assert "考試" in reason
    assert confidence >= 0.9
