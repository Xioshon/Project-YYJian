import hashlib
import json
import mimetypes
import os
import re
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

ROOT_DIR = os.path.abspath(os.getenv("YUEYUE_ROOT_DIR") or os.path.dirname(__file__))
PROJECT_CACHE_DIR = os.path.join(ROOT_DIR, "workspace", "project_cache")
MEDIA_CACHE_FILE = os.path.join(PROJECT_CACHE_DIR, "media_cache.json")


class InteractionMode(str, Enum):
    CHAT = "chat"
    SOCIAL_STICKER = "social_sticker"
    VISION_TASK = "vision_task"
    TOOL_TASK = "tool_task"
    SCREEN_OBSERVE = "screen_observe"


@dataclass
class ResponsePolicy:
    max_tool_iterations: int = 25
    allow_vision: bool = True
    allow_sticker: bool = True
    progress_style: str = "normal"
    route: str = "tool_task"
    allowed_tools: list[str] | None = None
    max_repeated_tool_calls: int = 1
    max_self_repair_attempts: int = 1


@dataclass
class InteractionRoute:
    mode: InteractionMode
    reason: str = ""
    needs_tools: bool = False
    max_steps: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "reason": self.reason, "needs_tools": self.needs_tools, "max_steps": self.max_steps}


@dataclass
class ToolLoopBudget:
    route: str
    max_iterations: int
    allowed_tools: list[str]
    max_repeated_tool_calls: int = 1

    def allows(self, tool_name: str) -> bool:
        return not self.allowed_tools or tool_name in self.allowed_tools


@dataclass
class MediaCacheEntry:
    file_hash: str
    path: str
    media_type: str
    short_caption: str = ""
    vision_summary: str = ""
    created_at: float = 0.0


VISION_INTENT_MARKERS = [
    "看圖",
    "看图",
    "分析",
    "辨識",
    "识别",
    "這是什麼",
    "这是什么",
    "是什麼",
    "是什么",
    "幫我看",
    "帮我看",
    "看看",
    "analyze",
    "what is this",
    "describe",
]

TOOL_INTENT_MARKERS = [
    "執行",
    "执行",
    "修",
    "驗證",
    "验证",
    "測試",
    "测试",
    "找 bug",
    "找bug",
    "跑一下",
    "檢查",
    "检查",
    "啟動",
    "启动",
    "重啟",
    "重启",
    "部署",
    "跑自測",
    "跑自测",
    "run",
    "execute",
    "debug",
    "test",
]

SCREEN_OBSERVE_MARKERS = [
    "截圖",
    "截图",
    "截屏",
    "螢幕",
    "萤幕",
    "屏幕",
    "畫面",
    "画面",
    "看狀態",
    "看状态",
    "看看狀態",
    "看看状态",
    "看一下狀態",
    "看一下状态",
    "看一下螢幕",
    "看一下屏幕",
    "電腦屏幕",
    "电脑屏幕",
    "電腦螢幕",
    "电脑萤幕",
    "桌面狀態",
    "桌面状态",
    "screen",
    "screenshot",
]

QUICK_ACKS = {
    InteractionMode.VISION_TASK: "我先看一下～",
    InteractionMode.TOOL_TASK: "我先處理一下～",
    InteractionMode.SOCIAL_STICKER: "收到～",
    InteractionMode.SCREEN_OBSERVE: "我看一下畫面～",
}

VISION_INTENT_MARKERS.extend([
    "看图",
    "看圖",
    "分析",
    "辨识",
    "辨識",
    "识别",
    "識別",
    "这是什么",
    "這是什麼",
    "是什么",
    "是什麼",
    "帮我看",
    "幫我看",
    "看看",
    "看一下",
    "影片",
    "视频",
    "視頻",
])
TOOL_INTENT_MARKERS.extend([
    "执行",
    "執行",
    "修",
    "验证",
    "驗證",
    "测试",
    "測試",
    "找 bug",
    "找bug",
    "跑一下",
    "检查",
    "檢查",
    "启动",
    "啟動",
    "重启",
    "重啟",
    "部署",
    "跑自测",
    "跑自測",
])
SCREEN_OBSERVE_MARKERS.extend([
    "截图",
    "截圖",
    "截屏",
    "屏幕",
    "螢幕",
    "萤幕",
    "画面",
    "畫面",
    "看状态",
    "看狀態",
    "看看状态",
    "看看狀態",
    "看一下状态",
    "看一下狀態",
    "看一下屏幕",
    "看一下螢幕",
    "电脑屏幕",
    "電腦螢幕",
    "桌面状态",
    "桌面狀態",
])
QUICK_ACKS.update({
    InteractionMode.VISION_TASK: "我先看一下～",
    InteractionMode.TOOL_TASK: "我先处理一下～",
    InteractionMode.SOCIAL_STICKER: "收到～",
    InteractionMode.SCREEN_OBSERVE: "我看一下画面～",
})

