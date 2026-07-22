"""Long-term memory layer (ROADMAP Phase 2).

Three pieces:
- LongTermMemoryStore: JSONL-backed episodic summaries + semantic facts, embeddings inline,
  cosine retrieval. Degrades gracefully - any embedding failure means "no memories recalled",
  never a broken chat turn.
- MemoryDistiller: turns a window of recent chat turns into one episode summary + grounded
  facts via the cheap chat model. Every fact must quote its source from the actual transcript
  (the same anti-fabrication stance as report_result grounding) or it is dropped.
- SiliconFlowEmbedder: BAAI/bge-m3 (dirt cheap). Tests use a deterministic fake.

Honesty contract: retrieved memories are injected as *possibly relevant, dated* notes; the chat
contract already tells YueYue to admit not remembering rather than invent. Zero recall = zero
note - never a fabricated past.
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_MAX_ENTRIES = 2000
_EMBED_DIM_LIMIT = 4096


@dataclass
class MemoryEntry:
    kind: str  # "episode" | "fact"
    text: str
    date: str  # YYYY-MM-DD (when it happened / was said)
    topics: list[str] = field(default_factory=list)
    mood: str = ""
    source: str = ""  # for facts/commitments: the owner quote that grounds it
    confidence: float = 0.8
    due_date: str = ""  # for commitments: YYYY-MM-DD when a follow-up becomes due
    embedding: list[float] = field(default_factory=list)
    entry_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)


class SiliconFlowEmbedder:
    """bge-m3 embeddings. Any failure returns [] so callers degrade to keyword-free recall."""

    def __init__(self, api_key: str, model: str = "BAAI/bge-m3"):
        self.api_key = api_key
        self.model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts or not self.api_key:
            return []
        try:
            import httpx
            from openai import OpenAI

            with httpx.Client(timeout=4.0) as http_client:
                client = OpenAI(
                    api_key=self.api_key, base_url="https://api.siliconflow.cn/v1", http_client=http_client
                )
                response = client.embeddings.create(model=self.model, input=[t[:1600] for t in texts])
            return [list(item.embedding)[:_EMBED_DIM_LIMIT] for item in response.data]
        except Exception:
            return []


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class LongTermMemoryStore:
    def __init__(self, path: str | Path, embedder: Any):
        self.path = Path(path)
        self.embedder = embedder
        self.entries: list[MemoryEntry] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                raw = json.loads(line)
                if isinstance(raw, dict) and raw.get("text"):
                    known = {f.name for f in MemoryEntry.__dataclass_fields__.values()}
                    self.entries.append(MemoryEntry(**{k: v for k, v in raw.items() if k in known}))
        except Exception:
            self.entries = []
        self.entries = self.entries[-_MAX_ENTRIES:]

    def _append_to_disk(self, entry: MemoryEntry) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        except Exception:
            pass

    def add(self, entry: MemoryEntry) -> MemoryEntry:
        if not entry.embedding:
            vectors = self.embedder.embed([entry.text])
            entry.embedding = vectors[0] if vectors else []
        self.entries.append(entry)
        self.entries = self.entries[-_MAX_ENTRIES:]
        self._append_to_disk(entry)
        return entry

    def retrieve(self, query: str, k: int = 4, min_similarity: float = 0.45) -> list[MemoryEntry]:
        """Top-k relevant memories. Empty on any failure - recall silence is always safe."""
        query = str(query or "").strip()
        if not query or not self.entries:
            return []
        vectors = self.embedder.embed([query])
        if not vectors:
            return []
        query_vec = vectors[0]
        scored = [
            (_cosine(query_vec, entry.embedding), entry) for entry in self.entries if entry.embedding
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [entry for score, entry in scored[:k] if score >= min_similarity]

    def episodes_on(self, date: str, limit: int = 2) -> list[MemoryEntry]:
        """Episodes from a specific day (newest first) - powers emotional continuity: yesterday's
        low mood surfacing naturally when the owner opens today's first conversation."""
        matches = [entry for entry in self.entries if entry.kind == "episode" and entry.date == date]
        return list(reversed(matches))[:limit]

    def pop_due_commitments(self, today: str) -> list[MemoryEntry]:
        """Return commitments due today-or-earlier and CONSUME them (a follow-up fires once;
        repeat-nagging about the same exam is worse than occasionally missing one)."""
        due = [
            entry
            for entry in self.entries
            if entry.kind == "commitment" and entry.due_date and entry.due_date <= today
        ]
        for entry in due:
            self.forget(entry.entry_id)
        return due

    def forget(self, entry_id: str) -> bool:
        """Owner-correction path: drop a wrong memory and rewrite the file."""
        before = len(self.entries)
        self.entries = [entry for entry in self.entries if entry.entry_id != entry_id]
        if len(self.entries) == before:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as handle:
                for entry in self.entries:
                    handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        except Exception:
            pass
        return True


