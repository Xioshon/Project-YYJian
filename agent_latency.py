import hashlib
import json
import mimetypes
import os
import re
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from intent_router import is_sticker_send_request

ROOT_DIR = os.path.abspath(os.getenv("YUEYUE_ROOT_DIR") or os.path.dirname(__file__))
PROJECT_CACHE_DIR = os.path.join(ROOT_DIR, "workspace", "project_cache")
MEDIA_CACHE_FILE = os.path.join(PROJECT_CACHE_DIR, "media_cache.json")


class InteractionMode(StrEnum):
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
    "找 bug",
    "找bug",
    "跑一下",
    "部署",
    "跑自測",
    "跑自测",
    "run",
    "execute",
    "debug",
    # "use a command/instruction" is an unambiguous tool request on its own. Gap-battery evidence
    # (2026-07-15): 「用指令查一下這台機器的 Python 版本」 routed to CHAT because 查 (weak) found
    # no paired target word - and the chat model then HALLUCINATED a version number.
    "用指令",
    "用命令",
    "下指令",
    "跑指令",
    "跑個指令",
    "跑个指令",
]

# These are everyday words with heavy casual use ("測試驅動開發是什麼意思" is a plain question
# about a concept, not a task request; "驗證碼"/"身體檢查"/"執行力"/"修頭髮" are all ordinary chat
# with zero task intent). They only mean "go run/check/verify something for me" when paired with
# an explicit request phrase or a named controllable target - see TASK_TARGET_CONTEXT_MARKERS /
# TASK_REQUEST_PHRASE_MARKERS below, defined next to COMPUTER_ACTION_WEAK_MARKERS which needs the
# same co-occurrence gate for the same reason. Bare "test" lives here too, not in the strong list
# above: as a 4-letter substring it silently matches ordinary words like "fastest"/"contest"/
# "protest"/"detest", so on its own it must not be enough to route into tool_task.
TOOL_INTENT_WEAK_MARKERS = [
    "執行",
    "执行",
    "修",
    "驗證",
    "验证",
    "測試",
    "测试",
    "檢查",
    "检查",
    "啟動",
    "启动",
    "重啟",
    "重启",
    "test",
    # Natural "go look something up for me" verbs. Live-test evidence: 「幫我查一下C:\Agent這個
    # 資料夾裡現在有幾個Python檔案」 and 「幫我讀取一下...資料夾」 both fell through to CHAT because
    # none of these verbs were recognized, so the model replied with an empty promise ("行，我去看
    # 一眼") it structurally could not keep - CHAT's tool allowlist has no file tools. Weak, not
    # strong: 「我查過了沒這回事」/「你去读取一下我的心」 are ordinary chat, so they still need the
    # 幫我/named-target co-occurrence gate.
    "查",
    "讀取",
    "读取",
    "統計",
    "统计",
    "數一下",
    "数一下",
    # Search verbs - weak because 「我搜尋了一下發現…」is casual; a real request pairs them with
    # a target or 幫我. Consolidated here so classify_interaction is the single canonical mode
    # router that yueyue_v3.classify_turn_mode delegates to (previously classify_turn_mode had
    # 搜尋/搜索 as its own standalone markers, which is exactly the drift being removed).
    "搜尋",
    "搜索",
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
    "找 bug",
    "找bug",
    "跑一下",
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
SCREEN_OBSERVE_MARKERS.extend([
    "截圖", "截图", "截屏", "螢幕", "屏幕", "畫面", "画面", "看狀態", "看状态", "看看狀態", "看看状态",
    "看一下螢幕", "看一下屏幕", "電腦屏幕", "电脑屏幕", "桌面狀態", "桌面状态",
])
# These describe specific keyboard/UI mechanics with no everyday metaphorical use ("press the
# spacebar once" is never a figure of speech), so they stay safe to trigger standalone.
COMPUTER_ACTION_MARKERS = [
    "停止播放", "按一下", "按下", "快捷鍵", "快捷键", "熱鍵", "热键", "空格", "space",
]

# These verbs are everyday words used constantly in ordinary chat with no device/app in sight
# ("我打開音樂在聽" - I turned some music on, not a request to control anything; "關閉了心裡的門"
# - a figure of speech). They only mean "control my computer" when either a controllable target is
# named or the owner is actually asking for the action, so this gets gated behind that co-occurrence
# instead of firing on the bare verb alone.
# Only genuine ACTION verbs belong here. Nouns that name a controllable thing (設定/菜單/視窗/
# 窗口) live in TASK_TARGET_CONTEXT_MARKERS instead - listing a noun in both lists let a single
# word like 「設定」 self-satisfy the "action + target" co-occurrence gate, so a casual
# 「把你的設定精簡了」 wrongly routed to a task (live bug 2026-07-14). An action word plus a
# separately-named target is what signals a real request.
COMPUTER_ACTION_WEAK_MARKERS = [
    "暫停", "暂停", "播放", "點擊", "点击", "輸入", "输入", "控制", "切換", "切换", "關閉", "关闭",
    "打開", "打开", "導航", "导航", "按",
]

TASK_TARGET_CONTEXT_MARKERS = [
    "設定", "设置", "按鈕", "按钮", "視窗", "窗口", "頁面", "页面", "選單", "选单", "菜單", "菜单",
    "瀏覽器", "浏览器", "chrome", "edge", "檔案", "文件", "資料夾", "文件夹", "終端", "终端",
    "terminal", "powershell", "cmd", "codex", "app", "程式", "程序", "電腦", "电脑", "螢幕",
    "屏幕", "軟件", "软件", "軟體",
    # Gap-battery additions: 「這台機器」/「版本」/「腳本」/bare "python" are controllable-target
    # nouns a weak intent verb (查/執行/檢查) legitimately pairs with; without them,
    # 「用指令查一下這台機器的 Python 版本」 style requests fell through to chat.
    "指令", "命令", "版本", "機器", "机器", "python", "腳本", "脚本", "script",
]

# An explicit ask ("幫我"/"可以...嗎"/"請") is just as valid a signal as naming a specific app or
# window - "幫我暫停播放" has no app/window in it at all, but is still clearly a real request.
TASK_REQUEST_PHRASE_MARKERS = [
    "幫我", "帮我", "可以幫", "可以帮", "請幫", "请帮", "麻煩", "麻烦", "請你", "请你",
]

# Clean UTF-8 routing markers added at the final definition site. These are the
# markers normal runtime should rely on; older mojibake entries above are only
# kept so old traces/tests do not lose compatibility.
VISION_INTENT_MARKERS.extend([
    "看圖", "看图", "分析", "辨識", "识别", "這是什麼", "这是什么", "幫我看", "帮我看",
    "圖片", "图片", "影片", "視頻", "视频", "畫面", "画面",
])
SCREEN_OBSERVE_MARKERS.extend([
    "截圖", "截图", "截屏", "螢幕", "屏幕", "畫面", "画面", "看看狀態", "看看状态",
    "看一下螢幕", "看一下屏幕", "電腦屏幕", "电脑屏幕", "桌面狀態", "桌面状态",
])


# Sticker-send detection now lives in a single shared place: intent_router.is_sticker_send_request.
# It used to be duplicated here with a separately-drifted marker set (and duplicated again in the
# V5 wrapper below), which let the two implementations disagree on borderline input. Route through
# the shared function so there is exactly one definition of "is this a sticker-send request" for
# both turn classification (here) and reply-intent classification (intent_router.classify_owner_intent).

CHAT_SAFE_TOOLS = ["search_sticker", "send_telegram_media", "inspect_url", "read_url_context"]
SOCIAL_STICKER_TOOLS = ["search_sticker", "send_telegram_media"]
VISION_TOOLS = ["analyze_media", "capture_screen", "read_file", "search_sticker", "send_telegram_media", "inspect_url", "read_url_context"]
SCREEN_OBSERVE_TOOLS = [
    "get_screen_ui",
    "capture_screen",
    "analyze_media",
    "list_windows",
    "focus_window",
    "click_screen",
    "click_ui_element",
    "press_hotkey",
    "type_keyboard",
    "read_file",
    "list_files",
    "search_in_files",
    "search_sticker",
    "send_telegram_media",
    "execute_command",
    "execute_python",
]


def classify_interaction(text: str = "", has_media: bool = False, media_kind: str = "") -> InteractionMode:
    normalized = (text or "").casefold()
    if has_media and any(marker.casefold() in normalized for marker in VISION_INTENT_MARKERS):
        return InteractionMode.VISION_TASK
    # An actual sticker/animation attachment is a certain signal - settle it here, before any
    # text-heuristic below gets a chance to misroute on an unrelated word in the caption (e.g. a
    # sticker sent with the caption "debug this" should stay social, not fall into tool_task).
    if has_media and media_kind in {"sticker", "animation", "video_sticker"}:
        return InteractionMode.SOCIAL_STICKER
    if _looks_like_observe_only_query(normalized):
        return InteractionMode.SCREEN_OBSERVE
    if is_sticker_send_request(text):
        return InteractionMode.SOCIAL_STICKER
    if any(marker.casefold() in normalized for marker in COMPUTER_ACTION_MARKERS):
        return InteractionMode.TOOL_TASK
    if any(marker.casefold() in normalized for marker in COMPUTER_ACTION_WEAK_MARKERS) and (
        any(marker.casefold() in normalized for marker in TASK_TARGET_CONTEXT_MARKERS)
        or any(marker.casefold() in normalized for marker in TASK_REQUEST_PHRASE_MARKERS)
    ):
        return InteractionMode.TOOL_TASK
    if any(marker.casefold() in normalized for marker in SCREEN_OBSERVE_MARKERS):
        return InteractionMode.SCREEN_OBSERVE
    if any(marker.casefold() in normalized for marker in TOOL_INTENT_MARKERS):
        return InteractionMode.TOOL_TASK
    if any(marker.casefold() in normalized for marker in TOOL_INTENT_WEAK_MARKERS) and (
        any(marker.casefold() in normalized for marker in TASK_TARGET_CONTEXT_MARKERS)
        or any(marker.casefold() in normalized for marker in TASK_REQUEST_PHRASE_MARKERS)
    ):
        return InteractionMode.TOOL_TASK
    if has_media:
        return InteractionMode.VISION_TASK if any(marker.casefold() in normalized for marker in VISION_INTENT_MARKERS) else InteractionMode.CHAT
    return InteractionMode.CHAT


def classification_is_grey(text: str, mode: InteractionMode, has_media: bool = False, media_kind: str = "") -> bool:
    """True when the keyword decision above rested on WEAK signals - the historical misroute zone.

    Two grey shapes, one per direction:
    - decided CHAT while weak intent verbs / task-target nouns were present (the 「用指令查版本」
      class: fell to chat, then the chat model hallucinated an answer);
    - decided TOOL_TASK purely via the weak co-occurrence gate (the 「設定精簡」 class: a casual
      mention self-satisfied verb+target and hijacked a chat into a workflow).
    Certain signals (media/sticker attachments, strong action/tool markers) are never grey.
    An LLM router may then re-judge grey cases; the keyword result stays the fallback."""
    if has_media or media_kind:
        return False
    normalized = (text or "").casefold()
    if any(marker.casefold() in normalized for marker in COMPUTER_ACTION_MARKERS):
        return False
    if any(marker.casefold() in normalized for marker in TOOL_INTENT_MARKERS):
        return False
    weak_signals = any(
        marker.casefold() in normalized
        for marker in (*COMPUTER_ACTION_WEAK_MARKERS, *TOOL_INTENT_WEAK_MARKERS, *TASK_TARGET_CONTEXT_MARKERS)
    )
    if mode == InteractionMode.CHAT:
        return weak_signals
    # TOOL_TASK here was reached only via the weak co-occurrence gate (strong paths returned
    # False above); other modes (observe/vision/sticker) are considered certain.
    return mode == InteractionMode.TOOL_TASK


def _looks_like_observe_only_query(text: str) -> bool:
    # These verbs already imply an active "go check something" request, so they are
    # safe to trigger observe-mode on their own.
    strong_observe_markers = [
        "看一下", "幫我看", "帮我看", "確認", "确认", "檢查一下", "检查一下",
    ]
    # These are bare grammatical particles or everyday phrases that show up constantly in
    # ordinary chat ("你是不是覺得我很煩", "你現在心情點呀", "有沒有無聊", "發個表情包來看看").
    # Live-test evidence: a message containing a casual 「有沒有」 got misrouted into a real
    # screenshot-taking loop. They only mean "check the screen/task" when paired with an
    # actual screen/device/task-state word - otherwise they false-positive normal
    # conversation into a tool-calling loop.
    weak_observe_markers = ["是否", "是不是", "現在", "现在", "狀態", "状态", "有沒有", "有没有", "看看"]
    screen_context_markers = [
        "螢幕", "屏幕", "萤幕", "畫面", "画面", "電腦", "电脑", "截圖", "截图", "截屏",
        "視窗", "窗口", "程式", "程序", "軟件", "软件", "軟體", "任務", "任务",
        "播放", "進度", "进度", "桌面",
    ]
    has_strong = any(marker in text for marker in strong_observe_markers)
    has_weak_with_context = any(marker in text for marker in weak_observe_markers) and any(
        marker in text for marker in screen_context_markers
    )
    if not (has_strong or has_weak_with_context):
        return False
    navigation_context = [
        "設定", "设置", "菜單", "菜单", "選單", "选单", "二級", "二级", "頁面", "页面",
        "分頁", "分页", "tab", "sidebar", "側欄", "侧栏", "進入", "进入", "裡面", "里面",
        "導航", "导航", "找到", "切到", "切換到", "切换到",
    ]
    if any(marker in text for marker in navigation_context):
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
            max_tool_iterations=12,
            allow_vision=True,
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
    # Every incoming photo/sticker adds an entry; without a cap this grows for the
    # lifetime of the deployment (memory and media_cache.json both).
    MAX_ENTRIES = 500

    def __init__(self, path: str = MEDIA_CACHE_FILE):
        self.path = path
        self.entries: dict[str, MediaCacheEntry] = {}
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            self.entries = {}
            return
        try:
            with open(self.path, encoding="utf-8") as file:
                data = json.load(file)
            self.entries = {key: MediaCacheEntry(**value) for key, value in data.items()}
        except Exception:
            self.entries = {}

    def save(self) -> None:
        if len(self.entries) > self.MAX_ENTRIES:
            kept = sorted(self.entries.values(), key=lambda item: item.created_at)[-self.MAX_ENTRIES :]
            self.entries = {item.file_hash: item for item in kept}
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
