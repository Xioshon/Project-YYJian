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
    "\u540c\u610f",
    "\u6388\u6b0a",
    "\u57f7\u884c",
    "\u7e7c\u7e8c",
    "ok",
    "yes",
    "y",
]

TURN_APPROVAL_PHRASES = [
    "\u672c\u8f2a\u5141\u8a31",
    "\u9019\u6b21\u5168\u90e8\u53ef\u4ee5",
    "\u5168\u90e8\u53ef\u4ee5",
    "\u5168\u6b0a\u4ea4\u7d66\u4f60",
    "\u4e00\u9375\u5141\u8a31",
    "\u4e00\u9375\u6388\u6b0a",
    "global allow",
    "allow all",
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
    normalized = (text or "").strip().casefold()
    if not normalized or len(normalized) > 120:
        return "none"
    if any(marker.casefold() in normalized for marker in DENY_PHRASES):
        return "deny"
    if any(marker.casefold() in normalized for marker in TURN_APPROVAL_PHRASES):
        return "turn"
    if any(marker.casefold() == normalized or marker.casefold() in normalized for marker in SINGLE_APPROVAL_PHRASES):
        return "single"
    return "none"


@dataclass(frozen=True)
class ProtocolMarkers:
    sticker_label: str = STICKER_MARKER_LABEL
    screenshot_label: str = SCREENSHOT_MARKER_LABEL
    single_approval: tuple[str, ...] = tuple(SINGLE_APPROVAL_PHRASES)
    turn_approval: tuple[str, ...] = tuple(TURN_APPROVAL_PHRASES)
    deny: tuple[str, ...] = tuple(DENY_PHRASES)
