import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from agent_hooks import emit_trace
from agent_latency import InteractionMode, classify_interaction


ROOT_DIR = os.path.abspath(os.getenv("YUEYUE_ROOT_DIR") or os.path.dirname(__file__))
TURN_DEBOUNCE_ENV = "YUEYUE_TURN_DEBOUNCE_SECONDS"
DEFAULT_TURN_DEBOUNCE_SECONDS = 5.5


def _read_env_file_value(key: str) -> str:
    env_path = os.path.join(ROOT_DIR, ".env")
    if not os.path.exists(env_path):
        return ""
    try:
        with open(env_path, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                env_key, value = line.split("=", 1)
                if env_key.strip() == key:
                    return value.strip().strip('"').strip("'")
    except Exception as exc:
        emit_trace("turn.config_warning", key=key, error=str(exc))
    return ""


def configured_turn_debounce_seconds(default: float = DEFAULT_TURN_DEBOUNCE_SECONDS) -> float:
    raw = os.getenv(TURN_DEBOUNCE_ENV) or _read_env_file_value(TURN_DEBOUNCE_ENV)
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        emit_trace("turn.config_warning", key=TURN_DEBOUNCE_ENV, value=raw, fallback=default)
        return default
    if value <= 0:
        emit_trace("turn.config_warning", key=TURN_DEBOUNCE_ENV, value=raw, fallback=default)
        return default
    return value


@dataclass
class InboundMessagePart:
    chat_id: int | str
    message_id: int
    kind: str
    text: str = ""
    caption: str = ""
    path: str = ""
    media_type: str = ""
    media_kind: str = ""
    message: object | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class AggregatedTurn:
    chat_id: int | str
    primary_message_id: int
    reply_message: object
    primary_text: str = ""
    parts: list[InboundMessagePart] = field(default_factory=list)
    mode: InteractionMode = InteractionMode.CHAT

    @property
    def has_media(self) -> bool:
        return any(part.kind in {"photo", "sticker"} for part in self.parts)

    @property
    def stickers(self) -> list[InboundMessagePart]:
        return [part for part in self.parts if part.kind == "sticker"]

    @property
    def photos(self) -> list[InboundMessagePart]:
        return [part for part in self.parts if part.kind == "photo"]


class MessageCoalescer:
    def __init__(self, debounce_seconds: float | None = None):
        self.debounce_seconds = configured_turn_debounce_seconds() if debounce_seconds is None else debounce_seconds
        self._buffers: dict[int | str, list[InboundMessagePart]] = {}
        self._timers: dict[int | str, threading.Timer] = {}
        self._lock = threading.RLock()

    def add(self, part: InboundMessagePart, callback: Callable[[AggregatedTurn], None]) -> None:
        with self._lock:
            buffer = self._buffers.setdefault(part.chat_id, [])
            buffer.append(part)
            emit_trace(
                "turn.part",
                chat_id=part.chat_id,
                message_id=part.message_id,
                kind=part.kind,
                media_type=part.media_type,
                media_kind=part.media_kind,
                buffered_parts=len(buffer),
                debounce_seconds=self.debounce_seconds,
            )
            old_timer = self._timers.pop(part.chat_id, None)
            if old_timer:
                old_timer.cancel()
            timer = threading.Timer(self.debounce_seconds, self.flush_chat, args=(part.chat_id, callback))
            timer.daemon = True
            self._timers[part.chat_id] = timer
            timer.start()

    def flush_chat(self, chat_id: int | str, callback: Callable[[AggregatedTurn], None] | None = None) -> AggregatedTurn | None:
        with self._lock:
            timer = self._timers.pop(chat_id, None)
            if timer:
                timer.cancel()
            parts = self._buffers.pop(chat_id, [])
        if not parts:
            return None
        turn = build_aggregated_turn(parts)
        emit_trace(
            "turn.flush",
            chat_id=turn.chat_id,
            part_count=len(parts),
            primary_message_id=turn.primary_message_id,
            mode=turn.mode.value,
            kinds=[part.kind for part in turn.parts],
            has_primary_text=bool(turn.primary_text),
            debounce_seconds=self.debounce_seconds,
        )
        if callback:
            callback(turn)
        return turn


def build_aggregated_turn(parts: list[InboundMessagePart]) -> AggregatedTurn:
    ordered = sorted(parts, key=lambda part: (part.timestamp, part.message_id))
    text_parts = [part for part in ordered if part.kind == "text" and part.text.strip()]
    primary = text_parts[0] if text_parts else ordered[0]
    primary_text = "\n".join(part.text.strip() for part in text_parts if part.text.strip())
    media_parts = [part for part in ordered if part.kind in {"photo", "sticker"}]
    media_kind = _dominant_media_kind(media_parts)
    mode = classify_interaction(primary_text, has_media=bool(media_parts), media_kind=media_kind)
    if primary_text and mode == InteractionMode.SOCIAL_STICKER:
        mode = InteractionMode.CHAT
    return AggregatedTurn(
        chat_id=primary.chat_id,
        primary_message_id=primary.message_id,
        reply_message=primary.message,
        primary_text=primary_text,
        parts=ordered,
        mode=mode,
    )


def _dominant_media_kind(parts: list[InboundMessagePart]) -> str:
    if not parts:
        return ""
    if any(part.kind == "photo" for part in parts):
        return "photo"
    for part in parts:
        if part.media_kind:
            return part.media_kind
    return parts[0].kind


def build_turn_prompt(quote_context: str, turn: AggregatedTurn) -> str:
    lines = []
    if quote_context:
        lines.append(quote_context.rstrip())
    if turn.primary_text:
        lines.append("\u4e3b\u4eba\u4e3b\u8981\u8a0a\u606f\uff1a" + turn.primary_text)
    media_lines = []
    for part in turn.parts:
        if part.kind == "photo":
            caption = part.caption or "\u7121"
            media_lines.append(f"- photo: {part.path} ({part.media_type or 'unknown'}), caption={caption}")
        elif part.kind == "sticker":
            media_lines.append(f"- sticker: {part.path} ({part.media_type or 'unknown'})")
    if media_lines:
        lines.append("\u9644\u52a0\u5a92\u9ad4 / \u60c5\u7dd2\u8a0a\u865f\uff1a\n" + "\n".join(media_lines))
    if turn.mode == InteractionMode.VISION_TASK:
        lines.append(
            "\u4e3b\u4eba\u660e\u78ba\u8981\u6c42\u770b\u5716\u6216\u5206\u6790\u5a92\u9ad4\uff0c\u8acb\u5728\u9700\u8981\u6642\u4f7f\u7528 analyze_media\uff0c"
            "\u7136\u5f8c\u7528\u81ea\u7136\u7684\u8a9e\u6c23\u56de\u8986\u3002"
        )
    elif turn.primary_text and turn.has_media:
        lines.append(
            "\u9019\u4e9b sticker/photo \u662f\u4e3b\u4eba\u5728\u540c\u4e00\u53e5\u8a71\u88dc\u4e0a\u7684\u60c5\u7dd2\u6216\u8a9e\u6c23\u896f\u6258\u3002"
            "\u8acb\u4ee5\u6587\u5b57\u8a0a\u606f\u70ba\u4e3b\uff0c\u4e0d\u8981\u628a\u8868\u60c5\u5305\u7576\u6210\u65b0\u4efb\u52d9\uff0c"
            "\u4e5f\u4e0d\u8981\u4e3b\u52d5\u8abf\u7528 analyze_media\u3002"
        )
    elif turn.stickers:
        lines.append(
            "\u4e3b\u4eba\u50b3\u4f86 sticker\u3002\u5148\u628a\u5b83\u7576\u4f5c\u60c5\u7dd2\u6216\u793e\u4ea4\u8a0a\u865f\uff0c"
            "\u4e0d\u8981\u4e3b\u52d5\u8abf\u7528 analyze_media\uff0c\u81ea\u7136\u56de\u61c9\u5373\u53ef\u3002"
        )
    elif turn.photos:
        lines.append(
            "\u4e3b\u4eba\u50b3\u4f86 photo\uff1b\u5982\u679c\u6c92\u6709\u660e\u78ba\u8981\u6c42\u5206\u6790\uff0c"
            "\u5148\u628a\u5b83\u7576\u4f5c\u804a\u5929\u4e0a\u4e0b\u6587\uff0c\u4e0d\u8981\u4e3b\u52d5\u8abf\u7528 analyze_media\u3002"
        )
    return "\n".join(lines).strip()
