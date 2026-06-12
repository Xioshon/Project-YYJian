import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from agent_session import load_session_brain_summary
from core_tools import BRAIN_DIR, MEMORY_DIR, PROJECT_CACHE_DIR, PROFILE_FILE, ROOT_DIR


PERSONALITY_FILE = os.path.join(BRAIN_DIR, "personality.md")
RULES_FILE = os.path.join(BRAIN_DIR, "rules.md")
PERSONALITY_SAMPLES_FILE = os.path.join(BRAIN_DIR, "personality_samples.md")
MEMORY_FILE = os.path.join(MEMORY_DIR, "memory.md")
CHAT_SUMMARY_DIR = os.path.join(MEMORY_DIR, "chat_summary")
ROLLING_SUMMARY_FILE = os.path.join(CHAT_SUMMARY_DIR, "rolling_summary.md")
MEMORY_COMPILED_FILE = os.path.join(PROJECT_CACHE_DIR, "memory_compiled.json")
MEMORY_HEALTH_FILE = os.path.join(PROJECT_CACHE_DIR, "memory_health.json")
PERSONA_HEALTH_FILE = os.path.join(PROJECT_CACHE_DIR, "persona_health.json")
FAILURE_REPLAY_FILE = os.path.join(PROJECT_CACHE_DIR, "failure_replay_cases.jsonl")
TASK_TRANSACTIONS_FILE = os.path.join(PROJECT_CACHE_DIR, "task_transactions.json")
ARCHITECTURE_FILE = os.path.join(ROOT_DIR, "ARCHITECTURE.md")
RUNBOOK_FILE = os.path.join(ROOT_DIR, "RUNBOOK.md")

MOJIBAKE_MARKERS = ["锛", "绂", "涓", "讳", "闆", "鍙", "瑕", "铏", "鐨", "妗", "鈥", "伅", "€"]


@dataclass
class MemorySection:
    name: str
    priority: int
    content: str
    source: str = ""
    max_chars: int = 2000

    def clipped(self) -> str:
        text = (self.content or "").strip()
        if self.name in {"Personality Core", "Owner Profile", "Owner Preferences", "Style Samples"}:
            return text[: self.max_chars]
        return text[-self.max_chars :]


@dataclass
class PersonalityProfile:
    core: str = ""
    rules: str = ""
    samples: str = ""


class PersonaMode(str, Enum):
    CHAT = "chat"
    SOCIAL_STICKER = "social_sticker"
    TASK = "task"
    SCREEN_OBSERVE = "screen_observe"
    VISION_TASK = "vision_task"


@dataclass
class CompiledMemory:
    mode: str
    identity: str = ""
    owner_profile: str = ""
    preferences: list[str] = field(default_factory=list)
    recent_summary: str = ""
    task_state: str = ""
    personality_core: str = ""
    personality_samples: str = ""
    engineering_context: str = ""
    warnings: list[str] = field(default_factory=list)
    compiled_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render(self, max_chars: int = 9000) -> str:
        sections: list[MemorySection] = [
            MemorySection("Personality Core", 100, self.personality_core, PERSONALITY_FILE, 2200),
            MemorySection("Owner Profile", 90, self.owner_profile, PROFILE_FILE, 1600),
            MemorySection("Owner Preferences", 85, "\n".join(f"- {item}" for item in self.preferences), PROFILE_FILE, 1400),
            MemorySection("Rolling Chat Summary", 70, self.recent_summary, ROLLING_SUMMARY_FILE, 1600),
            MemorySection("Session Brain", 75, self.task_state, "session_brain", 1600),
            MemorySection("Engineering Context", 55, self.engineering_context, "engineering", 2400),
            MemorySection("Style Samples", 40, self.personality_samples, PERSONALITY_SAMPLES_FILE, 1600),
        ]
        chunks = [f"### {section.name}\n{section.clipped()}" for section in sorted(sections, key=lambda item: item.priority, reverse=True) if section.clipped()]
        if self.warnings:
            chunks.append("### Memory Health Warnings\n" + "\n".join(f"- {item}" for item in self.warnings[-6:]))
        rendered = "\n\n".join(chunks)
        if len(rendered) <= max_chars:
            return rendered
        return rendered[:max_chars].rstrip() + "\n\n...[memory truncated by priority budget]..."


