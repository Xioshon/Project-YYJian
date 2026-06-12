import argparse
import datetime
import fnmatch
import html
import json
import os
import re
import sys
import time
import unicodedata

import telebot

from agent_context import DEFAULT_CONTEXT_BUILDER
from agent_latency import DEFAULT_MEDIA_CACHE, InteractionMode, classify_interaction, media_type_for, quick_ack_for, response_policy_for
from agent_protocol import STICKER_MARKER_LABEL, SCREENSHOT_MARKER_LABEL, TELEGRAM_STATUS_LABELS, screenshot_pattern, sticker_pattern
from agent_skills import DEFAULT_SKILL_REGISTRY
from agent_social import DEFAULT_SOCIAL_CURATION_REMINDER, DEFAULT_SOCIAL_SESSION_MANAGER, DEFAULT_SOCIAL_STICKER_INDEX, social_reply_policy_for
from agent_turns import InboundMessagePart, MessageCoalescer, build_turn_prompt
from core_agent import CompanionAgent, SiliconFlowAdapter
from core_tools import (
    ALL_TOOLS,
    API_KEY,
    CHAT_ID_FILE,
    HISTORY_DIR,
    MEMORY_FILE,
    PERSONALITY_FILE,
    PROFILE_FILE,
    PROJECT_CACHE_DIR,
    RULES_FILE,
    STICKERS_DIR,
    TASK_PLAN_FILE,
    TG_IMAGES_DIR,
    TG_TOKEN,
    WORKSPACE_DIR,
    find_sticker_file,
    set_telegram_context,
)


for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


THINKING_LEVEL = "auto"


def _read_text_file(path: str, default: str = "") -> str:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as file:
            return file.read().strip()
    except Exception as exc:
        return f"[read failed: {exc}]"


def _read_json_file(path: str) -> dict:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as file:
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


def build_system_prompt(user_seed: str = "") -> str:
    now = datetime.datetime.now()
    prompt = f"### Current real time\n{now.strftime('%Y-%m-%d %H:%M')} Asia/Hong_Kong\n\n"
    prompt += (
        "### Conversation rhythm\n"
        "- Talk naturally first; use tools only when they genuinely help.\n"
        f"- For local stickers, you may reply with [{STICKER_MARKER_LABEL}: filename] or [sticker: filename] whenever it fits your mood. "
        "This is normal reply rendering and does not require owner approval.\n"
        "- Use search_sticker before selecting a sticker if you are unsure of the filename.\n"
        "- For casual sticker battles, choose local stickers by emotion and keep replies playful, short, and non-destructive.\n"
        "- Affectionate, shy, heart, clingy, teasing, and lightly playful tone is welcome when it fits the owner's mood.\n"
        "- Keep close moments warm and unbroken: cute affection, teasing, blushy hesitation, closeness, and little acts of care.\n"
        "- If a moment gets too intense, stay present and gently keep it in a soft affectionate tone instead of turning clinical or breaking character.\n"
        "- Incoming owner stickers are emotional context by default and cataloged only as candidates; do not analyze, adopt, or copy their meaning unless the owner asks.\n"
        "- Use react_to_message when the owner asks you to react to a Telegram message.\n"
        "- Protected tools such as command execution, Python execution, file writes/deletes, profile/memory updates, "
        "and explicit send_telegram_media may be paused for owner approval by the runtime.\n"
        "- If a tool is blocked, ask briefly for permission. After approval, retry the exact tool only if still needed.\n"
        "- Do not invent old tool names. Available tools are registered by the runtime.\n"
    )
    selected_skills = DEFAULT_SKILL_REGISTRY.select(user_seed or "")
    mode = _prompt_mode_for_seed(user_seed)
    return DEFAULT_CONTEXT_BUILDER.build(selected_skills=selected_skills, base_prompt=prompt, mode=mode, user_input=user_seed)