# Canonical Unicode-safe routing markers. These override the older mojibake
# lists above for normal runtime classification.
VISION_INTENT_MARKERS.extend([
    "看圖", "看图", "分析", "辨識", "识别", "這是什麼", "这是什么", "幫我看", "帮我看", "圖片", "图片", "影片", "視頻", "视频",
])
TOOL_INTENT_MARKERS.extend([
    "執行", "执行", "修", "測試", "测试", "驗證", "验证", "檢查", "检查", "啟動", "启动", "重啟", "重启",
    "暫停", "暂停", "播放", "停止播放", "按一下", "按下", "快捷鍵", "快捷键", "熱鍵", "热键", "空格", "space",
    "點擊", "点击", "輸入", "输入", "控制", "切換", "切换", "打開", "打开", "關閉", "关闭",
])
SCREEN_OBSERVE_MARKERS.extend([
    "截圖", "截图", "截屏", "螢幕", "屏幕", "畫面", "画面", "看狀態", "看状态", "看看狀態", "看看状态",
    "看一下螢幕", "看一下屏幕", "電腦屏幕", "电脑屏幕", "桌面狀態", "桌面状态",
])
COMPUTER_ACTION_MARKERS = [
    "暫停", "暂停", "播放", "停止播放", "按一下", "按下", "快捷鍵", "快捷键", "熱鍵", "热键", "空格", "space",
    "點擊", "点击", "輸入", "输入", "控制", "切換", "切换", "關閉", "关闭",
]

# Clean UTF-8 routing markers added at the final definition site. These are the
# markers normal runtime should rely on; older mojibake entries above are only
# kept so old traces/tests do not lose compatibility.
VISION_INTENT_MARKERS.extend([
    "看圖", "看图", "分析", "辨識", "识别", "這是什麼", "这是什么", "幫我看", "帮我看",
    "圖片", "图片", "影片", "視頻", "视频", "畫面", "画面",
])
TOOL_INTENT_MARKERS.extend([
    "執行", "执行", "驗證", "验证", "測試", "测试", "檢查", "检查", "啟動", "启动",
    "重啟", "重启", "暫停", "暂停", "播放", "停止播放", "按一下", "按下",
    "快捷鍵", "快捷键", "熱鍵", "热键", "空格", "點擊", "点击", "輸入", "输入",
    "控制", "切換", "切换", "打開", "打开", "關閉", "关闭", "導航", "导航",
    "設定", "设置", "菜單", "菜单", "二級菜單", "二级菜单",
])
SCREEN_OBSERVE_MARKERS.extend([
    "截圖", "截图", "截屏", "螢幕", "屏幕", "畫面", "画面", "看看狀態", "看看状态",
    "看一下螢幕", "看一下屏幕", "電腦屏幕", "电脑屏幕", "桌面狀態", "桌面状态",
])
COMPUTER_ACTION_MARKERS.extend([
    "打開", "打开", "導航", "导航", "設定", "设置", "菜單", "菜单", "二級菜單",
    "二级菜单", "窗口", "視窗", "按", "關閉", "关闭",
])

CHAT_SAFE_TOOLS = ["search_sticker", "send_telegram_media", "search_knowledge", "read_knowledge", "inspect_url", "read_url_context"]
SOCIAL_STICKER_TOOLS = ["search_sticker", "send_telegram_media"]
VISION_TOOLS = ["analyze_media", "read_file", "search_sticker", "send_telegram_media", "inspect_url", "read_url_context"]
SCREEN_OBSERVE_TOOLS = [
    "get_screen_ui",
    "read_file",
    "list_files",
    "search_in_files",
    "search_knowledge",
    "read_knowledge",
    "search_sticker",
    "send_telegram_media",
    "execute_command",
    "execute_python",
]


def classify_interaction(text: str = "", has_media: bool = False, media_kind: str = "") -> InteractionMode:
    normalized = (text or "").casefold()
    if has_media and any(marker.casefold() in normalized for marker in VISION_INTENT_MARKERS):
        return InteractionMode.VISION_TASK
    if _looks_like_observe_only_query(normalized):
        return InteractionMode.SCREEN_OBSERVE
    if any(marker.casefold() in normalized for marker in COMPUTER_ACTION_MARKERS):
        return InteractionMode.TOOL_TASK
    if any(marker.casefold() in normalized for marker in SCREEN_OBSERVE_MARKERS):
        return InteractionMode.SCREEN_OBSERVE
    if any(marker.casefold() in normalized for marker in TOOL_INTENT_MARKERS):
        return InteractionMode.TOOL_TASK
    if has_media and media_kind in {"sticker", "animation", "video_sticker"}:
        return InteractionMode.SOCIAL_STICKER
    if has_media:
        return InteractionMode.VISION_TASK if any(marker.casefold() in normalized for marker in VISION_INTENT_MARKERS) else InteractionMode.CHAT
    return InteractionMode.CHAT


