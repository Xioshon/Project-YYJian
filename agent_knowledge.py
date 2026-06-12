import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from typing import Any


ROOT_DIR = os.path.abspath(os.getenv("YUEYUE_ROOT_DIR") or os.path.dirname(__file__))
WORKSPACE_DIR = os.path.join(ROOT_DIR, "workspace")
BRAIN_DIR = os.path.join(WORKSPACE_DIR, "brain")
MEMORY_DIR = os.path.join(WORKSPACE_DIR, "memory")
CHAT_SUMMARY_DIR = os.path.join(MEMORY_DIR, "chat_summary")
PROJECT_CACHE_DIR = os.path.join(WORKSPACE_DIR, "project_cache")

ARCHITECTURE_FILE = os.path.join(ROOT_DIR, "ARCHITECTURE.md")
RUNBOOK_FILE = os.path.join(ROOT_DIR, "RUNBOOK.md")
ROLLING_SUMMARY_FILE = os.path.join(CHAT_SUMMARY_DIR, "rolling_summary.md")
TASK_TRANSACTIONS_FILE = os.path.join(PROJECT_CACHE_DIR, "task_transactions.json")
FAILURE_REPLAY_FILE = os.path.join(PROJECT_CACHE_DIR, "failure_replay_cases.jsonl")
TASK_GRAPHS_FILE = os.path.join(PROJECT_CACHE_DIR, "task_graphs.json")
WORKFLOW_REPLAY_FILE = os.path.join(PROJECT_CACHE_DIR, "workflow_replay_cases.jsonl")

KNOWLEDGE_MANIFEST_FILE = os.path.join(PROJECT_CACHE_DIR, "knowledge_manifest.json")
KNOWLEDGE_CHUNKS_FILE = os.path.join(PROJECT_CACHE_DIR, "knowledge_chunks.jsonl")
KNOWLEDGE_INDEX_FILE = os.path.join(PROJECT_CACHE_DIR, "knowledge_index.jsonl")

