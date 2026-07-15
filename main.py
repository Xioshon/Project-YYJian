import argparse
import contextlib
import datetime
import html
import json
import os
import random
import re
import sys
import threading
import time

import telebot

from agent_hooks import emit_trace
from agent_latency import (
    DEFAULT_MEDIA_CACHE,
    InteractionMode,
    media_type_for,
    quick_ack_for,
    response_policy_for,
)
from agent_presence import DEFAULT_PRESENCE_ENGINE, LOW_MOOD_MARKERS
from agent_protocol import (
    STICKER_MARKER_LABEL,
    screenshot_pattern,
    sticker_pattern,
)
from agent_short_context import DEFAULT_SHORT_CONTEXT_BUFFER, build_context_for_turn
from agent_social import (
    DEFAULT_SOCIAL_CURATION_REMINDER,
    DEFAULT_SOCIAL_SESSION_MANAGER,
    DEFAULT_SOCIAL_STICKER_INDEX,
    social_reply_policy_for,
)
from agent_turns import InboundMessagePart, MessageCoalescer, build_turn_prompt
from agent_watchdog import GatewayWatchdog, read_chat_id_file
from chat_text_sanitizers import is_benign_testing_note
from core_tools import (
    ALL_TOOLS,
    API_KEY,
    CHAT_ID_FILE,
    MEMORY_FILE,
    PERSONALITY_FILE,
    PROFILE_FILE,
    PROJECT_CACHE_DIR,
    RULES_FILE,
    TASK_PLAN_FILE,
    TG_IMAGES_DIR,
    TG_TOKEN,
    WORKSPACE_DIR,
    env_value,
    find_sticker_file,
    set_telegram_context,
)
from intent_router import classify_owner_intent
from reply_context import reply_summary_for_context
from response_composer import compose_fast_reply, is_plain_greeting, is_simple_wake_greeting
from sticker_assets import direct_sticker_reply
from sticker_flow import direct_sticker_resend_reply, is_sticker_resend
from telegram_input import owner_text_from_message, repair_mojibake
from telegram_outbox import mark_send_job_failed, mark_send_job_sent, start_send_job
from temporal_context import attach_temporal_context, build_temporal_snapshot, build_time_query_reply
from yueyue_v3.rendering import RenderLedger
from yueyue_v3.runtime import YueYueRuntimeV3

for stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        stream.reconfigure(encoding="utf-8", errors="replace")

TELEGRAM_MESSAGE_LIMIT = 4096


def _read_text_file(path: str, default: str = "") -> str:
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as file:
            return file.read().strip()
    except Exception as exc:
        return f"[read failed: {exc}]"


def _read_json_file(path: str) -> dict:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {}
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except Exception as exc:
        return {"read_failed": str(exc)}


def _split_sticker_command_payload(payload: str) -> tuple[str, list[str]]:
    payload = (payload or "").strip()
    if not payload:
        return "", []
    if payload[0] in {'"', "'"}:
        quote = payload[0]
        end = payload.find(quote, 1)
        if end > 0:
            filename = payload[1:end].strip()
            rest = payload[end + 1 :].strip()
            return filename, [tag for tag in re.split(r"[\s,，]+", rest) if tag]
    parts = [part for part in re.split(r"\s+", payload, maxsplit=1) if part]
    filename = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    return filename, [tag for tag in re.split(r"[\s,，]+", rest) if tag]


def _parse_recent_count(value: str, default: int = 3, maximum: int = 20) -> int:
    value = (value or "").strip().casefold()
    if value in {"all", "全部", "所有"}:
        return maximum
    match = re.search(r"\d+", value)
    if not match:
        return default
    return max(1, min(maximum, int(match.group(0))))


def check_health() -> dict:
    return {
        "api": "ok" if API_KEY and len(API_KEY) > 10 else "missing",
        "telegram": "ok" if TG_TOKEN and ":" in TG_TOKEN else "missing",
        "personality": "ok" if os.path.exists(PERSONALITY_FILE) else "missing",
        "rules": "ok" if os.path.exists(RULES_FILE) else "missing",
        "profile": "ok" if os.path.exists(PROFILE_FILE) else "empty",
        "memory": "ok" if os.path.exists(MEMORY_FILE) else "empty",
        "planner": "ok" if os.path.exists(TASK_PLAN_FILE) else "not started",
    }


def print_gateway_dashboard() -> None:
    os.system("cls" if os.name == "nt" else "clear")
    health = check_health()
    print("=" * 60)
    print(" YueYue Agent - stabilized runtime")
    print("=" * 60)
    print(f" Workspace : {WORKSPACE_DIR}")
    print(f" API       : {health['api']}")
    print(f" Telegram  : {health['telegram']}")
    print(f" Tools     : {len(ALL_TOOLS)}")
    print("-" * 60)
    print(" [1] Terminal chat")
    print(" [2] Telegram bot")
    print(" [0] Exit")
    print("=" * 60)


def _coerce_benign_testing_mode(owner_text: str, mode: InteractionMode) -> InteractionMode:
    if is_benign_testing_note(owner_text):
        return InteractionMode.CHAT
    return mode


def _should_allow_auto_sticker(
    owner_text: str,
    *,
    has_sticker: bool,
    has_photo: bool,
    mode_value: str,
    suggested_stickers: list[str] | None = None,
) -> bool:
    if not suggested_stickers:
        return False
    if has_sticker:
        return True
    if has_photo:
        return False
    if str(mode_value or "").casefold() == InteractionMode.SOCIAL_STICKER.value:
        return True

    text = (owner_text or "").casefold()
    sticker_markers = [
        "表情包", "貼圖", "贴图", "sticker", "鬥圖", "斗图", "meme",
    ]
    return any(marker in text for marker in sticker_markers)



FAKE_STICKER_CLAIM_RE = re.compile(
    r"[（(]\s*(?:發送|发送|送出|sent|send|貼了|贴了|發了|发了).*?(?:表情包|貼圖|贴图|sticker).*?[)）]\s*",
    re.IGNORECASE,
)