def _looks_like_observe_only_query(text: str) -> bool:
    observe_markers = [
        "看一下", "看看", "幫我看", "帮我看", "確認", "确认", "檢查一下", "检查一下",
        "有沒有", "有没有", "是否", "是不是", "現在", "现在", "狀態", "状态",
    ]
    if not any(marker in text for marker in observe_markers):
        return False
    action_phrases = [
        "幫我按", "帮我按", "幫我點", "帮我点", "幫我點擊", "帮我点击", "按下", "按一下",
        "點擊", "点击", "關閉", "关闭", "打開", "打开", "切換", "切换", "輸入", "输入",
        "幫我暫停", "帮我暂停", "暫停播放", "暂停播放", "停止播放", "播放一下",
    ]
    return not any(phrase in text for phrase in action_phrases)


def policy_for_semantic_intent(intent: str, fallback: ResponsePolicy | None = None) -> ResponsePolicy:
    """Map natural turn intent to a tool budget without exposing modes to users."""
    normalized = (intent or "").casefold()
    if fallback is not None and fallback.route not in {"", "chat"}:
        return fallback
    if normalized in {"permission_reply", "permission_granted", "task_continuation", "task_followup"}:
        return ResponsePolicy(max_tool_iterations=12, allow_vision=True, allow_sticker=True, progress_style="normal", route="task_continuation", max_repeated_tool_calls=2)
    if normalized in {"task", "tool_task", "active_task"}:
        return ResponsePolicy(max_tool_iterations=25, allow_vision=True, allow_sticker=True, progress_style="normal", route="tool_task", max_repeated_tool_calls=2)
    if normalized in {"screen_observe", "screen"}:
        return response_policy_for(InteractionMode.SCREEN_OBSERVE)
    if normalized in {"vision_task", "vision"}:
        return response_policy_for(InteractionMode.VISION_TASK)
    if fallback is not None:
        return fallback
    return response_policy_for(InteractionMode.CHAT)


def response_policy_for(mode: InteractionMode) -> ResponsePolicy:
    if mode == InteractionMode.CHAT:
        return ResponsePolicy(max_tool_iterations=2, allow_vision=False, allow_sticker=True, progress_style="quiet", route=mode.value, allowed_tools=CHAT_SAFE_TOOLS)
    if mode == InteractionMode.SOCIAL_STICKER:
        return ResponsePolicy(max_tool_iterations=2, allow_vision=False, allow_sticker=True, progress_style="quiet", route=mode.value, allowed_tools=SOCIAL_STICKER_TOOLS)
    if mode == InteractionMode.VISION_TASK:
        return ResponsePolicy(max_tool_iterations=8, allow_vision=True, allow_sticker=True, progress_style="quick_ack", route=mode.value, allowed_tools=VISION_TOOLS)
    if mode == InteractionMode.SCREEN_OBSERVE:
        return ResponsePolicy(
            max_tool_iterations=6,
            allow_vision=False,
            allow_sticker=True,
            progress_style="quick_ack",
            route=mode.value,
            allowed_tools=SCREEN_OBSERVE_TOOLS,
            max_repeated_tool_calls=1,
        )
    return ResponsePolicy(max_tool_iterations=25, allow_vision=True, allow_sticker=True, progress_style="normal", route=mode.value, max_repeated_tool_calls=2)


def quick_ack_for(mode: InteractionMode) -> str:
    return QUICK_ACKS.get(mode, "")


def media_type_for(path: str) -> str:
    ext = os.path.splitext(path or "")[1].lower()
    if ext in {".tgs", ".webm", ".mp4"}:
        return "video_sticker"
    mime = mimetypes.guess_type(path)[0] or ""
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    return "unknown"


def file_hash(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MediaCache:
    def __init__(self, path: str = MEDIA_CACHE_FILE):
        self.path = path
        self.entries: dict[str, MediaCacheEntry] = {}
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            self.entries = {}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
            self.entries = {key: MediaCacheEntry(**value) for key, value in data.items()}
        except Exception:
            self.entries = {}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as file:
            json.dump({key: asdict(value) for key, value in self.entries.items()}, file, ensure_ascii=False, indent=2)

    def remember(self, path: str, media_type: str = "", short_caption: str = "") -> MediaCacheEntry:
        key = file_hash(path)
        entry = self.entries.get(key) or MediaCacheEntry(
            file_hash=key,
            path=path,
            media_type=media_type or media_type_for(path),
            created_at=time.time(),
        )
        entry.path = path
        if media_type:
            entry.media_type = media_type
        if short_caption:
            entry.short_caption = short_caption
        self.entries[key] = entry
        self.save()
        return entry

    def get_by_path(self, path: str) -> MediaCacheEntry | None:
        try:
            return self.entries.get(file_hash(path))
        except Exception:
            return None

    def set_vision_summary(self, path: str, summary: str) -> MediaCacheEntry:
        entry = self.remember(path)
        entry.vision_summary = summarize_vision_text(summary)
        self.entries[entry.file_hash] = entry
        self.save()
        return entry


def summarize_vision_text(text: str, max_chars: int = 700) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_chars:
        return text
    sentences = re.split(r"(?<=[.!?。！？])\s+", text)
    output = ""
    for sentence in sentences:
        if len(output) + len(sentence) + 1 > max_chars:
            break
        output = (output + " " + sentence).strip()
    return output or text[:max_chars]


DEFAULT_MEDIA_CACHE = MediaCache()
