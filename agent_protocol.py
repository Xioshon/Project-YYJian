import re
from dataclasses import dataclass


# Keep protocol strings here as Unicode escapes where useful. This prevents
# Windows console/code-page mojibake from becoming runtime behavior.

STICKER_MARKER_LABEL = "\u8868\u60c5\u5305"
SCREENSHOT_MARKER_LABEL = "\u7cfb\u7d71\u622a\u5716"

SINGLE_APPROVAL_PHRASES = [
    "\u53ef\u4ee5",
    "\u597d",
    "\u597d\u554a",
    "\u5141\u8a31",
    "\u5141\u8bb8",
    "\u540c\u610f",
    "\u6388\u6b0a",
    "\u6388\u6743",
    "\u57f7\u884c",
    "\u6267\u884c",
    "\u7e7c\u7e8c",
    "\u7ee7\u7eed",
    "\u55ef",
    "\u884c",
    "\u8dd1\u5427",
    "\u505a\u5427",
    "\u6309\u5427",
    "ok",
    "yes",
    "y",
    "go",
    "continue",
]

TURN_APPROVAL_PHRASES = [
    "\u672c\u8f2a\u5141\u8a31",
    "\u672c\u8f6e\u5141\u8bb8",
    "\u9019\u6b21\u5168\u90e8\u53ef\u4ee5",
    "\u8fd9\u6b21\u5168\u90e8\u53ef\u4ee5",
    "\u5168\u90e8\u53ef\u4ee5",
    "\u5168\u6b0a\u4ea4\u7d66\u4f60",
    "\u5168\u6743\u4ea4\u7ed9\u4f60",
    "\u4e00\u9375\u5141\u8a31",
    "\u4e00\u952e\u5141\u8bb8",
    "\u4e00\u9375\u6388\u6b0a",
    "\u4e00\u952e\u6388\u6743",
    "\u672c\u6b21\u5141\u8bb8",
    "\u672c\u6b21\u5141\u8a31",
    "\u8fd9\u8f6e\u5141\u8bb8",
    "\u9019\u8f2a\u5141\u8a31",
    "global allow",
    "globalallow",
    "allow all",
    "allowall",
]

DENY_PHRASES = [
    "\u4e0d\u8981",
    "\u4e0d\u53ef\u4ee5",
    "\u62d2\u7d55",
    "\u53d6\u6d88",
    "\u505c",
    "\u7b97\u4e86",
    "no",
    "n",
]

FAIL_SAFE_REPLY = "\u4e3b\u4eba\uff0c\u6211\u5075\u6e2c\u5230 fail-safe\uff0c\u5df2\u7d93\u7acb\u523b\u505c\u6b62\u6240\u6709\u64cd\u4f5c\u3002"
EMPTY_REPLY_FALLBACK = "\u4e3b\u4eba\uff0c\u6211\u5df2\u7d93\u8655\u7406\u5b8c\u4e86\u3002"
TOOL_LOOP_TIMEOUT_REPLY = "\u4e3b\u4eba\uff0c\u6211\u5361\u5728\u5de5\u5177\u8ff4\u5708\u88e1\u4e86\uff0c\u5df2\u505c\u6b62\u672c\u8f2a\u64cd\u4f5c\u3002"

PRIMARY_MESSAGE_LABELS = [
    "\u4e3b\u4eba\u4e3b\u8981\u8a0a\u606f\uff1a",
    "\u4e3b\u4eba\u4e3b\u8981\u4fe1\u606f\uff1a",
    "\u4e3b\u4eba\u4e3b\u8981\u8a0a\u606f:",
    "\u4e3b\u4eba\u4e3b\u8981\u4fe1\u606f:",
    "\u4e3b\u8981\u8a0a\u606f\uff1a",
    "\u4e3b\u8981\u4fe1\u606f\uff1a",
    "Owner primary message:",
]

TELEGRAM_STATUS_LABELS = {
    "get_screen_ui": "\u6b63\u5728\u89e3\u6790\u87a2\u5e55...",
    "click_ui_element": "\u6b63\u5728\u9ede\u64ca\u4ecb\u9762...",
    "type_keyboard": "\u6b63\u5728\u8f38\u5165\u6587\u5b57...",
    "press_hotkey": "\u6b63\u5728\u6309\u5feb\u6377\u9375...",
    "execute_command": "\u6b63\u5728\u57f7\u884c\u7cfb\u7d71\u547d\u4ee4...",
    "execute_python": "\u6b63\u5728\u57f7\u884c Python...",
    "analyze_media": "\u6b63\u5728\u5206\u6790\u5716\u7247...",
    "send_telegram_media": "\u6b63\u5728\u767c\u9001\u5a92\u9ad4...",
    "react_to_message": "\u6b63\u5728\u52a0\u5165 reaction...",
    "list_files": "\u6b63\u5728\u8b80\u53d6\u6a94\u6848\u5217\u8868...",
}