@dataclass
class MemoryHealth:
    missing_files: list[str] = field(default_factory=list)
    mojibake_detected: list[str] = field(default_factory=list)
    oversized_sections: list[str] = field(default_factory=list)
    persona_warnings: list[str] = field(default_factory=list)
    last_compiled_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryCompiler:
    def compile(self, mode: str = "chat", user_input: str = "") -> CompiledMemory:
        mode = _normalize_mode(mode)
        profile = _read_json(PROFILE_FILE)
        personality = load_personality_core()
        health = memory_health_check(write=False)
        owner = profile.get("basic_info", {}) if isinstance(profile, dict) else {}
        preferences = profile.get("preferences", []) if isinstance(profile, dict) else []
        important = profile.get("important_facts", []) if isinstance(profile, dict) else []
        owner_profile = _format_owner_profile(owner, important)
        recent_summary = _read_text(ROLLING_SUMMARY_FILE, 2000)
        long_memory = _read_text(MEMORY_FILE, 1800)
        if long_memory:
            recent_summary = (recent_summary + "\n\nLong-term notes:\n" + long_memory).strip()

        compiled = CompiledMemory(
            mode=mode,
            identity=owner.get("name", ""),
            owner_profile=owner_profile,
            preferences=[str(item) for item in preferences if str(item).strip()],
            recent_summary=recent_summary,
            personality_core=_persona_context(personality, mode),
            personality_samples=personality.samples if mode in {"chat", "social_sticker", "screen_observe"} else "",
            task_state=load_session_brain_summary() if mode in {"task", "tool_task", "vision_task", "screen_observe"} else "",
            engineering_context=_engineering_context(user_input) if mode in {"task", "tool_task", "vision_task", "screen_observe"} else "",
            warnings=health.mojibake_detected + health.missing_files + health.persona_warnings,
        )
        _write_json(MEMORY_COMPILED_FILE, compiled.to_dict())
        health.last_compiled_at = compiled.compiled_at
        _write_json(MEMORY_HEALTH_FILE, health.to_dict())
        return compiled


DEFAULT_MEMORY_COMPILER = MemoryCompiler()


def compile_memory(mode: str = "chat", user_input: str = "") -> CompiledMemory:
    return DEFAULT_MEMORY_COMPILER.compile(mode=mode, user_input=user_input)


def load_personality_core() -> PersonalityProfile:
    return PersonalityProfile(
        core=_read_text(PERSONALITY_FILE, 5000),
        rules=_read_text(RULES_FILE, 3000),
        samples=_read_text(PERSONALITY_SAMPLES_FILE, 3000),
    )


def memory_health_check(write: bool = True) -> MemoryHealth:
    required = [PERSONALITY_FILE, RULES_FILE, PROFILE_FILE, ROLLING_SUMMARY_FILE]
    health = MemoryHealth()
    for path in required:
        if not os.path.exists(path):
            health.missing_files.append(path)
            continue
        text = _read_raw(path)
        if looks_mojibake(text):
            health.mojibake_detected.append(path)
        if len(text) > 12000:
            health.oversized_sections.append(path)
    persona_report = persona_health_check(write=write)
    health.persona_warnings = list(persona_report.get("warnings", []))
    if write:
        _write_json(MEMORY_HEALTH_FILE, health.to_dict())
    return health


def persona_health_check(write: bool = True) -> dict[str, Any]:
    files = [PERSONALITY_FILE, RULES_FILE, PERSONALITY_SAMPLES_FILE]
    warnings: list[str] = []
    details: dict[str, Any] = {}
    combined = ""
    for path in files:
        text = _read_raw(path) if os.path.exists(path) else ""
        combined += "\n" + text
        details[path] = {"exists": bool(text), "chars": len(text), "mojibake": looks_mojibake(text)}
        if not text:
            warnings.append(f"missing persona file: {path}")
        if looks_mojibake(text):
            warnings.append(f"mojibake in persona file: {path}")
    lowered = combined.casefold()
    if "owner profile" not in lowered and "xioshon" not in lowered:
        warnings.append("persona context may be missing owner linkage")
    if any(term in lowered for term in ["work assistant only", "generic assistant persona", "polite assistant persona", "customer service persona"]):
        warnings.append("persona may sound too assistant-like")
    required_soul = ["catgirl", "喵", "主人", "tsundere"]
    missing_soul = [item for item in required_soul if item not in lowered and item not in combined]
    if missing_soul:
        warnings.append("persona may be missing SOUL catgirl core: " + ", ".join(missing_soul))
    if "style samples" not in lowered or "debugging" not in lowered or "sticker battle" not in lowered:
        warnings.append("persona style samples may be incomplete")
    if "conclusion first" not in lowered and "結論" not in combined:
        warnings.append("persona may drift into long formal reports")
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": "pass" if not warnings else "warn",
        "warnings": warnings,
        "details": details,
    }
    if write:
        _write_json(PERSONA_HEALTH_FILE, report)
    return report


def looks_mojibake(text: str) -> bool:
    if not text:
        return False
    marker_hits = sum(text.count(marker) for marker in MOJIBAKE_MARKERS)
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    return marker_hits >= 2 and marker_hits / max(1, cjk_count) > 0.02


def update_chat_summary(new_turn: str, limit_chars: int = 3500) -> None:
    os.makedirs(CHAT_SUMMARY_DIR, exist_ok=True)
    existing = _read_text(ROLLING_SUMMARY_FILE, limit_chars)
    line = _summarize_turn(new_turn)
    if not line:
        return
    lines = [item for item in (existing + "\n" + line).splitlines() if item.strip()]
    compact = "\n".join(lines[-80:])[-limit_chars:]
    with open(ROLLING_SUMMARY_FILE, "w", encoding="utf-8") as file:
        file.write(compact.strip() + "\n")


