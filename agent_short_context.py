import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from agent_hooks import emit_trace
from agent_url_context import URLContextEntry, inspect_urls_for_text, render_url_context, should_preview_url


ROOT_DIR = os.path.abspath(os.getenv("YUEYUE_ROOT_DIR") or os.path.dirname(__file__))
PROJECT_CACHE_DIR = os.path.join(ROOT_DIR, "workspace", "project_cache")
SHORT_CONTEXT_FILE = os.path.join(PROJECT_CACHE_DIR, "short_context.json")
REFERENCE_MARKERS = [
    "这个",
    "這個",
    "刚刚",
    "剛剛",
    "那个",
    "那個",
    "你觉得",
    "你覺得",
    "她怎么",
    "她怎麼",
    "他怎么",
    "他怎麼",
    "怎么样",
    "怎麼樣",
    "这个视频",
    "這個影片",
]


@dataclass
class ShortContextTurn:
    chat_id: str
    text: str = ""
    urls: list[dict[str, Any]] = field(default_factory=list)
    media: list[dict[str, str]] = field(default_factory=list)
    mood: str = ""
    topic: str = ""
    assistant_summary: str = ""
    created_at: float = field(default_factory=time.time)


class ShortContextBuffer:
    def __init__(self, path: str = SHORT_CONTEXT_FILE, max_turns: int = 20):
        self.path = path
        self.max_turns = max_turns
        self.turns: dict[str, list[ShortContextTurn]] = {}
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            self.turns = {}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                raw = json.load(file)
            self.turns = {
                str(chat_id): [ShortContextTurn(**item) for item in items[-self.max_turns :] if isinstance(item, dict)]
                for chat_id, items in raw.items()
                if isinstance(items, list)
            }
        except Exception:
            self.turns = {}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as file:
            json.dump({chat_id: [asdict(item) for item in items[-self.max_turns :]] for chat_id, items in self.turns.items()}, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")

    def observe_turn(self, chat_id: int | str, text: str = "", media: list[dict[str, str]] | None = None, depth: str = "metadata") -> ShortContextTurn:
        chat_key = str(chat_id)
        entries = inspect_urls_for_text(text, depth=depth)
        turn = ShortContextTurn(
            chat_id=chat_key,
            text=(text or "")[:1200],
            urls=[entry.to_dict() for entry in entries],
            media=(media or [])[:8],
            mood=infer_mood(text, media or []),
            topic=infer_topic(text, entries, media or []),
        )
        self.turns.setdefault(chat_key, []).append(turn)
        self.turns[chat_key] = self.turns[chat_key][-self.max_turns :]
        self.save()
        emit_trace("short_context.turn_recorded", chat_id=chat_key, url_count=len(entries), media_count=len(media or []), mood=turn.mood, topic=turn.topic)
        return turn

    def update_last_assistant(self, chat_id: int | str, text: str) -> None:
        items = self.turns.get(str(chat_id)) or []
        if not items:
            return
        items[-1].assistant_summary = re.sub(r"\s+", " ", text or "").strip()[:500]
        self.save()

    def render_for_turn(self, chat_id: int | str, current_text: str = "", max_chars: int = 1600) -> str:
        items = self.turns.get(str(chat_id)) or []
        if not items:
            if has_reference_marker(current_text):
                return "短期上下文：主人用了「这个/刚刚那个」这类指代，但目前没有可靠的最近对象。请自然追问，不要乱猜。"
            return ""
        recent = items[-5:]
        lines = ["短期聊天上下文（只用来接住最近语境，不要写入长期记忆）："]
        candidate = latest_referent(items)
        if candidate and has_reference_marker(current_text):
            lines.append("可能指代：" + candidate)
        for turn in recent:
            parts = []
            if turn.topic:
                parts.append(f"topic={turn.topic}")
            if turn.mood:
                parts.append(f"mood={turn.mood}")
            if turn.text:
                parts.append("text=" + turn.text[:180])
            if turn.urls:
                url_entries = [_entry_from_dict(item) for item in turn.urls[:2]]
                parts.append(render_url_context([entry for entry in url_entries if entry], max_entries=2))
            if turn.media:
                parts.append("media=" + ", ".join(f"{item.get('kind', '')}:{item.get('media_type', '')}" for item in turn.media[:3]))
            if turn.assistant_summary:
                parts.append("last_reply=" + turn.assistant_summary[:160])
            if parts:
                lines.append("- " + " | ".join(part for part in parts if part))
        rendered = "\n".join(lines)
        return rendered[-max_chars:]


def build_context_for_turn(chat_id: int | str, text: str, media: list[dict[str, str]] | None = None) -> tuple[str, ShortContextTurn]:
    depth = "preview" if should_preview_url(text) else "metadata"
    before = DEFAULT_SHORT_CONTEXT_BUFFER.render_for_turn(chat_id, text)
    turn = DEFAULT_SHORT_CONTEXT_BUFFER.observe_turn(chat_id, text, media=media or [], depth=depth)
    current_url_context = render_url_context([_entry_from_dict(item) for item in turn.urls if isinstance(item, dict)])
    sections = [before]
    if current_url_context:
        sections.append("本轮 URL 理解：\n" + current_url_context)
    if has_reference_marker(text):
        sections.append(
            "回复策略：如果能绑定最近 URL/图片/话题，就自然接上；如果只看到标题、封面或页面，就明确说明限制；"
            "不要说客服腔的「请提供更多上下文」。平台视频/社交链接优先使用 inspect_url/read_url_context，不要改用 read_webpage。"
        )
    return "\n\n".join(section for section in sections if section).strip(), turn


def has_reference_marker(text: str) -> bool:
    lowered = (text or "").casefold()
    return any(marker.casefold() in lowered for marker in REFERENCE_MARKERS)


def latest_referent(items: list[ShortContextTurn]) -> str:
    for turn in reversed(items):
        if turn.urls:
            entry = _entry_from_dict(turn.urls[0])
            if entry:
                label = entry.title or entry.platform or entry.url
                return f"最近的 URL：{label} ({entry.platform}, {entry.status})"
        if turn.media:
            item = turn.media[0]
            return f"最近的媒体：{item.get('kind', 'media')} {item.get('caption', '')}".strip()
        if turn.topic:
            return "最近话题：" + turn.topic
    return ""


def infer_mood(text: str, media: list[dict[str, str]]) -> str:
    lowered = (text or "").casefold()
    if any(item.get("kind") == "sticker" for item in media):
        return "social/emotional"
    if any(marker in lowered for marker in ["好笑", "笑死", "哈哈", "抽象", "樂", "乐"]):
        return "amused"
    if any(marker in lowered for marker in ["无语", "無語", "离谱", "離譜"]):
        return "speechless"
    if any(marker in lowered for marker in ["怎么", "怎麼", "疑惑", "?"]):
        return "curious"
    return ""


def infer_topic(text: str, urls: list[URLContextEntry], media: list[dict[str, str]]) -> str:
    if urls:
        entry = urls[0]
        return (entry.title or f"{entry.platform} link" or "url")[:120]
    if media:
        return "media/sticker context"
    clean = re.sub(r"https?://\S+", "", text or "")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:120]


def _entry_from_dict(item: dict[str, Any]) -> URLContextEntry | None:
    try:
        return URLContextEntry(**{key: value for key, value in item.items() if key in URLContextEntry.__dataclass_fields__})
    except Exception:
        return None


DEFAULT_SHORT_CONTEXT_BUFFER = ShortContextBuffer()
