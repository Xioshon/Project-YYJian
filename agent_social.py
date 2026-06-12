import json
import os
import shutil
import time
import hashlib
from dataclasses import asdict, dataclass, field
from typing import Iterable


ROOT_DIR = os.path.abspath(os.getenv("YUEYUE_ROOT_DIR") or os.path.dirname(__file__))
ASSETS_DIR = os.path.join(ROOT_DIR, "workspace", "assets")
STICKERS_DIR = os.path.join(ASSETS_DIR, "stickers")
STICKERS_INDEX_FILE = os.path.join(ASSETS_DIR, "stickers_index.json")
SOCIAL_STICKER_INDEX_FILE = os.path.join(ASSETS_DIR, "social_sticker_index.json")
DEFAULT_SOCIAL_SESSION_TTL_SECONDS = 180.0
DEFAULT_CURATION_REMINDER_THRESHOLD = 3
DEFAULT_CURATION_REMINDER_COOLDOWN_SECONDS = 900.0

STICKER_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
MATURE_MARKERS = [
    "flirt",
    "fuck",
    "sex",
    "estrus",
    "\u6027",
    "\u8214",
    "\u725b\u725b",
    "\u8a98",
    "\u817f\u7167",
    "\u5617\u5617",
    "\u5c1d\u5c1d",
    "\u5473\u9053",
    "\u6253\u81a0",
    "\u6253\u80f6",
    "\u5c04",
    "\u5634\u88cf",
    "\u5634\u91cc",
]

EMOTION_KEYWORDS = {
    "happy": ["laugh", "haha", "\u54c8", "\u563f", "\u958b\u5fc3", "\u7b11"],
    "confused": ["confuse", "question", "\u554f\u865f", "\u4e0d\u61c2", "\u597d\u5947"],
    "angry": ["angry", "angr", "\u751f\u6c23", "\u54c8\u6c23", "\u6253\u6211"],
    "cute": ["cute", "meow", "\u8ce3\u840c", "\u6492\u5b0c", "\u55b5"],
    "cry": ["sob", "\u55da", "\u54ed", "\u6295\u964d"],
    "battle": ["pointing", "\u6307", "\u4f60", "\u53bb\u4e16", "\u5632", "\u9b25\u5716"],
    "agree": ["yes", "nod", "\u5c0d", "\u55ef"],
    "affection": ["love", "heart", "\u611b\u5fc3", "\u5bb3\u7f9e", "\u8cbc\u8cbc", "\u6492\u5b0c", "\u5fc3\u52d5", "\u60f3\u4f60", "\u62b1\u62b1"],
    "teasing": ["tease", "\u5403\u918b", "\u5634\u786c", "\u64a9", "\u50b2\u5b0c", "\u54fc"],
}

EMOJI_TAGS = {
    "\U0001f602": ["happy"],
    "\U0001f923": ["happy"],
    "\U0001f604": ["happy"],
    "\U0001f60d": ["affection"],
    "\U0001f970": ["affection", "cute"],
    "\U0001f618": ["affection"],
    "\u2764": ["affection"],
    "\U0001f495": ["affection"],
    "\U0001f914": ["confused"],
    "\U0001f928": ["confused"],
    "\U0001f622": ["cry"],
    "\U0001f62d": ["cry"],
    "\U0001f620": ["angry"],
    "\U0001f621": ["angry"],
    "\U0001f624": ["angry", "teasing"],
}


@dataclass
class SocialStickerEntry:
    filename: str
    tags: list[str] = field(default_factory=list)
    source: str = "local"
    original_path: str = ""
    uses: int = 0
    safe_for_minor: bool = True
    approved_for_autouse: bool = True
    rejected: bool = False
    added_at: float = field(default_factory=time.time)
    file_unique_id: str = ""
    emoji: str = ""
    set_name: str = ""
    media_type: str = ""
    content_hash: str = ""