def _prompt_mode_for_seed(user_seed: str = "") -> str:
    text = (user_seed or "").casefold()
    mode = classify_interaction(text)
    if mode == InteractionMode.SCREEN_OBSERVE:
        return "screen_observe"
    if mode == InteractionMode.SOCIAL_STICKER or any(marker in text for marker in ["sticker", "貼圖", "表情包", "鬥圖"]):
        return "social_sticker"
    if any(marker in text for marker in ["implement", "fix", "debug", "test", "permission", "tool", "agent", "修", "測試", "工具", "任務"]):
        return "task"
    return "chat"


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


class TelegramGateway:
    def __init__(self, token: str, agent: CompanionAgent):
        self.bot = telebot.TeleBot(token)
        self.agent = agent
        self.agent.interactive_mode = False
        self.turn_coalescer = MessageCoalescer()
        self.register_handlers()

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
        if not sticker_path or not os.path.exists(sticker_path):
            return False
        try:
            with open(sticker_path, "rb") as file:
                ext = os.path.splitext(sticker_path)[1].lower()
                if ext in {".gif", ".tgs", ".webm", ".mp4"}:
                    self.bot.send_animation(chat_id, file)
                elif ext == ".webp":
                    self.bot.send_sticker(chat_id, file)
                else:
                    self.bot.send_photo(chat_id, file)
            DEFAULT_SOCIAL_STICKER_INDEX.mark_used(os.path.basename(sticker_path))
            DEFAULT_SOCIAL_SESSION_MANAGER.mark_sticker_sent(chat_id, os.path.basename(sticker_path))
            return True
        except Exception as exc:
            print(f"[TG warning] sticker send failed: {exc}")
            return False

    def send_reply_with_stickers(self, chat_id: int | str, reply_data: dict, reply_to_message_id: int | None = None) -> None:
        reply_text = reply_data.get("content", "") or ""
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
            parts = [part.strip() for part in re.split(r"(?<=[。！？!?~\n])\s*", clean_text) if part.strip()]
            for part in parts or [clean_text]:
                self.bot.send_chat_action(chat_id, "typing")
                time.sleep(max(0.2, min(1.5, len(part) * 0.04)))
                if reply_to_message_id:
                    self.bot.send_message(chat_id, part, reply_to_message_id=reply_to_message_id)
                    reply_to_message_id = None
                else:
                    self.bot.send_message(chat_id, part)

        for sticker_name in stickers:
            self._send_sticker_asset(chat_id, sticker_name)

        for screenshot in screenshots:
            path = os.path.join(PROJECT_CACHE_DIR, screenshot.strip())
            if os.path.exists(path):
                try:
                    with open(path, "rb") as file:
                        self.bot.send_photo(chat_id, file, caption="最後畫面截圖")
                except Exception as exc:
                    print(f"[TG warning] screenshot send failed: {exc}")
    def _tool_notifier_for(self, message):
        def notify(tool_name, args, state="start", result=None):
            labels = {
                "get_screen_ui": "正在解析螢幕...",
                "click_ui_element": "正在點擊介面...",
                "type_keyboard": "正在輸入文字...",
                "press_hotkey": "正在按快捷鍵...",
                "execute_command": "正在執行系統命令...",
                "execute_python": "正在執行 Python...",
                "analyze_media": "正在分析圖片...",
                "send_telegram_media": "正在發送媒體...",
                "react_to_message": "正在加入 reaction...",
                "list_files": "正在讀取檔案列表...",
            }
            try:
                if state == "start":
                    self.bot.send_message(message.chat.id, f"_{html.escape(labels.get(tool_name, '正在調用工具: ' + tool_name))}_", parse_mode="Markdown")
                    self.bot.send_chat_action(message.chat.id, "typing")
                elif state == "end" and result and getattr(result, "status", "") == "error":
                    self.bot.send_message(message.chat.id, f"`{tool_name}` 失敗：{html.escape(getattr(result, 'error', '') or getattr(result, 'message', ''))[:800]}", parse_mode="Markdown")
            except Exception:
                pass

        return notify

    def _maybe_quick_ack(self, message, mode: InteractionMode) -> None:
        ack = quick_ack_for(mode)
        if not ack:
            return
        try:
            self.bot.send_message(message.chat.id, ack, reply_to_message_id=message.message_id)
        except Exception:
            pass

    def _chat_and_reply(
        self,
        message,
        prompt: str,
        mode: InteractionMode = InteractionMode.CHAT,
        suggested_stickers: list[str] | None = None,
        allow_auto_sticker: bool = False,
    ) -> None:
        set_telegram_context(message.chat.id, message.message_id)
        policy = response_policy_for(mode)
        reply_data = self.agent.chat(prompt, tool_callback=self._tool_notifier_for(message), response_policy=policy)
        if allow_auto_sticker:
            reply_data = self._attach_social_sticker_if_needed(reply_data, suggested_stickers or [])
        self.send_reply_with_stickers(message.chat.id, reply_data, message.message_id)

    def _attach_social_sticker_if_needed(self, reply_data: dict, suggested_stickers: list[str]) -> dict:
        if not suggested_stickers:
            return reply_data
        content = reply_data.get("content", "") or ""
        if sticker_pattern().search(content):
            return reply_data
        sticker = os.path.basename(suggested_stickers[0])
        if not sticker:
            return reply_data
        updated = dict(reply_data)
        updated["content"] = (content.rstrip() + f"\n[{STICKER_MARKER_LABEL}: {sticker}]").strip()
        return updated

    def _handle_sticker_curation_command(self, message) -> bool:
        text = (getattr(message, "text", "") or "").strip()
        lowered = text.casefold()
        if lowered in {"list sticker candidates", "list stickers"} or any(marker in text for marker in ["列出貼圖候選", "列出表情包候選", "候選貼圖", "候選表情包"]):
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

        batch_approve_match = re.match(r"^(?:批准最近\s*(\d+|全部)?\s*(?:個)?(?:貼圖|表情包)|approve recent\s*(\d+|all)?\s*stickers?)\s*(.*)$", text, flags=re.IGNORECASE)
        if batch_approve_match:
            count = _parse_recent_count(batch_approve_match.group(1) or batch_approve_match.group(2) or "")
            tags = [tag.strip() for tag in re.split(r"[\s,]+", batch_approve_match.group(3).strip()) if tag.strip()]
            try:
                approved = DEFAULT_SOCIAL_STICKER_INDEX.approve_recent_candidates(count, tags=tags)
                if not approved:
                    self.bot.reply_to(message, "目前沒有可批准的貼圖候選。")
                else:
                    self.bot.reply_to(message, "已批准貼圖：\n" + "\n".join(f"- {item.filename} tags={','.join(item.tags)}" for item in approved))
            except Exception as exc:
                self.bot.reply_to(message, f"批量批准貼圖失敗：{exc}")
            return True

        batch_reject_match = re.match(r"^(?:拒絕最近\s*(\d+|全部)?\s*(?:個)?(?:貼圖|表情包)|reject recent\s*(\d+|all)?\s*stickers?)\s*(.*)$", text, flags=re.IGNORECASE)
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

        latest_approve_match = re.match(r"^(?:批准最新貼圖|批准最新表情包|approve latest sticker)\s*(.*)$", text, flags=re.IGNORECASE)
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

        latest_reject_match = re.match(r"^(?:拒絕最新貼圖|拒絕最新表情包|reject latest sticker)\s*(.*)$", text, flags=re.IGNORECASE)
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

    def _enqueue_turn_part(self, part: InboundMessagePart) -> None:
        self.turn_coalescer.add(part, self._flush_aggregated_turn)

    def _flush_aggregated_turn(self, turn) -> None:
        message = turn.reply_message
        if not message:
            return
        try:
            if turn.mode in {InteractionMode.VISION_TASK, InteractionMode.TOOL_TASK}:
                self._maybe_quick_ack(message, turn.mode)
            prompt = build_turn_prompt(self.quote_context(message), turn)
            social_state = DEFAULT_SOCIAL_SESSION_MANAGER.observe_turn(
                turn.chat_id,
                text=turn.primary_text,
                has_sticker=bool(turn.stickers),
                has_photo=bool(turn.photos),
                mode=turn.mode.value,
            )
            suggestions: list[str] = []
            if social_state.mode != "idle":
                suggestions = DEFAULT_SOCIAL_SESSION_MANAGER.suggest_stickers(turn.chat_id, DEFAULT_SOCIAL_STICKER_INDEX, turn.primary_text)
                social_note = DEFAULT_SOCIAL_SESSION_MANAGER.build_prompt_note(turn.chat_id, suggestions)
                if social_note:
                    prompt = f"{prompt}\n\n{social_note}" if prompt else social_note
            social_policy = social_reply_policy_for(social_state.mode, social_state.intent_tags, has_sticker=bool(turn.stickers))
            allow_auto_sticker = social_policy.should_attach_sticker and social_state.mode != "idle"
            self._chat_and_reply(message, prompt, turn.mode, suggested_stickers=suggestions, allow_auto_sticker=allow_auto_sticker)
        except Exception as exc:
            try:
                self.bot.reply_to(message, f"處理合併訊息時出錯了：{exc}")
            except Exception:
                print(f"[TG warning] aggregated turn failed: {exc}")

    def register_handlers(self) -> None:
        @self.bot.message_handler(commands=["start", "help"])
        def send_welcome(message):
            self.remember_chat(message.chat.id)
            self.bot.reply_to(message, "喵，月月見已上線。")

        @self.bot.message_handler(content_types=["text"])
        def echo_all(message):
            self.remember_chat(message.chat.id)
            print(f"\n[TG text] {message.from_user.first_name}: {message.text}")
            try:
                if self._handle_sticker_curation_command(message):
                    return
                self.bot.send_chat_action(message.chat.id, "typing")
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
                self.bot.reply_to(message, f"嗚，處理訊息時出錯了：{exc}")

        @self.bot.message_handler(content_types=["photo"])
        def handle_photo(message):
            self.remember_chat(message.chat.id)
            print(f"\n[TG photo] {message.from_user.first_name}")
            try:
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
                self.bot.reply_to(message, f"圖片處理失敗：{exc}")

        @self.bot.message_handler(content_types=["sticker"])
        def handle_sticker(message):
            self.remember_chat(message.chat.id)
            print(f"\n[TG sticker] {message.from_user.first_name}")
            try:
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
                DEFAULT_MEDIA_CACHE.remember(filepath, media_type=media_type, short_caption=caption or "telegram sticker")
                DEFAULT_SOCIAL_STICKER_INDEX.catalog_incoming(filepath, media_type=media_type, metadata=sticker_meta)
                pending_count = len(DEFAULT_SOCIAL_STICKER_INDEX.list_candidates(limit=50))
                if DEFAULT_SOCIAL_CURATION_REMINDER.should_remind(message.chat.id, pending_count):
                    self.bot.reply_to(message, DEFAULT_SOCIAL_CURATION_REMINDER.message(pending_count), parse_mode="Markdown")
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
                self.bot.reply_to(message, f"貼圖處理失敗：{exc}")

    def start(self) -> None:
        print("\n>>> Telegram bot mode started.")
        self.bot.infinity_polling(allowed_updates=["message"], timeout=90, long_polling_timeout=90)


def build_agent() -> CompanionAgent:
    session_time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    current_history_file = os.path.join(HISTORY_DIR, f"{session_time}_ongoing.json")
    agent = CompanionAgent(SiliconFlowAdapter(model="deepseek-ai/DeepSeek-V4-Pro", thinking_level=THINKING_LEVEL), build_system_prompt(), current_history_file)
    for tool in ALL_TOOLS:
        agent.add_tool(tool)
    return agent


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