def sticker_marker(filename: str) -> str:
    return f"[{STICKER_MARKER_LABEL}: {filename}]"


def screenshot_marker(filename: str) -> str:
    return f"[{SCREENSHOT_MARKER_LABEL}: {filename}]"


def screenshot_tags(filename: str) -> str:
    return f"{screenshot_marker(filename)} [screenshot: {filename}]"


def sticker_pattern() -> re.Pattern:
    return re.compile(rf"\[(?:{re.escape(STICKER_MARKER_LABEL)}|sticker):\s*(.*?)\]", re.IGNORECASE)


def screenshot_pattern() -> re.Pattern:
    return re.compile(rf"\[(?:{re.escape(SCREENSHOT_MARKER_LABEL)}|screenshot):\s*(.*?)\]", re.IGNORECASE)


def classify_approval(text: str, has_pending: bool) -> str:
    if not has_pending:
        return "none"
    for candidate in _approval_text_candidates(text):
        normalized = _normalize_approval_text(candidate)
        if not normalized or len(normalized) > 120:
            continue
        if any(marker.casefold() in normalized for marker in DENY_PHRASES):
            return "deny"
        if any(marker.casefold() in normalized for marker in TURN_APPROVAL_PHRASES):
            return "turn"
        if any(marker.casefold() == normalized or marker.casefold() in normalized for marker in SINGLE_APPROVAL_PHRASES):
            return "single"
        if _looks_like_turn_approval(normalized):
            return "turn"
        if _looks_like_single_approval(normalized):
            return "single"
    return "none"


def extract_primary_message(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    for label in PRIMARY_MESSAGE_LABELS:
        if label in raw:
            primary = raw.split(label, 1)[1].splitlines()[0].strip()
            if primary:
                return primary
    return raw


def _approval_text_candidates(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    candidates: list[str] = []
    labels = [
        "主人主要訊息：",
        "主人主要信息：",
        "主人主要訊息:",
        "主人主要信息:",
        "主要訊息：",
        "主要信息：",
        "Owner primary message:",
    ]
    for label in labels:
        if label in raw:
            primary = raw.split(label, 1)[1].splitlines()[0].strip()
            if primary:
                candidates.append(primary)
    first_line = raw.splitlines()[0].strip()
    if first_line:
        candidates.append(first_line)
    candidates.append(raw)
    deduped: list[str] = []
    for item in candidates:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def _normalize_approval_text(text: str) -> str:
    normalized = (text or "").strip().casefold()
    normalized = re.sub(r"[\s　]+", "", normalized)
    normalized = normalized.replace("輪", "轮").replace("許", "许").replace("權", "权").replace("續", "续").replace("這", "这")
    normalized = normalized.replace("執", "执").replace("給", "给")
    return normalized


def _looks_like_turn_approval(text: str) -> bool:
    turn_scope = ("本轮", "这轮", "这一轮", "本次", "这次", "整轮", "全程", "全部", "都")
    allow_action = ("允许", "授权", "可以", "同意", "放行", "继续", "执行", "跑", "做")
    return any(scope in text for scope in turn_scope) and any(action in text for action in allow_action)


def _looks_like_single_approval(text: str) -> bool:
    approval_patterns = [
        r"^(嗯+|唔+|好+|行+|可以+|ok+|yes+|y+)[啦喔哦啊吧呀]*$",
        r"^(继续|继续吧|接着|接着来|跑吧|做吧|执行吧|按吧|按下去|点吧|去吧)$",
        r"(你|月月)?(继续|接着|跑|做|执行|按|点)(吧|下去|一下|好了)?$",
        r"(可以了|允许了|同意了|授权了|放行吧|继续做|继续执行)",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in approval_patterns)


@dataclass(frozen=True)
class ProtocolMarkers:
    sticker_label: str = STICKER_MARKER_LABEL
    screenshot_label: str = SCREENSHOT_MARKER_LABEL
    single_approval: tuple[str, ...] = tuple(SINGLE_APPROVAL_PHRASES)
    turn_approval: tuple[str, ...] = tuple(TURN_APPROVAL_PHRASES)
    deny: tuple[str, ...] = tuple(DENY_PHRASES)