@dataclass
class SocialSessionState:
    chat_id: str
    mode: str = "idle"
    intent_tags: list[str] = field(default_factory=list)
    last_owner_text: str = ""
    last_owner_had_sticker: bool = False
    recent_sent: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)
    turn_count: int = 0

    def is_active(self, now: float | None = None, ttl_seconds: float = DEFAULT_SOCIAL_SESSION_TTL_SECONDS) -> bool:
        current = time.time() if now is None else now
        return self.mode != "idle" and current - self.updated_at <= ttl_seconds


@dataclass
class SocialReplyPolicy:
    mode: str
    max_sentences: int
    tone: str
    should_attach_sticker: bool = False
    allow_tools: bool = False
    instruction: str = ""

    def to_prompt_note(self) -> str:
        lines = [
            "### Social reply policy",
            f"- rhythm: {self.mode}",
            f"- tone: {self.tone}",
            f"- max sentences: {self.max_sentences}",
        ]
        if self.should_attach_sticker:
            lines.append("- one fitting local sticker is encouraged.")
        if not self.allow_tools:
            lines.append("- do not use tools for this social reply.")
        if self.instruction:
            lines.append(f"- style: {self.instruction}")
        return "\n".join(lines)


class SocialSessionManager:
    def __init__(self, ttl_seconds: float = DEFAULT_SOCIAL_SESSION_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self.sessions: dict[str, SocialSessionState] = {}

    def observe_turn(
        self,
        chat_id: int | str,
        text: str = "",
        has_sticker: bool = False,
        has_photo: bool = False,
        mode: str = "",
        now: float | None = None,
    ) -> SocialSessionState:
        key = str(chat_id)
        current = time.time() if now is None else now
        state = self.sessions.get(key)
        if not state or not state.is_active(current, self.ttl_seconds):
            state = SocialSessionState(chat_id=key)
        intent_tags = infer_intent_tags(text)
        social_mode = infer_social_mode(text=text, has_sticker=has_sticker, has_photo=has_photo, turn_mode=mode)
        if social_mode != "idle":
            state.mode = social_mode
        elif state.is_active(current, self.ttl_seconds) and has_sticker:
            state.mode = state.mode if state.mode != "idle" else "sticker_battle"
        else:
            state.mode = "idle"
        state.intent_tags = sorted(set(state.intent_tags + intent_tags))[-6:]
        state.last_owner_text = text or state.last_owner_text
        state.last_owner_had_sticker = has_sticker
        state.updated_at = current
        state.turn_count += 1
        self.sessions[key] = state
        _emit_trace(
            "social_session.observed",
            chat_id=key,
            mode=state.mode,
            tags=state.intent_tags,
            has_sticker=has_sticker,
            has_photo=has_photo,
            turn_count=state.turn_count,
        )
        return state

    def mark_sticker_sent(self, chat_id: int | str, filename: str) -> None:
        key = str(chat_id)
        state = self.sessions.get(key)
        if not state:
            state = SocialSessionState(chat_id=key)
        base = os.path.basename(filename or "")
        if base:
            state.recent_sent = ([base] + [item for item in state.recent_sent if item != base])[:8]
            state.updated_at = time.time()
            self.sessions[key] = state
            _emit_trace("social_session.sticker_sent", chat_id=key, filename=base)

    def suggest_stickers(self, chat_id: int | str, index: "SocialStickerIndex", text: str = "", limit: int = 4) -> list[str]:
        state = self.sessions.get(str(chat_id))
        if not state or not state.is_active(ttl_seconds=self.ttl_seconds):
            return []
        intent = " ".join(sorted(set(state.intent_tags + infer_intent_tags(text) + _mode_tags(state.mode))))
        candidates = index.choose(intent or state.mode, limit=max(limit + len(state.recent_sent), limit))
        suggestions = [name for name in candidates if name not in set(state.recent_sent)]
        return suggestions[:limit]

    def build_prompt_note(self, chat_id: int | str, suggestions: Iterable[str] | None = None) -> str:
        state = self.sessions.get(str(chat_id))
        if not state or not state.is_active(ttl_seconds=self.ttl_seconds):
            return ""
        policy = social_reply_policy_for(state.mode, state.intent_tags, has_sticker=state.last_owner_had_sticker)
        if state.mode == "sticker_battle":
            mood = "The owner is likely playing a sticker-battle / meme-reply rhythm."
        elif state.mode == "affection":
            mood = "The owner is in a soft affectionate rhythm."
        elif state.mode == "teasing":
            mood = "The owner is in a teasing banter rhythm."
        else:
            mood = "The owner is sending a light social signal."
        lines = [
            "### Social session",
            mood,
            "Treat stickers as emotional timing, not as separate tasks.",
            "Reply briefly and naturally; adding one local sticker is welcome when it fits.",
            policy.to_prompt_note(),
        ]
        sticker_list = [os.path.basename(item) for item in (suggestions or []) if item]
        if sticker_list:
            lines.append("Good local sticker candidates: " + ", ".join(sticker_list))
        return "\n".join(lines)


@dataclass
class SocialCurationReminderState:
    last_count: int = 0
    last_reminded_at: float = 0.0


class SocialCurationReminder:
    def __init__(
        self,
        threshold: int = DEFAULT_CURATION_REMINDER_THRESHOLD,
        cooldown_seconds: float = DEFAULT_CURATION_REMINDER_COOLDOWN_SECONDS,
    ):
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.states: dict[str, SocialCurationReminderState] = {}

    def should_remind(self, chat_id: int | str, pending_count: int, now: float | None = None) -> bool:
        if pending_count < self.threshold:
            return False
        key = str(chat_id)
        current = time.time() if now is None else now
        state = self.states.get(key) or SocialCurationReminderState()
        count_changed = pending_count > state.last_count
        cooldown_ready = current - state.last_reminded_at >= self.cooldown_seconds
        if state.last_reminded_at == 0.0 or (count_changed and cooldown_ready):
            state.last_count = pending_count
            state.last_reminded_at = current
            self.states[key] = state
            _emit_trace("social_sticker.curation_reminder", chat_id=key, pending_count=pending_count)
            return True
        state.last_count = max(state.last_count, pending_count)
        self.states[key] = state
        return False

    def message(self, pending_count: int) -> str:
        return (
            f"我先把 {pending_count} 個新表情包存成候選啦。"
            "想讓我以後自己用，可以回：`approve recent 3 stickers cute`；不想要就回：`reject recent 3 stickers`。"
        )


class SocialStickerIndex:
    def __init__(self, path: str = SOCIAL_STICKER_INDEX_FILE, sticker_dir: str = STICKERS_DIR):
        self.path = path
        self.sticker_dir = sticker_dir
        self.entries: dict[str, SocialStickerEntry] = {}
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            self.entries = {}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
            self.entries = {}
            changed = False
            for name, value in data.items():
                entry = SocialStickerEntry(**{key: item for key, item in value.items() if key in SocialStickerEntry.__dataclass_fields__})
                entry.safe_for_minor = is_safe_sticker(name)
                if entry.source == "incoming":
                    entry.approved_for_autouse = False
                if not entry.safe_for_minor:
                    entry.approved_for_autouse = False
                    entry.tags = sorted(set(entry.tags + ["restricted"]))
                self.entries[name] = entry
                changed = changed or value.get("safe_for_minor") != entry.safe_for_minor or value.get("approved_for_autouse") != entry.approved_for_autouse
            if changed:
                self.save()
        except Exception as exc:
            _emit_trace("social_sticker.index_load_failed", error=str(exc))
            self.entries = {}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as file:
            json.dump({name: asdict(entry) for name, entry in self.entries.items()}, file, ensure_ascii=False, indent=2)

    def rebuild_from_files(self) -> int:
        if not os.path.isdir(self.sticker_dir):
            self.entries = {}
            self.save()
            return 0
        count = 0
        for filename in sorted(os.listdir(self.sticker_dir)):
            if not filename.lower().endswith(STICKER_EXTENSIONS):
                continue
            if not is_safe_sticker(filename):
                continue
            tags = infer_sticker_tags(filename)
            if not tags:
                continue
            existing = self.entries.get(filename)
            uses = existing.uses if existing else 0
            self.entries[filename] = SocialStickerEntry(filename=filename, tags=tags, source="local", uses=uses, safe_for_minor=True, approved_for_autouse=True, original_path=os.path.join(self.sticker_dir, filename))
            count += 1
        self.save()
        _emit_trace("social_sticker.index_rebuilt", count=count)
        return count

    def catalog_incoming(self, file_path: str, media_type: str = "", tags: list[str] | None = None, metadata: dict | None = None) -> SocialStickerEntry:
        filename = os.path.basename(file_path)
        metadata = metadata or {}
        content_hash = metadata.get("content_hash") or file_hash(file_path)
        unique_id = str(metadata.get("file_unique_id") or "")
        entry_key = self._find_existing_key(filename=filename, file_unique_id=unique_id, content_hash=content_hash)
        tags = tags or []
        inferred_tags = infer_sticker_tags(filename) + infer_metadata_tags(metadata)
        merged_tags = sorted(set(tags + inferred_tags + ["incoming"]))
        safe = is_safe_sticker(" ".join([filename, str(metadata.get("set_name") or ""), str(metadata.get("emoji") or "")]))
        entry = self.entries.get(entry_key or filename) or SocialStickerEntry(filename=filename, source="incoming", safe_for_minor=safe, approved_for_autouse=False, original_path=file_path)
        entry.tags = sorted(set(entry.tags + merged_tags))
        if entry.source not in {"approved_incoming", "local"}:
            entry.source = "incoming"
        entry.original_path = file_path
        entry.safe_for_minor = safe
        entry.media_type = media_type
        entry.file_unique_id = unique_id or entry.file_unique_id
        entry.emoji = str(metadata.get("emoji") or entry.emoji or "")
        entry.set_name = str(metadata.get("set_name") or entry.set_name or "")
        entry.content_hash = content_hash or entry.content_hash
        if entry.source == "incoming":
            entry.approved_for_autouse = False
        if not safe:
            entry.rejected = True
            entry.tags = sorted(set(entry.tags + ["restricted"]))
        key = entry_key or filename
        self.entries[key] = entry
        self.save()
        _emit_trace(
            "social_sticker.cataloged",
            filename=entry.filename,
            media_type=media_type,
            tags=entry.tags,
            duplicate=bool(entry_key),
            file_unique_id=entry.file_unique_id,
            emoji=entry.emoji,
            set_name=entry.set_name,
        )
        return entry

    def list_candidates(self, limit: int = 20) -> list[SocialStickerEntry]:
        candidates = [
            entry
            for entry in self.entries.values()
            if entry.source == "incoming" and entry.safe_for_minor and not entry.approved_for_autouse and not entry.rejected
        ]
        candidates.sort(key=lambda entry: entry.added_at, reverse=True)
        return candidates[:limit]

    def latest_candidate(self) -> SocialStickerEntry | None:
        candidates = self.list_candidates(limit=1)
        return candidates[0] if candidates else None

    def summarize_candidates(self, limit: int = 10) -> list[str]:
        lines: list[str] = []
        for index, entry in enumerate(self.list_candidates(limit=limit), start=1):
            details = [f"tags={','.join(entry.tags) or '-'}"]
            if entry.emoji:
                details.append(f"emoji={entry.emoji}")
            if entry.set_name:
                details.append(f"set={entry.set_name}")
            if entry.file_unique_id:
                details.append(f"id={entry.file_unique_id[:10]}")
            lines.append(f"{index}. {entry.filename} ({'; '.join(details)})")
        return lines

    def approve_recent_candidates(self, count: int, tags: list[str] | None = None) -> list[SocialStickerEntry]:
        approved: list[SocialStickerEntry] = []
        for entry in list(self.list_candidates(limit=max(0, count))):
            approved.append(self.approve_candidate(entry.filename, tags=tags))
        _emit_trace("social_sticker.batch_approved", count=len(approved), tags=tags or [])
        return approved

    def reject_recent_candidates(self, count: int, reason: str = "") -> list[SocialStickerEntry]:
        rejected: list[SocialStickerEntry] = []
        for entry in list(self.list_candidates(limit=max(0, count))):
            rejected.append(self.reject_candidate(entry.filename, reason=reason))
        _emit_trace("social_sticker.batch_rejected", count=len(rejected), reason=reason)
        return rejected

    def approve_candidate(self, filename: str, tags: list[str] | None = None) -> SocialStickerEntry:
        base = os.path.basename((filename or "").strip())
        entry = self.entries.get(base)
        if not entry:
            raise ValueError(f"Sticker candidate not found: {base}")
        if not entry.safe_for_minor or not is_safe_sticker(base):
            entry.rejected = True
            entry.approved_for_autouse = False
            entry.tags = sorted(set(entry.tags + ["restricted"]))
            self.entries[base] = entry
            self.save()
            raise ValueError("Sticker candidate is not safe for automatic use.")
        source_path = entry.original_path
        if not source_path or not os.path.exists(source_path):
            raise ValueError(f"Sticker source file not found: {source_path}")
        os.makedirs(self.sticker_dir, exist_ok=True)
        target_name = _unique_filename(self.sticker_dir, base)
        target_path = os.path.join(self.sticker_dir, target_name)
        if os.path.abspath(source_path) != os.path.abspath(target_path):
            shutil.copy2(source_path, target_path)
        entry.filename = target_name
        entry.source = "approved_incoming"
        entry.original_path = target_path
        entry.tags = sorted(set((tags or []) + entry.tags + infer_sticker_tags(target_name)))
        entry.safe_for_minor = True
        entry.approved_for_autouse = True
        entry.rejected = False
        self.entries.pop(base, None)
        self.entries[target_name] = entry
        self.save()
        _emit_trace("social_sticker.approved", filename=target_name, tags=entry.tags)
        return entry

    def reject_candidate(self, filename: str, reason: str = "") -> SocialStickerEntry:
        base = os.path.basename((filename or "").strip())
        entry = self.entries.get(base)
        if not entry:
            raise ValueError(f"Sticker candidate not found: {base}")
        entry.rejected = True
        entry.approved_for_autouse = False
        self.entries[base] = entry
        self.save()
        _emit_trace("social_sticker.rejected", filename=base, reason=reason)
        return entry

    def choose(self, intent: str, limit: int = 3) -> list[str]:
        if not self.entries:
            self.rebuild_from_files()
        wanted = set(infer_intent_tags(intent))
        keyword = (intent or "").casefold()
        scored: list[tuple[int, str]] = []
        for filename, entry in self.entries.items():
            if not entry.safe_for_minor or not entry.approved_for_autouse or entry.rejected:
                continue
            score = 0
            if keyword and keyword in filename.casefold():
                score += 4
            score += 3 * len(wanted.intersection(entry.tags))
            if keyword and keyword in " ".join(entry.tags).casefold():
                score += 2
            if score:
                scored.append((score - min(entry.uses, 5), filename))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [filename for _, filename in scored[:limit]]

    def mark_used(self, filename: str) -> None:
        entry = self.entries.get(filename)
        if not entry:
            return
        entry.uses += 1
        self.entries[filename] = entry
        self.save()

    def _find_existing_key(self, filename: str, file_unique_id: str = "", content_hash: str = "") -> str:
        if filename in self.entries:
            return filename
        for key, entry in self.entries.items():
            if file_unique_id and entry.file_unique_id == file_unique_id:
                return key
            if content_hash and entry.content_hash == content_hash:
                return key
        return ""


def infer_sticker_tags(filename: str) -> list[str]:
    if not is_safe_sticker(filename):
        return ["restricted"]
    normalized = (filename or "").casefold()
    tags = []
    for tag, markers in EMOTION_KEYWORDS.items():
        if any(marker.casefold() in normalized for marker in markers):
            tags.append(tag)
    return sorted(set(tags))


def infer_metadata_tags(metadata: dict | None) -> list[str]:
    metadata = metadata or {}
    tags: list[str] = []
    emoji = str(metadata.get("emoji") or "")
    for marker, marker_tags in EMOJI_TAGS.items():
        if marker in emoji:
            tags.extend(marker_tags)
    set_name = str(metadata.get("set_name") or "")
    if set_name:
        tags.extend(infer_sticker_tags(set_name))
    return sorted(set(tags))


def infer_intent_tags(text: str) -> list[str]:
    normalized = (text or "").casefold()
    tags = []
    for tag, markers in EMOTION_KEYWORDS.items():
        if tag in normalized or any(marker.casefold() in normalized for marker in markers):
            tags.append(tag)
    if "\u9b25\u5716" in normalized or "battle" in normalized:
        tags.append("battle")
    return sorted(set(tags))


def infer_social_mode(text: str = "", has_sticker: bool = False, has_photo: bool = False, turn_mode: str = "") -> str:
    normalized = (text or "").casefold()
    tags = set(infer_intent_tags(text))
    if "battle" in tags or "\u9b25\u5716" in normalized or "\u8868\u60c5\u5305" in normalized and has_sticker:
        return "sticker_battle"
    if tags.intersection({"affection", "cute"}):
        return "affection"
    if tags.intersection({"teasing", "angry"}) and has_sticker:
        return "teasing"
    if has_sticker and not text:
        return "sticker_battle"
    if turn_mode == "social_sticker":
        return "sticker_battle"
    return "idle"


def social_reply_policy_for(mode: str, tags: list[str] | None = None, has_sticker: bool = False) -> SocialReplyPolicy:
    tag_set = set(tags or [])
    if mode == "sticker_battle":
        return SocialReplyPolicy(
            mode=mode,
            max_sentences=2,
            tone="playful, quick, lightly competitive",
            should_attach_sticker=True,
            allow_tools=False,
            instruction="banter back like a live chat; do not explain the sticker.",
        )
    if mode == "affection":
        return SocialReplyPolicy(
            mode=mode,
            max_sentences=2,
            tone="warm, clingy, soft, a little shy",
            should_attach_sticker=has_sticker or bool(tag_set.intersection({"cute", "affection"})),
            allow_tools=False,
            instruction="answer the feeling directly; keep it intimate but gentle.",
        )
    if mode == "teasing":
        return SocialReplyPolicy(
            mode=mode,
            max_sentences=2,
            tone="teasing, smug, affectionate",
            should_attach_sticker=has_sticker,
            allow_tools=False,
            instruction="push back playfully without becoming mean.",
        )
    return SocialReplyPolicy(
        mode=mode or "idle",
        max_sentences=3,
        tone="natural and concise",
        should_attach_sticker=False,
        allow_tools=False,
        instruction="reply like normal chat.",
    )


def _mode_tags(mode: str) -> list[str]:
    if mode == "sticker_battle":
        return ["battle", "teasing", "happy"]
    if mode == "affection":
        return ["affection", "cute"]
    if mode == "teasing":
        return ["teasing", "battle"]
    return []


def is_safe_sticker(filename: str) -> bool:
    normalized = (filename or "").casefold()
    return not any(marker.casefold() in normalized for marker in MATURE_MARKERS)


def _unique_filename(directory: str, filename: str) -> str:
    base, ext = os.path.splitext(os.path.basename(filename))
    candidate = filename
    index = 1
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{base}_{index}{ext}"
        index += 1
    return candidate


def file_hash(path: str) -> str:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return ""


def _emit_trace(event: str, **data) -> None:
    try:
        from agent_hooks import emit_trace

        emit_trace(event, **data)
    except Exception:
        pass


DEFAULT_SOCIAL_STICKER_INDEX = SocialStickerIndex()
DEFAULT_SOCIAL_SESSION_MANAGER = SocialSessionManager()
DEFAULT_SOCIAL_CURATION_REMINDER = SocialCurationReminder()