MAX_SOURCE_CHARS = 60000
CHUNK_TARGET_CHARS = 1100
CHUNK_OVERLAP_CHARS = 120
TOKEN_RE = re.compile(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", re.IGNORECASE)
SKIP_SUFFIXES = {
    ".env",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".ico",
    ".mp4",
    ".webm",
    ".tgs",
    ".pyc",
}
EXCLUDED_PATH_PARTS = {
    os.path.normcase(os.path.join("workspace", "chat_history")),
    os.path.normcase(os.path.join("workspace", "assets", "tg_images")),
    os.path.normcase(os.path.join("workspace", "assets", "screenshots")),
}


@dataclass
class KnowledgeSource:
    source_id: str
    path: str
    source_type: str
    mtime: float
    size: int
    hash: str


@dataclass
class KnowledgeChunk:
    chunk_id: str
    source_id: str
    source_path: str
    source_type: str
    title: str
    text: str
    tags: list[str]


@dataclass
class KnowledgeHit:
    chunk_id: str
    score: float
    source_path: str
    source_type: str
    title: str
    snippet: str
    tags: list[str]


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _rel(path: str) -> str:
    try:
        return os.path.relpath(os.path.abspath(path), ROOT_DIR).replace("\\", "/")
    except ValueError:
        return os.path.abspath(path)


def _read_text(path: str, limit: int = MAX_SOURCE_CHARS) -> str:
    try:
        with open(path, "r", encoding="utf-8") as file:
            return file.read(limit)
    except UnicodeDecodeError:
        try:
            with open(path, "r", encoding="utf-8-sig") as file:
                return file.read(limit)
        except Exception:
            return ""
    except Exception:
        return ""


def _file_hash(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as file:
            for block in iter(lambda: file.read(65536), b""):
                digest.update(block)
        return digest.hexdigest()
    except Exception:
        return ""


def _tokenize(text: str) -> list[str]:
    return [token.casefold() for token in TOKEN_RE.findall(text or "") if len(token.strip()) >= 2]


def _title_for(path: str, text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:120] or os.path.basename(path)
    return os.path.basename(path)


def _tags_for(source_type: str, path: str, text: str) -> list[str]:
    tags = [source_type]
    lowered = f"{path}\n{text[:2000]}".casefold()
    for tag in ["permission", "replay", "debounce", "execute_command", "cwd", "memory", "personality", "telegram", "verification", "failure"]:
        if tag in lowered:
            tags.append(tag)
    return sorted(set(tags))


def _chunk_text(text: str, target: int = CHUNK_TARGET_CHARS) -> list[str]:
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text or "") if item.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > target * 2:
            for start in range(0, len(paragraph), target):
                piece = paragraph[start : start + target].strip()
                if piece:
                    chunks.append(piece)
            continue
        if current and len(current) + len(paragraph) + 2 > target:
            chunks.append(current.strip())
            tail = current[-CHUNK_OVERLAP_CHARS:].strip()
            current = (tail + "\n\n" + paragraph).strip() if tail else paragraph
        else:
            current = (current + "\n\n" + paragraph).strip()
    if current:
        chunks.append(current.strip())
    return chunks or ([text.strip()] if text.strip() else [])


def _write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return default


def _write_jsonl(path: str, rows: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except Exception:
        return []
    return rows


class KnowledgeIndexer:
    def __init__(self, root_dir: str = ROOT_DIR):
        self.root_dir = os.path.abspath(root_dir)

    def discover_sources(self) -> list[tuple[str, str]]:
        sources: list[tuple[str, str]] = []
        for path, source_type in [
            (ARCHITECTURE_FILE, "architecture"),
            (RUNBOOK_FILE, "runbook"),
            (ROLLING_SUMMARY_FILE, "chat_summary"),
            (TASK_TRANSACTIONS_FILE, "task_transactions"),
            (FAILURE_REPLAY_FILE, "failure_replay"),
            (TASK_GRAPHS_FILE, "task_graphs"),
            (WORKFLOW_REPLAY_FILE, "workflow_replay"),
        ]:
            self._append_source(sources, path, source_type)
        if os.path.isdir(BRAIN_DIR):
            for name in sorted(os.listdir(BRAIN_DIR)):
                path = os.path.join(BRAIN_DIR, name)
                if name.lower().endswith(".md"):
                    self._append_source(sources, path, "brain")
        return sources

    def _append_source(self, sources: list[tuple[str, str]], path: str, source_type: str) -> None:
        if os.path.exists(path) and self.is_allowed_source(path):
            sources.append((os.path.abspath(path), source_type))

    def is_allowed_source(self, path: str) -> bool:
        absolute = _norm(path)
        rel = os.path.normcase(_rel(path))
        basename = os.path.basename(path).casefold()
        suffix = os.path.splitext(basename)[1]
        if basename in {".env", "tg_chat_id.txt"} or suffix in SKIP_SUFFIXES:
            return False
        if any(part in rel for part in EXCLUDED_PATH_PARTS):
            return False
        allowed = {
            _norm(ARCHITECTURE_FILE),
            _norm(RUNBOOK_FILE),
            _norm(ROLLING_SUMMARY_FILE),
            _norm(TASK_TRANSACTIONS_FILE),
            _norm(FAILURE_REPLAY_FILE),
            _norm(TASK_GRAPHS_FILE),
            _norm(WORKFLOW_REPLAY_FILE),
        }
        if absolute in allowed:
            return True
        return os.path.commonpath([_norm(BRAIN_DIR), absolute]) == _norm(BRAIN_DIR) and basename.endswith(".md")

    def current_sources(self) -> list[KnowledgeSource]:
        result: list[KnowledgeSource] = []
        for path, source_type in self.discover_sources():
            stat = os.stat(path)
            source_id = hashlib.sha1(_rel(path).encode("utf-8")).hexdigest()[:12]
            result.append(
                KnowledgeSource(
                    source_id=source_id,
                    path=_rel(path),
                    source_type=source_type,
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                    hash=_file_hash(path),
                )
            )
        return result

    def is_current(self) -> bool:
        if not (os.path.exists(KNOWLEDGE_MANIFEST_FILE) and os.path.exists(KNOWLEDGE_CHUNKS_FILE) and os.path.exists(KNOWLEDGE_INDEX_FILE)):
            return False
        manifest = _read_json(KNOWLEDGE_MANIFEST_FILE, {})
        current = [asdict(item) for item in self.current_sources()]
        previous = manifest.get("sources", [])
        return [(item.get("path"), item.get("hash"), item.get("size")) for item in previous] == [
            (item.get("path"), item.get("hash"), item.get("size")) for item in current
        ]

    def build(self, force: bool = False) -> dict[str, Any]:
        if not force and self.is_current():
            return _read_json(KNOWLEDGE_MANIFEST_FILE, {})

        sources = self.current_sources()
        chunks: list[KnowledgeChunk] = []
        index_rows: list[dict[str, Any]] = []
        for source in sources:
            source_path = os.path.join(ROOT_DIR, source.path)
            text = _read_text(source_path)
            title = _title_for(source_path, text)
            tags = _tags_for(source.source_type, source.path, text)
            for chunk_index, chunk_text in enumerate(_chunk_text(text)):
                chunk_id = f"{source.source_id}:{chunk_index}"
                chunk = KnowledgeChunk(
                    chunk_id=chunk_id,
                    source_id=source.source_id,
                    source_path=source.path,
                    source_type=source.source_type,
                    title=title,
                    text=chunk_text,
                    tags=tags,
                )
                chunks.append(chunk)
                terms: dict[str, int] = {}
                for token in _tokenize(f"{title} {chunk_text} {' '.join(tags)}"):
                    terms[token] = terms.get(token, 0) + 1
                index_rows.append(
                    {
                        "chunk_id": chunk_id,
                        "source_id": source.source_id,
                        "source_path": source.path,
                        "source_type": source.source_type,
                        "title": title,
                        "tags": tags,
                        "terms": terms,
                    }
                )

        manifest = {
            "version": 1,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source_count": len(sources),
            "chunk_count": len(chunks),
            "sources": [asdict(source) for source in sources],
        }
        _write_json(KNOWLEDGE_MANIFEST_FILE, manifest)
        _write_jsonl(KNOWLEDGE_CHUNKS_FILE, [asdict(chunk) for chunk in chunks])
        _write_jsonl(KNOWLEDGE_INDEX_FILE, index_rows)
        return manifest


class KnowledgeStore:
    def __init__(self, indexer: KnowledgeIndexer | None = None):
        self.indexer = indexer or KnowledgeIndexer()

    def ensure_index(self) -> dict[str, Any]:
        return self.indexer.build(force=False)

    def rebuild(self) -> dict[str, Any]:
        return self.indexer.build(force=True)

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        self.ensure_index()
        query_terms = _tokenize(query or "")
        if not query_terms:
            return []
        try:
            limit = max(1, min(int(limit), 10))
        except Exception:
            limit = 5

        chunks = {row.get("chunk_id"): row for row in _read_jsonl(KNOWLEDGE_CHUNKS_FILE)}
        hits: list[KnowledgeHit] = []
        for row in _read_jsonl(KNOWLEDGE_INDEX_FILE):
            terms = row.get("terms", {})
            title_terms = set(_tokenize(row.get("title", "")))
            score = 0.0
            for term in query_terms:
                score += float(terms.get(term, 0))
                if term in title_terms:
                    score += 1.5
                if term in row.get("tags", []):
                    score += 1.0
            if score <= 0:
                continue
            chunk = chunks.get(row.get("chunk_id"), {})
            text = chunk.get("text", "")
            hits.append(
                KnowledgeHit(
                    chunk_id=row.get("chunk_id", ""),
                    score=round(score, 3),
                    source_path=row.get("source_path", ""),
                    source_type=row.get("source_type", ""),
                    title=row.get("title", ""),
                    snippet=_snippet(text, query_terms),
                    tags=row.get("tags", []),
                )
            )
        hits.sort(key=lambda item: (item.score, item.source_type != "chat_summary"), reverse=True)
        return [asdict(hit) for hit in hits[:limit]]

    def read(self, chunk_id: str) -> dict[str, Any] | None:
        self.ensure_index()
        for row in _read_jsonl(KNOWLEDGE_CHUNKS_FILE):
            if row.get("chunk_id") == chunk_id:
                return row
        return None


def _snippet(text: str, query_terms: list[str], limit: int = 520) -> str:
    lowered = (text or "").casefold()
    first = -1
    for term in query_terms:
        index = lowered.find(term)
        if index >= 0 and (first < 0 or index < first):
            first = index
    if first < 0:
        return (text or "")[:limit]
    start = max(0, first - 160)
    end = min(len(text), start + limit)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix


DEFAULT_KNOWLEDGE_STORE = KnowledgeStore()


def reindex_workspace() -> dict[str, Any]:
    return DEFAULT_KNOWLEDGE_STORE.rebuild()


def search_knowledge(query: str, limit: int = 5) -> list[dict[str, Any]]:
    return DEFAULT_KNOWLEDGE_STORE.search(query, limit=limit)


def read_knowledge(chunk_id: str) -> dict[str, Any] | None:
    return DEFAULT_KNOWLEDGE_STORE.read(chunk_id)