def render_memory_note(entries: list[MemoryEntry], limit_chars: int = 700) -> str:
    """Bounded system note. Dated, hedged, and honest about uncertainty."""
    if not entries:
        return ""
    lines = [
        "### 可能相關的過往記憶（帶日期；只在真的對上話題時自然引用，不確定就別提，記不得就老實說）"
    ]
    for entry in entries:
        tag = "事實" if entry.kind == "fact" else "當天"
        lines.append(f"- [{entry.date} {tag}] {entry.text}"[:220])
    return "\n".join(lines)[:limit_chars]


_DISTILL_PROMPT = (
    "你在為陪伴 agent 月月整理與主人的對話記憶。從下面的對話抽取：\n"
    "1. episode：這段對話的一句話摘要（發生了什麼、主人狀態如何），40字內。\n"
    "2. facts：值得長期記住的穩定事實（喜好/人物/習慣/紀念日），每條都必須附 source——"
    "主人原話的逐字片段。沒有就給空陣列。猜測、模型自己說的話、一次性瑣事都不要。\n"
    "3. commitments：主人提到的、之後值得關心跟進的事（明天考試/週末面試/等下出門），"
    "每條附 source（主人原話逐字片段）和 due（今天/明天/後天 或 YYYY-MM-DD）。沒有就空陣列。\n"
    "只回 JSON："
    '{"episode": "...", "mood": "...", "facts": [{"text": "...", "source": "..."}], '
    '"commitments": [{"text": "...", "source": "...", "due": "..."}]}\n\n'
    "對話：\n{transcript}"
)


def _resolve_due(hint: str, today: str) -> str:
    hint = str(hint or "").strip()
    if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", hint):
        return hint
    try:
        import datetime

        base = datetime.date.fromisoformat(today)
        offsets = {"今天": 0, "今晚": 0, "等下": 0, "明天": 1, "聽日": 1, "後天": 2, "后天": 2}
        for key, days in offsets.items():
            if key in hint:
                return (base + datetime.timedelta(days=days)).isoformat()
    except Exception:
        pass
    return ""


class MemoryDistiller:
    """Distills a chat window into memory entries using the cheap chat model."""

    def __init__(self, provider: Any, chat_model: str = ""):
        self.provider = provider
        self.chat_model = chat_model

    def distill(self, turns: list[tuple[str, str]], date: str) -> list[MemoryEntry]:
        transcript = "\n".join(f"{'主人' if role == 'user' else '月月'}: {text}" for role, text in turns if text)
        if len(transcript) < 40:
            return []
        try:
            kwargs = {"model": self.chat_model} if self.chat_model else {}
            response = self.provider.chat(
                [{"role": "user", "content": _DISTILL_PROMPT.replace("{transcript}", transcript[:4000])}],
                [],
                **kwargs,
            )
            raw = _extract_json(getattr(response, "content", "") or "")
        except Exception:
            return []
        if not isinstance(raw, dict):
            return []
        entries: list[MemoryEntry] = []
        episode = str(raw.get("episode") or "").strip()
        if episode:
            entries.append(MemoryEntry("episode", episode[:200], date, mood=str(raw.get("mood") or "")[:40]))
        owner_text = "".join(text for role, text in turns if role == "user")
        for item in raw.get("facts") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            source = str(item.get("source") or "").strip()
            # Grounding gate: the quoted source must actually appear in what the owner said.
            # A fact the transcript cannot back is a fabrication and gets dropped (same stance
            # as report_result grounding).
            if text and source and _normalized(source) in _normalized(owner_text):
                entries.append(MemoryEntry("fact", text[:200], date, source=source[:160]))
        for item in raw.get("commitments") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            source = str(item.get("source") or "").strip()
            due = _resolve_due(str(item.get("due") or ""), date)
            # Same grounding stance; a commitment additionally needs a resolvable due date -
            # "sometime" follow-ups would nag randomly instead of caringly.
            if text and due and source and _normalized(source) in _normalized(owner_text):
                entries.append(MemoryEntry("commitment", text[:200], date, source=source[:160], due_date=due))
        return entries


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).casefold()


def _extract_json(text: str) -> Any:
    match = re.search(r"\{.*\}", str(text or ""), re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def build_default_memory(root: str | Path, api_key: str, provider: Any, chat_model: str = "") -> tuple[
    LongTermMemoryStore, MemoryDistiller
]:
    store = LongTermMemoryStore(
        Path(root) / "workspace" / "memory" / "long_term_memory.jsonl", SiliconFlowEmbedder(api_key)
    )
    return store, MemoryDistiller(provider, chat_model)


DistillTrigger = Callable[[], None]