def _strip_fake_sticker_claims(text: str) -> str:
    cleaned = FAKE_STICKER_CLAIM_RE.sub("", text or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned

def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        key = str(item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(key)
    return output


_PUNCTUATION_ONLY_RE = re.compile(r"^[\s。！？!?~～….,，、\-]*$")

# Modes where the turn is task/tool execution, not chat/social - used to keep the short-context
# mood/topic note (which is written for casual continuity) from reading like an invitation to be
# uncertain or casual once the turn is actually about getting something done.
_TASK_ORIENTED_MODES = frozenset({InteractionMode.VISION_TASK, InteractionMode.TOOL_TASK, InteractionMode.SCREEN_OBSERVE})


def _merge_contentless_fragments(parts: list[str]) -> list[str]:
    """Fold any split fragment that has no real text (pure punctuation/ellipsis) into
    the previous fragment, so a multi-bubble reply never sends a stray content-less message."""
    merged: list[str] = []
    for part in parts:
        if merged and _PUNCTUATION_ONLY_RE.match(part):
            merged[-1] = (merged[-1] + part).strip()
        else:
            merged.append(part)
    return merged


def _human_typing_delay(part: str) -> float:
    """Scale the pre-send pause with message length, with natural jitter so a multi-bubble
    reply does not read like a metronome."""
    base = max(0.2, min(1.5, len(part) * 0.04))
    return base * random.uniform(0.75, 1.35)


_BUBBLE_SOFT_LIMIT = 60
_MAX_BUBBLES = 4
_AFTERTHOUGHT_PREFIXES = ("不過", "不过", "對了", "对了", "欸", "诶", "還有", "还有", "話說", "话说", "啊對", "啊对")


def _split_reply_bubbles(text: str) -> list[str]:
    """Newlines are the model's own bubble breaks; sentence-splitting only kicks in for an
    overlong run. This lets the model deliberately keep two sentences in one bubble or hold
    a short line for its own bubble - which is how real chat rhythm works."""
    chunks = [chunk.strip() for chunk in re.split(r"\n+", str(text or "")) if chunk.strip()]
    parts: list[str] = []
    for chunk in chunks:
        if len(chunk) <= _BUBBLE_SOFT_LIMIT:
            parts.append(chunk)
            continue
        sentences = [item.strip() for item in re.split(r"(?<=[。！？!?~～…])\s*", chunk) if item.strip()]
        group = ""
        for sentence in sentences:
            if group and len(group) + len(sentence) > _BUBBLE_SOFT_LIMIT:
                parts.append(group)
                group = sentence
            else:
                group = f"{group}{sentence}" if group else sentence
        if group:
            parts.append(group)
    parts = _merge_contentless_fragments(parts)
    if len(parts) > _MAX_BUBBLES:
        parts = parts[: _MAX_BUBBLES - 1] + ["".join(parts[_MAX_BUBBLES - 1 :])]
    return [item for item in (_drop_bubble_trailing_full_stop(part) for part in parts) if item]


def _afterthought_hold(part: str, index: int, total: int) -> float:
    """A short trailing afterthought bubble (不過…/對了…) lands a beat later, like a person
    who already hit send and then adds one more line."""
    if total >= 2 and index == total - 1 and len(part) <= 20 and part.startswith(_AFTERTHOUGHT_PREFIXES):
        return random.uniform(1.5, 3.5)
    return 0.0


def _drop_bubble_trailing_full_stop(text: str) -> str:
    """Each bubble is its own visible Telegram message, so a plain trailing full stop (。)
    reads as written/formal even if it is only the end of one bubble, not the whole reply."""
    value = str(text or "").rstrip()
    if value.endswith("。") and not value.endswith(("！。", "？。", "~。", "～。")):
        value = value[:-1].rstrip()
    return value


# CHAT/SOCIAL-only, contextual "not glued to the phone" reply pacing. Only the general chat/social
# send path passes a real `mode` and
# `owner_text` into send_reply_with_stickers, so this cannot fire for task-oriented replies.
#
# Mandatory safety valve: _should_skip_reply_pacing() is checked before anything else runs, and
# any urgent/help-seeking or low-mood signal in the owner's own message always wins - even if the
# turn otherwise classified as CHAT, even during late night, even mid-rapid-exchange. See
# tests_v3/test_reply_pacing.py for a dedicated, independent test of this valve.
_URGENT_SIGNAL_MARKERS = (
    "救命",
    "緊急",
    "紧急",
    "sos",
    "emergency",
    "urgent",
    "出事了",
    "出事",
    "怎麼辦",
    "怎么办",
    "快幫我",
    "快帮我",
    "快來",
    "快来",
    "help me",
    "help!",
)
_REPLY_PACING_LATE_NIGHT_HOURS = frozenset(range(0, 6))
_REPLY_PACING_RAPID_TURN_WINDOW_SECONDS = 600
_REPLY_PACING_RAPID_TURN_COUNT = 4
_REPLY_PACING_PROBABILITY_ENV = "YUEYUE_REPLY_PACING_PROBABILITY"
_DEFAULT_REPLY_PACING_PROBABILITY = 0.15
_REPLY_PACING_DELAY_RANGE_SECONDS = (8.0, 40.0)


def _should_skip_reply_pacing(owner_text: str) -> bool:
    """Mandatory safety valve: urgent/help-seeking or low-mood signals always get an instant
    reply. Checked independently of mode - a CHAT-routed turn with these signals still skips
    pacing even though it never reached TASK mode."""
    lowered = str(owner_text or "").casefold()
    return any(marker.casefold() in lowered for marker in _URGENT_SIGNAL_MARKERS) or any(
        marker.casefold() in lowered for marker in LOW_MOOD_MARKERS
    )


def _reply_pacing_probability() -> float:
    raw = os.getenv(_REPLY_PACING_PROBABILITY_ENV)
    if raw is None:
        return _DEFAULT_REPLY_PACING_PROBABILITY
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_REPLY_PACING_PROBABILITY
    return max(0.0, min(0.6, value))


def _recent_turn_count(chat_id: int | str, now: float, window_seconds: float) -> int:
    turns = DEFAULT_SHORT_CONTEXT_BUFFER.turns.get(str(chat_id)) or []
    return sum(1 for turn in turns if now - float(getattr(turn, "created_at", 0.0) or 0.0) <= window_seconds)


def reply_pacing_delay_seconds(
    chat_id: int | str,
    owner_text: str,
    mode: "InteractionMode | None",
    now: float | None = None,
) -> float:
    """Occasionally hold the whole reply for a while before the first typing indicator even
    shows, so YueYue does not read as glued to the phone. Only ever considered for CHAT/SOCIAL
    turns, only when context suggests she plausibly would not answer instantly (late night, or
    several rapid-fire turns just now), and always 0 when _should_skip_reply_pacing() is True."""
    if mode not in {InteractionMode.CHAT, InteractionMode.SOCIAL_STICKER}:
        return 0.0
    if _should_skip_reply_pacing(owner_text):
        return 0.0
    now = time.time() if now is None else now
    late_night = datetime.datetime.fromtimestamp(now).hour in _REPLY_PACING_LATE_NIGHT_HOURS
    rapid_turns = _recent_turn_count(chat_id, now, _REPLY_PACING_RAPID_TURN_WINDOW_SECONDS) >= _REPLY_PACING_RAPID_TURN_COUNT
    if not (late_night or rapid_turns):
        return 0.0
    if random.random() >= _reply_pacing_probability():
        return 0.0
    return random.uniform(*_REPLY_PACING_DELAY_RANGE_SECONDS)


def _record_render_dedupe(chat_id: int | str, kind: str, original_count: int, rendered_count: int) -> None:
    if original_count <= rendered_count:
        return
    path = os.path.join(PROJECT_CACHE_DIR, "render_dedupe.jsonl")
    payload = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chat_id": str(chat_id),
        "kind": kind,
        "original_count": original_count,
        "rendered_count": rendered_count,
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _record_temporal_context_debug(chat_id: int | str, message_id: int | str, snapshot: dict) -> None:
    path = os.path.join(PROJECT_CACHE_DIR, "temporal_context_debug.jsonl")
    payload = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chat_id": str(chat_id),
        "message_id": str(message_id),
        "current_time": snapshot.get("current_time", ""),
        "timezone": snapshot.get("timezone", ""),
        "period": snapshot.get("period", ""),
        "has_contradiction": bool(snapshot.get("contradiction")),
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _looks_transient_telegram_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".casefold()
    markers = [
        "connectionreseterror",
        "connection aborted",
        "remote end closed",
        "remotedisconnected",
        "timed out",
        "timeout",
        "read timed out",
        "temporarily unavailable",
        "10054",
        "502",
        "503",
        "504",
    ]
    return any(marker in text for marker in markers)


def _should_use_pre_v3_time_reply(intent) -> bool:
    return getattr(intent, "kind", "") == "time_query"


class TelegramGateway:
    def __init__(self, token: str, agent: YueYueRuntimeV3):
        self.bot = telebot.TeleBot(token)
        self.agent = agent
        self.agent.interactive_mode = False
        self.turn_coalescer = MessageCoalescer()
        self._presence_stop = threading.Event()
        self._presence_thread: threading.Thread | None = None
        self.render_ledger = RenderLedger(os.path.join(PROJECT_CACHE_DIR, "v3", "render_ledger.json"))
        self.watchdog = GatewayWatchdog(
            token=token,
            chat_id_reader=lambda: read_chat_id_file(CHAT_ID_FILE),
            dump_dir=os.path.join(WORKSPACE_DIR, "logs", "watchdog"),
        )
        original_get_updates = self.bot.get_updates

        def _beating_get_updates(*args, **kwargs):
            self.watchdog.beat("tg_get_updates")
            return original_get_updates(*args, **kwargs)

        self.bot.get_updates = _beating_get_updates
        self.register_handlers()

    def _telegram_call(self, fn, *args, attempts: int = 5, **kwargs):
        last_exc = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                if not _looks_transient_telegram_error(exc) or attempt >= attempts:
                    raise
                print(f"[TG transient] {type(exc).__name__}: {exc}; retry {attempt}/{attempts - 1}")
                time.sleep(min(1.5 * attempt, 6.0))
        if last_exc:
            raise last_exc

    def _telegram_call_best_effort(self, fn, *args, **kwargs):
        try:
            return self._telegram_call(fn, *args, attempts=2, **kwargs)
        except Exception as exc:
            if _looks_transient_telegram_error(exc):
                print(f"[TG best-effort skipped] {type(exc).__name__}: {exc}")
                return None
            print(f"[TG best-effort warning] {type(exc).__name__}: {exc}")
            return None

    def _trace_fast_reply(self, message, kind: str, reply_data: dict) -> None:
        try:
            content = str((reply_data or {}).get("content") or "")
            emit_trace(
                "fast_reply.composed",
                kind=kind,
                composer_source=str((reply_data or {}).get("_composer_source") or kind),
                chat_id=str(message.chat.id),
                message_id=str(getattr(message, "message_id", "")),
                text_len=len(content),
                has_sticker_marker=bool(sticker_pattern().search(content)),
            )
        except Exception:
            pass

    def remember_chat(self, chat_id: int | str) -> None:
        with open(CHAT_ID_FILE, "w", encoding="utf-8") as file:
            file.write(str(chat_id))

    def quote_context(self, message) -> str:
        quoted = getattr(message, "reply_to_message", None)
        if not quoted:
            return ""
        quoted_text = getattr(quoted, "text", None) or getattr(quoted, "caption", None) or "[non-text message]"
        return f"主人引用了先前訊息：「{quoted_text}」\n\n"

    def _send_sticker_asset(self, chat_id: int | str, sticker_name: str) -> bool:
        sticker_path = find_sticker_file(sticker_name)
        job = start_send_job(
            "sticker_asset",
            chat_id,
            os.path.basename(str(sticker_name or "")),
            metadata={"path": sticker_path or ""},
        )

        if not sticker_path or not os.path.exists(sticker_path):
            mark_send_job_failed(job, "sticker file not found", retryable=False)
            return False

        try:
            result = None
            with open(sticker_path, "rb") as file:
                ext = os.path.splitext(sticker_path)[1].lower()
                if ext in {".gif", ".tgs", ".webm", ".mp4"}:
                    result = self._telegram_call(self.bot.send_animation, chat_id, file)
                elif ext == ".webp":
                    result = self._telegram_call(self.bot.send_sticker, chat_id, file)
                else:
                    result = self._telegram_call(self.bot.send_photo, chat_id, file)

            mark_send_job_sent(job, result)
            DEFAULT_SOCIAL_STICKER_INDEX.mark_used(os.path.basename(sticker_path))
            DEFAULT_SOCIAL_SESSION_MANAGER.mark_sticker_sent(chat_id, os.path.basename(sticker_path))
            return True

        except Exception as exc:
            mark_send_job_failed(job, exc, retryable=_looks_transient_telegram_error(exc))
            print(f"[TG warning] sticker send failed: {type(exc).__name__}: {exc}")
            return False

    def send_reply_with_stickers(
        self,
        chat_id: int | str,
        reply_data: dict,
        reply_to_message_id: int | None = None,
        render_event_id: str = "",
        mode: InteractionMode | None = None,
        owner_text: str = "",
    ) -> None:
        if render_event_id and not hasattr(self, "render_ledger"):
            # Lightweight test/fake gateways do not have a persistent render lifecycle.
            # Production gateways always initialize their ledger in __init__.
            render_event_id = ""
        pacing_delay = reply_pacing_delay_seconds(chat_id, owner_text, mode)
        if pacing_delay > 0:
            time.sleep(pacing_delay)
        reply_text = reply_data.get("content", "") or ""
        send_report = reply_data.setdefault("_send_report", {
            "text_sent": 0,
            "text_failed": 0,
            "sticker_sent": 0,
            "sticker_failed": 0,
            "screenshot_sent": 0,
            "screenshot_failed": 0,
            "partial_failed": False,
        })
        if not sticker_pattern().search(reply_text):
            reply_text = _strip_fake_sticker_claims(reply_text)
        sticker_re = sticker_pattern()
        screenshot_re = screenshot_pattern()
        raw_stickers = sticker_re.findall(reply_text)
        stickers = _dedupe_preserve_order(raw_stickers)
        clean_text = sticker_re.sub("", reply_text).strip()
        raw_screenshots = screenshot_re.findall(clean_text)
        screenshots = _dedupe_preserve_order(raw_screenshots)
        clean_text = screenshot_re.sub("", clean_text).strip()
        _record_render_dedupe(chat_id, "sticker", len(raw_stickers), len(stickers))
        _record_render_dedupe(chat_id, "screenshot", len(raw_screenshots), len(screenshots))

        if clean_text:
            parts = _split_reply_bubbles(clean_text)
            for index, part in enumerate(parts or [clean_text]):
                if len(part) > TELEGRAM_MESSAGE_LIMIT:
                    part = part[: TELEGRAM_MESSAGE_LIMIT - 1].rstrip() + "…"
                key = self._render_key(render_event_id, "text", f"{index}:{part}")
                if key and not self.render_ledger.claim(key):
                    continue
                try:
                    self._telegram_call_best_effort(self.bot.send_chat_action, chat_id, "typing")
                    time.sleep(_human_typing_delay(part) + _afterthought_hold(part, index, len(parts)))
                    if reply_to_message_id:
                        self._telegram_call(
                            self.bot.send_message, chat_id, part, reply_to_message_id=reply_to_message_id
                        )
                        reply_to_message_id = None
                    else:
                        self._telegram_call(self.bot.send_message, chat_id, part)
                except Exception:
                    if key:
                        self.render_ledger.release(key)
                    raise

        for sticker_name in stickers:
            key = self._render_key(render_event_id, "sticker", sticker_name)
            if key and not self.render_ledger.claim(key):
                continue
            sticker_ok = self._send_sticker_asset(chat_id, sticker_name)
            if sticker_ok:
                send_report["sticker_sent"] = int(send_report.get("sticker_sent") or 0) + 1
            else:
                send_report["sticker_failed"] = int(send_report.get("sticker_failed") or 0) + 1
                send_report["partial_failed"] = True
                if key:
                    self.render_ledger.release(key)

        for screenshot in screenshots:
            key = self._render_key(render_event_id, "screenshot", screenshot)
            if key and not self.render_ledger.claim(key):
                continue
            screenshot_name = os.path.basename(screenshot.strip())
            path = os.path.abspath(os.path.join(PROJECT_CACHE_DIR, screenshot_name))
            path_is_contained = os.path.commonpath([PROJECT_CACHE_DIR, path]) == os.path.abspath(PROJECT_CACHE_DIR)
            if path_is_contained and os.path.exists(path):
                try:
                    with open(path, "rb") as file:
                        self._telegram_call(self.bot.send_photo, chat_id, file, caption="最後畫面截圖")
                except Exception as exc:
                    if key:
                        self.render_ledger.release(key)
                    print(f"[TG warning] screenshot send failed: {exc}")
            elif key:
                self.render_ledger.release(key)

    @staticmethod
    def _render_key(render_event_id: str, kind: str, identity: str) -> str:
        if not render_event_id:
            return ""
        return RenderLedger.key(render_event_id, kind, identity)

    # Tools worth narrating to the owner while a task runs. Anything not listed stays silent
    # (typing indicator only) so plain chat and cheap lookups never turn into status spam.
    TOOL_PROGRESS_LABELS = {
        "list_windows": "正在看有哪些視窗...",
        "focus_window": "正在切換視窗...",
        "get_screen_ui": "正在解析屏幕...",
        "capture_screen": "正在截取畫面...",
        "analyze_media": "正在看畫面內容...",
        "click_ui_element": "正在點擊介面...",
        "click_screen": "正在點擊畫面...",
        "type_keyboard": "正在輸入文字...",
        "press_hotkey": "正在按快捷鍵...",
        "execute_command": "正在執行系統命令...",
        "execute_python": "正在執行 Python...",
        "send_telegram_media": "正在發送媒體...",
        "list_files": "正在讀取檔案列表...",
        "read_file": "正在讀檔案...",
        "search_in_files": "正在搜尋檔案內容...",
    }
    _PROGRESS_MAX_LINES = 6
    _PROGRESS_MAX_ERRORS = 2
    _PROGRESS_REPEAT_GAP_SECONDS = 20.0

    def _tool_notifier_for(self, message):
        progress = {"lines": 0, "errors": 0, "last_label": "", "last_at": 0.0}

        def notify(tool_name, args, state="start", result=None):
            try:
                if state == "start":
                    self._telegram_call_best_effort(self.bot.send_chat_action, message.chat.id, "typing")
                    label = self.TOOL_PROGRESS_LABELS.get(tool_name)
                    if not label or progress["lines"] >= self._PROGRESS_MAX_LINES:
                        return
                    now = time.time()
                    if label == progress["last_label"] and now - progress["last_at"] < self._PROGRESS_REPEAT_GAP_SECONDS:
                        return
                    progress.update(lines=progress["lines"] + 1, last_label=label, last_at=now)
                    self._telegram_call_best_effort(
                        self.bot.send_message,
                        message.chat.id,
                        f"_{html.escape(label)}_",
                        parse_mode="Markdown",
                    )
                elif state == "end" and result and getattr(result, "status", "") == "error":
                    if getattr(result, "error_category", "") == "transient" or getattr(result, "retryable", False):
                        return
                    if progress["errors"] >= self._PROGRESS_MAX_ERRORS:
                        return
                    progress["errors"] += 1
                    detail = html.escape(str(getattr(result, "error", "") or getattr(result, "message", ""))[:400])
                    self._telegram_call_best_effort(
                        self.bot.send_message,
                        message.chat.id,
                        f"`{tool_name}` 這一步失敗了：{detail}",
                        parse_mode="Markdown",
                    )
            except Exception:
                pass

        return notify

    def _maybe_quick_ack(self, message, mode: InteractionMode) -> None:
        ack = quick_ack_for(mode)
        if not ack:
            return
        with contextlib.suppress(Exception):
            self._telegram_call(self.bot.send_message, message.chat.id, ack, reply_to_message_id=message.message_id)

    def _chat_and_reply(
        self,
        message,
        prompt: str,
        mode: InteractionMode = InteractionMode.CHAT,
        suggested_stickers: list[str] | None = None,
        allow_auto_sticker: bool = False,
    ) -> dict:
        set_telegram_context(message.chat.id, message.message_id)

        owner_prompt = owner_text_from_message(message, prompt)
        prompt = repair_mojibake(prompt)
        turn_now = datetime.datetime.now().astimezone()
        temporal_snapshot = build_temporal_snapshot(owner_prompt, now=turn_now)
        prompt = attach_temporal_context(prompt, chat_id=message.chat.id, owner_prompt=owner_prompt, now=turn_now)
        _record_temporal_context_debug(message.chat.id, message.message_id, temporal_snapshot)
        render_event_id = f"telegram:{message.chat.id}:{message.message_id}"

        mode = _coerce_benign_testing_mode(owner_prompt, mode)
        mode_value = getattr(mode, "value", str(mode))
        intent = classify_owner_intent(owner_prompt, mode_value=mode_value)

        if os.getenv("YUEYUE_DEBUG_PROBE", "").strip() == "1":
            emit_trace(
                "chat_reply.route_probe",
                chat_id=str(message.chat.id),
                intent=str(intent.kind),
                mode=mode_value,
                owner_prompt=owner_prompt[:200],
                allow_auto_sticker=bool(allow_auto_sticker),
            )

        if _should_use_pre_v3_time_reply(intent):
            base_reply = build_time_query_reply(now=turn_now)
            exact_time = str(base_reply.get("content", "")).strip()
            reply_data = compose_fast_reply(
                self.agent,
                kind="time_query",
                owner_prompt=owner_prompt,
                facts={**base_reply, "exact_time": exact_time},
                fallback=base_reply,
            )
            self._trace_fast_reply(message, "time_query", reply_data)
            self.send_reply_with_stickers(
                message.chat.id,
                reply_data,
                message.message_id,
                render_event_id=render_event_id,
            )
            return reply_data

        if is_plain_greeting(owner_prompt):
            reply_data = compose_fast_reply(
                self.agent,
                kind="plain_greeting",
                owner_prompt=owner_prompt,
                facts={},
                fallback={"content": ""},
            )
            self._trace_fast_reply(message, "plain_greeting", reply_data)
            self.send_reply_with_stickers(
                message.chat.id,
                reply_data,
                message.message_id,
                render_event_id=render_event_id,
            )
            return reply_data

        if is_simple_wake_greeting(owner_prompt):
            reply_data = compose_fast_reply(
                self.agent,
                kind="wake_greeting",
                owner_prompt=owner_prompt,
                facts=temporal_snapshot,
                fallback={"content": ""},
            )
            self._trace_fast_reply(message, "wake_greeting", reply_data)
            self.send_reply_with_stickers(
                message.chat.id,
                reply_data,
                message.message_id,
                render_event_id=render_event_id,
            )
            return reply_data

        if intent.kind == "sticker_cancel":
            base_reply = {"content": "好，這張先不發。"}
            reply_data = compose_fast_reply(
                self.agent,
                kind="sticker_cancel",
                owner_prompt=owner_prompt,
                facts={},
                fallback=base_reply,
            )
            self._trace_fast_reply(message, "sticker_cancel", reply_data)
            self.send_reply_with_stickers(
                message.chat.id,
                reply_data,
                message.message_id,
                render_event_id=render_event_id,
            )
            return reply_data

        if is_sticker_resend(owner_prompt, prompt):
            base_reply = direct_sticker_resend_reply(owner_prompt, suggested_stickers or [])
            base_content = str(base_reply.get("content", "") or "")
            marker = ""
            sticker_name = ""

            marker_match = re.search(r"\[(?:表情包|貼圖|贴图|sticker):\s*([^\]]+)\]", base_content, re.I)
            if marker_match:
                sticker_name = marker_match.group(1).strip()
                marker = marker_match.group(0).strip()

            reply_data = compose_fast_reply(
                self.agent,
                kind="sticker_resend",
                owner_prompt=owner_prompt,
                facts={
                    "sticker_marker": marker,
                    "sticker_name": sticker_name,
                },
                fallback=base_reply,
            )
            self._trace_fast_reply(message, "sticker_resend", reply_data)
            self.send_reply_with_stickers(
                message.chat.id,
                reply_data,
                message.message_id,
                render_event_id=render_event_id,
            )
            return reply_data

        if intent.kind == "sticker_send":
            base_reply = direct_sticker_reply(owner_prompt, suggested_stickers or [])
            base_content = str(base_reply.get("content", "") or "")
            marker = ""
            sticker_name = ""

            marker_match = re.search(r"\[(?:表情包|貼圖|贴图|sticker):\s*([^\]]+)\]", base_content, re.I)
            if marker_match:
                sticker_name = marker_match.group(1).strip()
                marker = marker_match.group(0).strip()

            reply_data = compose_fast_reply(
                self.agent,
                kind="sticker_send",
                owner_prompt=owner_prompt,
                facts={
                    "sticker_marker": marker,
                    "sticker_name": sticker_name,
                },
                fallback=base_reply,
            )
            self._trace_fast_reply(message, "sticker_send", reply_data)
            self.send_reply_with_stickers(
                message.chat.id,
                reply_data,
                message.message_id,
                render_event_id=render_event_id,
            )
            return reply_data

        policy = response_policy_for(mode)
        reply_data = self.agent.chat(
            prompt,
            tool_callback=self._tool_notifier_for(message),
            response_policy=policy,
        )

        if allow_auto_sticker:
            reply_data = self._attach_social_sticker_if_needed(reply_data, suggested_stickers or [])

        self.send_reply_with_stickers(
            message.chat.id,
            reply_data,
            message.message_id,
            render_event_id=render_event_id,
            mode=mode,
            owner_text=owner_prompt,
        )
        return reply_data

    def _attach_social_sticker_if_needed(self, reply_data: dict, suggested_stickers: list[str]) -> dict:
        if not suggested_stickers:
            return reply_data
        content = reply_data.get("content", "") or ""
        if sticker_pattern().search(content):
            return reply_data
        sticker = os.path.basename(suggested_stickers[0])
        if not sticker:
            return reply_data
        if not find_sticker_file(sticker):
            return reply_data
        updated = dict(reply_data)
        updated["content"] = (content.rstrip() + f"\n[{STICKER_MARKER_LABEL}: {sticker}]").strip()
        return updated

    def _handle_sticker_curation_command(self, message) -> bool:
        text = (getattr(message, "text", "") or "").strip()
        lowered = text.casefold()
        if self._handle_presence_command(message, text, lowered):
            return True
        if lowered in {"list sticker candidates", "list stickers"} or any(
            marker in text for marker in ["列出貼圖候選", "列出表情包候選", "候選貼圖", "候選表情包"]
        ):
            candidates = DEFAULT_SOCIAL_STICKER_INDEX.list_candidates(limit=10)
            if not candidates:
                self.bot.reply_to(message, "目前沒有待審核的貼圖候選。")
                return True
            lines = ["待審核貼圖候選："]
            for item in candidates:
                details = [f"tags={','.join(item.tags)}"]
                if item.emoji:
                    details.append(f"emoji={item.emoji}")
                if item.set_name:
                    details.append(f"set={item.set_name}")
                if item.file_unique_id:
                    details.append(f"id={item.file_unique_id[:10]}")
                lines.append(f"- {item.filename} ({'; '.join(details)})")
            self.bot.reply_to(message, "\n".join(lines))
            return True

        batch_approve_match = re.match(
            r"^(?:批准最近\s*(\d+|全部)?\s*(?:個)?(?:貼圖|表情包)|approve recent\s*(\d+|all)?\s*stickers?)\s*(.*)$",
            text,
            flags=re.IGNORECASE,
        )
        if batch_approve_match:
            count = _parse_recent_count(batch_approve_match.group(1) or batch_approve_match.group(2) or "")
            tags = [tag.strip() for tag in re.split(r"[\s,]+", batch_approve_match.group(3).strip()) if tag.strip()]
            try:
                approved = DEFAULT_SOCIAL_STICKER_INDEX.approve_recent_candidates(count, tags=tags)
                if not approved:
                    self.bot.reply_to(message, "目前沒有可批准的貼圖候選。")
                else:
                    self.bot.reply_to(
                        message,
                        "已批准貼圖：\n"
                        + "\n".join(f"- {item.filename} tags={','.join(item.tags)}" for item in approved),
                    )
            except Exception as exc:
                self.bot.reply_to(message, f"批量批准貼圖失敗：{exc}")
            return True

        batch_reject_match = re.match(
            r"^(?:拒絕最近\s*(\d+|全部)?\s*(?:個)?(?:貼圖|表情包)|reject recent\s*(\d+|all)?\s*stickers?)\s*(.*)$",
            text,
            flags=re.IGNORECASE,
        )
        if batch_reject_match:
            count = _parse_recent_count(batch_reject_match.group(1) or batch_reject_match.group(2) or "")
            reason = batch_reject_match.group(3).strip()
            try:
                rejected = DEFAULT_SOCIAL_STICKER_INDEX.reject_recent_candidates(count, reason=reason)
                if not rejected:
                    self.bot.reply_to(message, "目前沒有可拒絕的貼圖候選。")
                else:
                    self.bot.reply_to(message, "已拒絕貼圖：\n" + "\n".join(f"- {item.filename}" for item in rejected))
            except Exception as exc:
                self.bot.reply_to(message, f"批量拒絕貼圖失敗：{exc}")
            return True

        latest_approve_match = re.match(
            r"^(?:批准最新貼圖|批准最新表情包|approve latest sticker)\s*(.*)$", text, flags=re.IGNORECASE
        )
        if latest_approve_match:
            payload = ("最新 " + latest_approve_match.group(1).strip()).strip()
            filename, tags = _split_sticker_command_payload(payload)
            latest = DEFAULT_SOCIAL_STICKER_INDEX.latest_candidate()
            filename = latest.filename if latest else ""
            try:
                entry = DEFAULT_SOCIAL_STICKER_INDEX.approve_candidate(filename, tags=tags)
                self.bot.reply_to(message, f"已批准貼圖：{entry.filename} tags={','.join(entry.tags)}")
            except Exception as exc:
                self.bot.reply_to(message, f"批准貼圖失敗：{exc}")
            return True

        approve_match = re.match(r"^(?:批准貼圖|批准表情包|approve sticker)\s+(.+)$", text, flags=re.IGNORECASE)
        if approve_match:
            payload = approve_match.group(1).strip()
            filename, tags = _split_sticker_command_payload(payload)
            if filename in {"最新", "latest", "last"}:
                latest = DEFAULT_SOCIAL_STICKER_INDEX.latest_candidate()
                filename = latest.filename if latest else ""
            try:
                entry = DEFAULT_SOCIAL_STICKER_INDEX.approve_candidate(filename, tags=tags)
                self.bot.reply_to(message, f"已批准貼圖：{entry.filename} tags={','.join(entry.tags)}")
            except Exception as exc:
                self.bot.reply_to(message, f"批准貼圖失敗：{exc}")
            return True

        latest_reject_match = re.match(
            r"^(?:拒絕最新貼圖|拒絕最新表情包|reject latest sticker)\s*(.*)$", text, flags=re.IGNORECASE
        )
        if latest_reject_match:
            payload = ("最新 " + latest_reject_match.group(1).strip()).strip()
            filename, tags = _split_sticker_command_payload(payload)
            latest = DEFAULT_SOCIAL_STICKER_INDEX.latest_candidate()
            filename = latest.filename if latest else ""
            try:
                entry = DEFAULT_SOCIAL_STICKER_INDEX.reject_candidate(filename, reason=" ".join(tags))
                self.bot.reply_to(message, f"已拒絕貼圖：{entry.filename}")
            except Exception as exc:
                self.bot.reply_to(message, f"拒絕貼圖失敗：{exc}")
            return True

        reject_match = re.match(r"^(?:拒絕貼圖|拒絕表情包|reject sticker)\s+(.+)$", text, flags=re.IGNORECASE)
        if reject_match:
            payload = reject_match.group(1).strip()
            filename, tags = _split_sticker_command_payload(payload)
            if filename in {"最新", "latest", "last"}:
                latest = DEFAULT_SOCIAL_STICKER_INDEX.latest_candidate()
                filename = latest.filename if latest else ""
            try:
                entry = DEFAULT_SOCIAL_STICKER_INDEX.reject_candidate(filename, reason=" ".join(tags))
                self.bot.reply_to(message, f"已拒絕貼圖：{entry.filename}")
            except Exception as exc:
                self.bot.reply_to(message, f"拒絕貼圖失敗：{exc}")
            return True
        return False

    def _handle_presence_command(self, message, text: str, lowered: str) -> bool:
        markers = [
            "presence status",
            "presence candidates",
            "presence debug",
            "月月剛剛有沒有想找我",
            "月月刚刚有没有想找我",
            "月月剛才有沒有想找我",
            "月月刚才有没有想找我",
            "月月想找我嗎",
            "月月想找我吗",
            "月月為什麼沒找我",
            "月月为什么没找我",
            "月月剛剛想說什麼",
            "月月刚刚想说什么",
        ]
        if lowered not in markers and not any(marker in text for marker in markers if not marker.isascii()):
            return False
        health = DEFAULT_PRESENCE_ENGINE.write_health()
        candidates = DEFAULT_PRESENCE_ENGINE.recent_candidates(limit=3)
        debug_rows = DEFAULT_PRESENCE_ENGINE.recent_debug(limit=5)
        lines = [
            f"月月的待命狀態：{health.get('mode', 'unknown')}",
            f"候選 {health.get('candidate_count', 0)} 次，已主動發 {health.get('sent_count', 0)} 次，被壓住 {health.get('suppressed_count', 0)} 次。",
            f"今天 {health.get('daily_count', 0)}/{health.get('daily_limit', 1)}，冷卻 {health.get('min_interval_minutes', 0)} 分鐘，破冰 {health.get('icebreak_after_minutes', 0)} 分鐘，安靜時間 {health.get('quiet_hours', '')}。",
            f"Composer：{'啟用' if health.get('composer_enabled') else '未啟用'}，模型 {health.get('composer_model', 'unknown')}。",
        ]
        if debug_rows:
            lines.append("最近判斷：")
            for row in debug_rows[-3:]:
                decision = row.get("decision", "unknown")
                reason = row.get("reason", "")
                awake = "醒著" if row.get("owner_likely_awake") else "未確認醒著"
                quiet = "安靜時間" if row.get("quiet_now") else "非安靜時間"
                quality = row.get("quality") if isinstance(row.get("quality"), dict) else {}
                quality_reason = quality.get("reason") if quality else ""
                suffix = f"，內容 gate：{quality_reason}" if quality_reason else ""
                lines.append(f"- {decision}: {reason}（{awake} / {quiet}{suffix}）")
        if not candidates:
            lines.append("剛剛沒有特別想打擾你的候選，月月很乖地待著～")
        else:
            lines.append("最近幾個候選：")
            for item in candidates:
                message_text = item.message or "等 composer 判斷有沒有值得說的內容"
                lines.append(f"- {item.kind}: {message_text} ({item.reason})")
        self.bot.reply_to(message, "\n".join(lines))
        return True

    def _enqueue_turn_part(self, part: InboundMessagePart) -> None:
        self.turn_coalescer.add(part, self._flush_aggregated_turn)

    def _flush_aggregated_turn(self, turn) -> None:
        message = turn.reply_message
        if not message:
            return
        watchdog_token = self.watchdog.begin_turn(turn.chat_id, turn.primary_message_id)
        try:
            turn.mode = _coerce_benign_testing_mode(turn.primary_text, turn.mode)
            if turn.mode in {InteractionMode.VISION_TASK, InteractionMode.TOOL_TASK}:
                self._maybe_quick_ack(message, turn.mode)
            prompt = build_turn_prompt(self.quote_context(message), turn)
            context_text = "\n".join(
                item for item in [turn.primary_text, *[part.caption for part in turn.parts if part.caption]] if item
            )
            media_context = [
                {
                    "kind": part.kind,
                    "path": part.path,
                    "media_type": part.media_type,
                    "caption": part.caption,
                }
                for part in turn.parts
                if part.kind in {"photo", "sticker"}
            ]
            # CHAT/SOCIAL already get these exact turns re-injected as role-tagged messages by the v3
            # ContextCompiler (short_context.recent), so we drop the duplicate conversation lines from
            # this note and keep only its unique parts (URL/media/reference/topic). TASK/VISION do not
            # get v3 recent(), so they keep the full conversational background.
            include_conversation = turn.mode in _TASK_ORIENTED_MODES
            context_note, _context_turn = build_context_for_turn(
                turn.chat_id, context_text, media_context, include_conversation=include_conversation
            )
            if context_note:
                if turn.mode in _TASK_ORIENTED_MODES:
                    context_note = (
                        "以下是最近幾輪的話題/情緒中繼資料，僅供理解代詞或背景使用；"
                        "這是任務執行情境，不要因為裡面的語氣或情緒描述而放鬆執行標準、"
                        "變得隨性或不確定，繼續保持任務模式該有的可靠度。\n" + context_note
                    )
                prompt = f"{prompt}\n\n{context_note}" if prompt else context_note
            social_state = DEFAULT_SOCIAL_SESSION_MANAGER.observe_turn(
                turn.chat_id,
                text=turn.primary_text,
                has_sticker=bool(turn.stickers),
                has_photo=bool(turn.photos),
                mode=turn.mode.value,
            )
            suggestions: list[str] = []
            if social_state.mode != "idle" and should_inject_social_note(turn.mode.value):
                suggestions = DEFAULT_SOCIAL_SESSION_MANAGER.suggest_stickers(
                    turn.chat_id, DEFAULT_SOCIAL_STICKER_INDEX, turn.primary_text
                )
                social_note = DEFAULT_SOCIAL_SESSION_MANAGER.build_prompt_note(turn.chat_id, suggestions)
                if social_note:
                    prompt = f"{prompt}\n\n{social_note}" if prompt else social_note
            social_policy = social_reply_policy_for(
                social_state.mode, social_state.intent_tags, has_sticker=bool(turn.stickers)
            )
            allow_auto_sticker = (
                social_policy.should_attach_sticker
                and social_state.mode != "idle"
                and _should_allow_auto_sticker(
                    turn.primary_text,
                    has_sticker=bool(turn.stickers),
                    has_photo=bool(turn.photos),
                    mode_value=turn.mode.value,
                    suggested_stickers=suggestions,
                )
            )
            reply_data = self._chat_and_reply(
                message, prompt, turn.mode, suggested_stickers=suggestions, allow_auto_sticker=allow_auto_sticker
            )
            DEFAULT_SHORT_CONTEXT_BUFFER.update_last_assistant(
                turn.chat_id,
                reply_summary_for_context(reply_data) if isinstance(reply_data, dict) else "",
            )
            self._record_presence_candidate(turn, reply_data)
            # ROADMAP P2: long-term memory distillation runs on the live path only, in the
            # background, AFTER the reply is out - the owner never waits on it and scripted
            # test providers never see it. Chat/social turns only; task turns are workflows.
            if turn.mode.value in {"chat", "social_sticker"}:
                threading.Thread(
                    target=lambda: self.agent.maybe_distill_memory("telegram"), daemon=True
                ).start()
        except Exception as exc:
            print(f"[TG warning] aggregated turn failed: {type(exc).__name__}: {exc}")
            try:
                self.bot.reply_to(message, self._owner_safe_turn_error(exc))
            except Exception:
                return
        finally:
            self.watchdog.end_turn(watchdog_token)

    @staticmethod
    def _owner_safe_turn_error(_exc: Exception) -> str:
        return "剛剛處理訊息時卡了一下，我沒有繼續亂操作。你可以再試一次。"

    def _record_presence_candidate(self, turn, reply_data) -> None:
        try:
            turns = DEFAULT_SHORT_CONTEXT_BUFFER.turns.get(str(turn.chat_id)) or []
            latest = turns[-1] if turns else None
            short_context = {
                "primary_text": turn.primary_text,
                "summary": reply_data.get("content", "") if isinstance(reply_data, dict) else "",
                "topic": getattr(latest, "topic", "") if latest else "",
                "mood": getattr(latest, "mood", "") if latest else "",
                "turns": [
                    {
                        "text": item.text,
                        "topic": item.topic,
                        "emotion": item.mood,
                        "assistant_summary": item.assistant_summary,
                    }
                    for item in turns[-5:]
                ],
            }
            if latest and getattr(latest, "created_at", None):
                short_context["created_at"] = latest.created_at
            session_summary, session_updated_at = self._presence_session_state()
            DEFAULT_PRESENCE_ENGINE.evaluate(
                turn.chat_id,
                short_context=short_context,
                session_summary=session_summary,
                session_updated_at=session_updated_at,
                source="post_turn",
            )
        except Exception as exc:
            print(f"[presence warning] {exc}")

    def _presence_session_state(self) -> tuple[str, float | None]:
        workflow = getattr(getattr(self.agent, "state", None), "workflow", None)
        if not workflow:
            return "", None
        status = getattr(getattr(workflow, "status", None), "value", "")
        summary = {
            "running": "active_task",
            "planned": "active_task",
            "awaiting_permission": "awaiting_permission",
            "blocked": "blocked",
        }.get(status, "")
        updated_at = getattr(workflow, "updated_at", None)
        return summary, updated_at if isinstance(updated_at, (int, float)) else None

    def _presence_context_for_chat(self, chat_id: int | str) -> dict:
        turns = DEFAULT_SHORT_CONTEXT_BUFFER.turns.get(str(chat_id)) or []
        latest = turns[-1] if turns else None
        return {
            "primary_text": getattr(latest, "text", "") if latest else "",
            "topic": getattr(latest, "topic", "") if latest else "",
            "mood": getattr(latest, "mood", "") if latest else "",
            "assistant_summary": getattr(latest, "assistant_summary", "") if latest else "",
            "created_at": getattr(latest, "created_at", 0.0) if latest else 0.0,
            "turns": [
                {
                    "text": item.text,
                    "topic": item.topic,
                    "emotion": item.mood,
                    "assistant_summary": item.assistant_summary,
                    "created_at": item.created_at,
                }
                for item in turns[-5:]
            ],
        }

    def _read_presence_chat_id(self) -> str:
        try:
            with open(CHAT_ID_FILE, encoding="utf-8") as file:
                return file.read().strip()
        except Exception:
            return ""

    def _presence_scheduler_loop(self) -> None:
        interval = max(60, int(DEFAULT_PRESENCE_ENGINE.config.tick_minutes * 60))
        while not self._presence_stop.wait(interval):
            chat_id = self._read_presence_chat_id()
            if not chat_id:
                continue
            try:
                session_summary, session_updated_at = self._presence_session_state()
                context = self._presence_context_for_chat(chat_id)
                DEFAULT_PRESENCE_ENGINE.tick(
                    chat_id,
                    short_context=context,
                    session_summary=session_summary,
                    session_updated_at=session_updated_at,
                    send_callback=lambda text, cid=chat_id: self._telegram_call(self.bot.send_message, cid, text),
                )
            except Exception as exc:
                print(f"[presence scheduler warning] {exc}")

    def _start_presence_scheduler(self) -> None:
        if self._presence_thread and self._presence_thread.is_alive():
            return
        self._presence_stop.clear()
        self._presence_thread = threading.Thread(
            target=self._presence_scheduler_loop, name="YueYuePresenceScheduler", daemon=True
        )
        self._presence_thread.start()

    def register_handlers(self) -> None:
        @self.bot.message_handler(commands=["start", "help"])
        def send_welcome(message):
            try:
                self.remember_chat(message.chat.id)
                self.bot.reply_to(message, "喵，月月見已上線。")
            except Exception as exc:
                print(f"[TG welcome handler error] {type(exc).__name__}: {exc}")

        @self.bot.message_handler(content_types=["text"])
        def echo_all(message):
            try:
                self.remember_chat(message.chat.id)
                print(f"\n[TG text] {message.from_user.first_name}: {message.text}")
                if self._handle_sticker_curation_command(message):
                    return
                self._telegram_call_best_effort(self.bot.send_chat_action, message.chat.id, "typing")
                self._enqueue_turn_part(
                    InboundMessagePart(
                        chat_id=message.chat.id,
                        message_id=message.message_id,
                        kind="text",
                        text=message.text or "",
                        message=message,
                    )
                )
            except Exception as exc:
                print(f"[TG text handler error] {type(exc).__name__}: {exc}")
                with contextlib.suppress(Exception):
                    self.bot.reply_to(message, f"嗚，處理訊息時出錯了：{exc}")

        @self.bot.message_handler(content_types=["photo"])
        def handle_photo(message):
            try:
                self.remember_chat(message.chat.id)
                print(f"\n[TG photo] {message.from_user.first_name}")
                file_info = self.bot.get_file(message.photo[-1].file_id)
                downloaded = self.bot.download_file(file_info.file_path)
                ext = os.path.splitext(file_info.file_path)[1] or ".jpg"
                filename = f"tg_photo_{message.message_id}_{int(time.time())}{ext}"
                filepath = os.path.join(TG_IMAGES_DIR, filename)
                with open(filepath, "wb") as file:
                    file.write(downloaded)
                caption = message.caption or ""
                media_type = media_type_for(filepath)
                DEFAULT_MEDIA_CACHE.remember(filepath, media_type=media_type, short_caption=caption or "telegram photo")
                self._enqueue_turn_part(
                    InboundMessagePart(
                        chat_id=message.chat.id,
                        message_id=message.message_id,
                        kind="photo",
                        caption=caption,
                        path=filepath,
                        media_type=media_type,
                        media_kind="photo",
                        message=message,
                    )
                )
            except Exception as exc:
                print(f"[TG photo handler error] {type(exc).__name__}: {exc}")
                with contextlib.suppress(Exception):
                    self.bot.reply_to(message, f"圖片處理失敗：{exc}")

        @self.bot.message_handler(content_types=["sticker"])
        def handle_sticker(message):
            try:
                self.remember_chat(message.chat.id)
                print(f"\n[TG sticker] {message.from_user.first_name}")
                file_info = self.bot.get_file(message.sticker.file_id)
                downloaded = self.bot.download_file(file_info.file_path)
                ext = ".webp"
                if getattr(message.sticker, "is_animated", False):
                    ext = ".tgs"
                elif getattr(message.sticker, "is_video", False):
                    ext = ".webm"
                elif file_info.file_path:
                    ext = os.path.splitext(file_info.file_path)[1] or ext
                filename = f"tg_sticker_{message.message_id}_{int(time.time())}{ext}"
                filepath = os.path.join(TG_IMAGES_DIR, filename)
                with open(filepath, "wb") as file:
                    file.write(downloaded)
                media_type = media_type_for(filepath)
                sticker_meta = {
                    "file_unique_id": getattr(message.sticker, "file_unique_id", "") or "",
                    "file_id": getattr(message.sticker, "file_id", "") or "",
                    "emoji": getattr(message.sticker, "emoji", "") or "",
                    "set_name": getattr(message.sticker, "set_name", "") or "",
                    "is_animated": bool(getattr(message.sticker, "is_animated", False)),
                    "is_video": bool(getattr(message.sticker, "is_video", False)),
                }
                caption = getattr(message, "caption", "") or sticker_meta["emoji"]
                DEFAULT_MEDIA_CACHE.remember(
                    filepath, media_type=media_type, short_caption=caption or "telegram sticker"
                )
                DEFAULT_SOCIAL_STICKER_INDEX.catalog_incoming(filepath, media_type=media_type, metadata=sticker_meta)
                pending_count = len(DEFAULT_SOCIAL_STICKER_INDEX.list_candidates(limit=50))
                if DEFAULT_SOCIAL_CURATION_REMINDER.should_remind(message.chat.id, pending_count):
                    self.bot.reply_to(
                        message, DEFAULT_SOCIAL_CURATION_REMINDER.message(pending_count), parse_mode="Markdown"
                    )
                self._enqueue_turn_part(
                    InboundMessagePart(
                        chat_id=message.chat.id,
                        message_id=message.message_id,
                        kind="sticker",
                        caption=caption,
                        path=filepath,
                        media_type=media_type,
                        media_kind="sticker" if media_type != "video_sticker" else "video_sticker",
                        message=message,
                    )
                )
            except Exception as exc:
                print(f"[TG sticker handler error] {type(exc).__name__}: {exc}")
                with contextlib.suppress(Exception):
                    self.bot.reply_to(message, f"貼圖處理失敗：{exc}")

        @self.bot.message_handler(
            content_types=[
                "voice",
                "video",
                "video_note",
                "document",
                "audio",
                "animation",
                "location",
                "contact",
                "venue",
                "poll",
            ]
        )
        def handle_unsupported(message):
            try:
                self.remember_chat(message.chat.id)
                print(f"\n[TG unsupported] {message.from_user.first_name}: content_type={message.content_type}")
                self.bot.reply_to(message, "喵，這種訊息類型我還不會處理，先傳文字、圖片或貼圖給我吧。")
            except Exception as exc:
                print(f"[TG unsupported handler error] {type(exc).__name__}: {exc}")

    def start(self) -> None:
        print("\n>>> Telegram bot mode started.")
        self._start_presence_scheduler()
        self.watchdog.beat("tg_get_updates")
        self.watchdog.start()

        import logging as _logging
        import random as _random

        # Reduce pyTelegramBotAPI connection-retry noise on unstable home networks, but keep
        # ERROR/CRITICAL visible - CRITICAL-only previously hid genuine handler exceptions
        # (e.g. an uncaught error inside a message handler) with no trace anywhere.
        try:
            telebot.logger.setLevel(_logging.ERROR)
            _logging.getLogger("TeleBot").setLevel(_logging.ERROR)
            _logging.getLogger("urllib3").setLevel(_logging.WARNING)
        except Exception:
            pass

        # Shorter timeouts recover faster on flaky Wi-Fi / Deco mesh.
        try:
            telebot.apihelper.CONNECT_TIMEOUT = 15
            telebot.apihelper.READ_TIMEOUT = 25
        except Exception:
            pass

        backoff = 3.0

        while True:
            try:
                print("[TG polling] starting, timeout=20, long_polling_timeout=20")
                self.bot.polling(
                    non_stop=False,
                    interval=1,
                    timeout=20,
                    long_polling_timeout=20,
                    allowed_updates=["message"],
                )
                print("[TG polling] stopped normally; restarting soon.")
                backoff = 3.0
            except KeyboardInterrupt:
                print("\n[TG polling] stopped by owner.")
                with contextlib.suppress(Exception):
                    self.bot.stop_polling()
                raise
            except Exception as exc:
                if _looks_transient_telegram_error(exc):
                    print(f"[TG polling reconnect] {type(exc).__name__}: {exc}")
                else:
                    print(f"[TG polling unexpected] {type(exc).__name__}: {exc}")

                with contextlib.suppress(Exception):
                    self.bot.stop_polling()

            sleep_for = min(backoff + _random.random(), 45.0)
            print(f"[TG polling] reconnecting in {sleep_for:.1f}s")
            time.sleep(sleep_for)
            backoff = min(backoff * 1.6, 45.0)



def should_inject_social_note(mode: str) -> bool:
    return str(mode or "") in {
        InteractionMode.CHAT.value,
        InteractionMode.SOCIAL_STICKER.value,
        "chat",
        "social",
    }


def build_agent(runtime_name: str | None = None, provider_override=None, state_dir=None):
    from yueyue_v3.providers import SiliconFlowProvider

    provider = provider_override or SiliconFlowProvider(
        API_KEY,
        # env_value checks real OS env first, then .env - a plain os.getenv() here silently
        # never saw a .env-only YUEYUE_TASK_MODEL/YUEYUE_STRONG_MODEL, same gap YUEYUE_CHAT_MODEL
        # had until this pass.
        model=env_value("YUEYUE_TASK_MODEL") or env_value("YUEYUE_STRONG_MODEL") or "deepseek-ai/DeepSeek-V4-Pro",
    )
    root = os.path.dirname(os.path.abspath(__file__))
    return YueYueRuntimeV3(root, provider, state_dir=state_dir)


def _acquire_single_instance_lock():
    """Bind a localhost port as a process-wide lock so a second `main.py --telegram` exits
    immediately instead of fighting the first over Telegram getUpdates (API error 409, both
    instances flapping). A socket is self-releasing on process death, unlike a pid file.
    Returns the bound socket to keep alive for the process lifetime, or None if another
    instance already holds it."""
    import socket

    port = int(os.getenv("YUEYUE_SINGLETON_PORT") or 48620)
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", port))
        lock.listen(1)
        return lock
    except OSError:
        lock.close()
        return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="YueYue Agent runtime")
    parser.add_argument("--telegram", action="store_true", help="Start Telegram bot mode directly.")
    parser.add_argument("--terminal", action="store_true", help="Start terminal chat mode directly.")
    parser.add_argument("--health", action="store_true", help="Print health dashboard and exit.")
    args = parser.parse_args()
    if args.health:
        print_gateway_dashboard()
        sys.exit(0)
    if args.telegram:
        _instance_lock = _acquire_single_instance_lock()
        if _instance_lock is None:
            print("[startup] Another YueYue Telegram instance already holds the singleton lock; exiting.")
            sys.exit(3)
        gateway = TelegramGateway(TG_TOKEN, build_agent())
        gateway.start()
        sys.exit(0)
    if args.terminal:
        agent = build_agent()
        print("\n>>> Terminal chat started. Type exit to quit.")
        while True:
            user_msg = input("\n主人: ")
            if user_msg.casefold() == "exit":
                break
            reply = agent.chat(user_msg)
            print(f"\n月月: {reply.get('content')}")
        sys.exit(0)

    while True:
        print_gateway_dashboard()
        choice = input("Choose: ").strip()
        if choice == "0":
            sys.exit(0)
        if choice not in {"1", "2"}:
            continue

        agent = build_agent()
        if choice == "1":
            print("\n>>> Terminal chat started. Type exit to quit.")
            while True:
                user_msg = input("\n主人: ")
                if user_msg.casefold() == "exit":
                    break
                reply = agent.chat(user_msg)
                print(f"\n月月見: {reply.get('content')}")
        elif choice == "2":
            gateway = TelegramGateway(TG_TOKEN, agent)
            try:
                gateway.start()
            except KeyboardInterrupt:
                break