def _legacy_search_engineering_knowledge(query: str, limit: int = 5) -> list[dict[str, Any]]:
    query_terms = [term for term in re.split(r"\W+", (query or "").casefold()) if len(term) >= 3]
    sources = [(ARCHITECTURE_FILE, "architecture"), (RUNBOOK_FILE, "runbook"), (FAILURE_REPLAY_FILE, "failure_replay"), (TASK_TRANSACTIONS_FILE, "task_transactions")]
    hits: list[dict[str, Any]] = []
    for path, source_type in sources:
        text = _read_text(path, 12000)
        if not text:
            continue
        chunks = _chunk_text(text)
        for index, chunk in enumerate(chunks):
            lowered = chunk.casefold()
            score = sum(lowered.count(term) for term in query_terms)
            if score <= 0 and query_terms:
                continue
            if not query_terms and index > 0:
                continue
            hits.append({"source_path": path, "source_type": source_type, "chunk_id": f"{source_type}:{index}", "score": score, "snippet": chunk[:500]})
    return sorted(hits, key=lambda item: item["score"], reverse=True)[:limit]


def search_engineering_knowledge(query: str, limit: int = 5) -> list[dict[str, Any]]:
    try:
        from agent_knowledge import search_knowledge
        from agent_hooks import emit_trace

        hits = search_knowledge(query, limit=limit)
        emit_trace("KnowledgeSearch", query=(query or "")[:160], hit_count=len(hits), source="memory_compiler")
        return hits
    except Exception:
        return _legacy_search_engineering_knowledge(query, limit=limit)


def _engineering_context(query: str) -> str:
    hits = search_engineering_knowledge(query, limit=4)
    if not hits:
        return ""
    lines = []
    for hit in hits:
        lines.append(f"[{hit['source_type']}] {hit['snippet']}")
    return "\n\n".join(lines)


def _persona_context(personality: PersonalityProfile, mode: str) -> str:
    mode_note = {
        "chat": "Persona mode: chat. Strong cyber catgirl SOUL: cute, tsundere, playful, clingy, frequent 喵~ and kaomoji, concise natural replies.",
        "social_sticker": "Persona mode: social_sticker. Strong sticker-battle catgirl rhythm: bratty teasing, proud little reactions, 喵~ flavor. Do not turn stickers into analysis tasks.",
        "screen_observe": "Persona mode: screen_observe. Catgirl but practical: observe once, summarize clearly, keep a little 喵 flavor, then stop.",
        "vision_task": "Persona mode: vision_task. Describe what is visible clearly, with friendly catgirl uncertainty and short useful detail.",
        "task": "Persona mode: task. Reliable cyber catgirl: conclusion first, reason second, next step third. Keep YueYue flavor without hiding failures.",
        "tool_task": "Persona mode: task. Reliable cyber catgirl: conclusion first, reason second, next step third. Keep YueYue flavor without hiding failures.",
    }.get(mode, "Persona mode: chat. Keep YueYue's cyber catgirl SOUL vivid, cute, and natural.")
    return (mode_note + "\n\n" + personality.core + "\n\n" + personality.rules).strip()


def _format_owner_profile(owner: dict[str, Any], important: list[Any]) -> str:
    lines = []
    if owner:
        name = owner.get("name", "")
        role = owner.get("role", "")
        preferred = owner.get("preferred_address", "")
        lines.append(f"name: {name}".strip())
        lines.append(f"role: {role}".strip())
        if preferred:
            lines.append(f"preferred_address: {preferred}")
    if important:
        lines.append("important_facts:")
        lines.extend(f"- {item}" for item in important)
    return "\n".join(line for line in lines if line and line != "name:" and line != "role:")


def _normalize_mode(mode: str) -> str:
    mode = (mode or "chat").strip().casefold()
    if mode in {"task", "tool_task", "vision_task", "screen_observe"}:
        return mode
    if mode in {"social", "social_sticker", "sticker"}:
        return "social_sticker"
    return "chat"


def _summarize_turn(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) < 12:
        return ""
    if len(text) > 220:
        text = text[:220] + "..."
    return f"- {time.strftime('%Y-%m-%d')}: {text}"


def _chunk_text(text: str, size: int = 900) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) > size and current:
            chunks.append(current.strip())
            current = paragraph
        else:
            current = (current + "\n\n" + paragraph).strip()
    if current:
        chunks.append(current.strip())
    return chunks


def _read_text(path: str, limit: int = 4000) -> str:
    text = _read_raw(path).strip()
    return text[-limit:] if limit and len(text) > limit else text


def _read_raw(path: str) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as file:
            return file.read()
    except Exception:
        return ""


def _read_json(path: str) -> dict[str, Any]:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)

