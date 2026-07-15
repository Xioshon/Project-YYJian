
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


@dataclass(frozen=True)
class Intent:
    kind: str
    confidence: float
    reason: str


def normalize_owner_text(text: str) -> str:
    s = str(text or "")
    s = unicodedata.normalize("NFKC", s)

    replacements = {
        chr(0x2018): "'",
        chr(0x2019): "'",
        chr(0x201C): '"',
        chr(0x201D): '"',
        chr(0xFF1F): "?",
        chr(0xFF01): "!",
    }

    for old, new in replacements.items():
        s = s.replace(old, new)

    s = re.sub(r"\s+", " ", s).strip()

    # Remove common bot name prefix.
    prefixes = [
        "月月，", "月月,", "月月 ",
        "月月見，", "月月见，", "月月見,", "月月见,",
        "yueyue,", "yueyue ",
    ]

    low = s.casefold()
    for prefix in prefixes:
        p = prefix.casefold()
        if low.startswith(p):
            s = s[len(prefix):].strip()
            break

    return s


def _has_any(text: str, items: list[str]) -> bool:
    low = text.casefold()
    return any(item.casefold() in low for item in items)


def _is_time_query(text: str) -> bool:
    s = normalize_owner_text(text)
    low = s.casefold()

    # Avoid false positives for screen/tool actions.
    blocked = [
        "點擊", "点击", "點一下", "点一下", "按一下",
        "點頭", "点头", "畫面", "画面", "螢幕", "屏幕",
        "截圖", "截图", "chrome", "瀏覽器", "浏览器",
    ]
    if _has_any(s, blocked):
        return False

    date_time_terms = [
        "現在是幾號", "现在是几号",
        "現在幾號", "现在几号",
        "而家幾號", "而家几号",
        "現在是晚上嗎", "现在是晚上吗",
        "現在晚上嗎", "现在晚上吗",
        "現在是夜晚嗎", "现在是夜晚吗",
        "現在是白天", "现在是白天",
        "現在是黑天", "现在是黑天",
        "外面是黑天", "外面是白天",
        "白天還是黑天", "白天还是黑天",
        "黑天還是白天", "黑天还是白天",
        "今天是白天嗎", "今天是白天吗",
        "現在是什麼日期", "现在是什么日期",
        "現在日期", "现在日期",
        "今天幾號", "今天几号",
        "今天幾日", "今天几日",
        "今天星期幾", "今天星期几",
        "今日幾號", "今日几号",
        "今日星期幾", "今日星期几",
        "what date", "what day is it", "today's date", "current date",
    ]
    if _has_any(s, date_time_terms):
        return True

    chinese_patterns = [
        r"(現在|现在|而家|宜家|依家).{0,10}(幾|几|多少).{0,4}(點|点|鐘|钟)",
        r"(你知道|知道|問一下|问一下).{0,10}(現在|现在|而家|宜家|依家).{0,10}(幾|几|多少).{0,4}(點|点|鐘|钟)",
        r"(現在|现在|而家|宜家|依家).{0,6}(時間|时间)",
        r"^(幾|几).{0,3}(點|点|鐘|钟)(了|呀|啊|嗎|吗|\?)?$",
    ]

    if any(re.search(p, s) for p in chinese_patterns):
        return True

    english_patterns = [
        r"\bwhat\s+time\b",
        r"\bwhat\s+is\s+the\s+time\b",
        r"\bwhat's\s+the\s+time\b",
        r"\bwhats\s+the\s+time\b",
        r"\btime\s+is\s+it\b",
        r"\bcurrent\s+time\b",
        r"\blocal\s+time\b",
    ]

    if any(re.search(p, low) for p in english_patterns):
        return True

    return False


def _has_sticker_target(text: str) -> bool:
    return _has_any(text, [
        "表情包", "貼圖", "贴图", "sticker", "鬥圖", "斗图",
    ])


def _is_sticker_cancel(text: str) -> bool:
    s = normalize_owner_text(text)

    if not _has_sticker_target(s):
        return False

    return _has_any(s, [
        "不要發", "不要发", "不用發", "不用发",
        "唔好發", "唔好发", "別發", "别发",
        "不要傳", "不要传", "不要送",
        "先不要", "暫時不要", "暂时不要",
        "取消", "停", "stop",
        "do not send", "don't send", "dont send",
        "no sticker", "not this sticker",
    ])


def is_sticker_send_request(text: str, mode_value: str = "") -> bool:
    s = normalize_owner_text(text)

    if _is_sticker_cancel(s):
        return False

    if str(mode_value or "").casefold() == "social_sticker":
        return True

    if not _has_sticker_target(s):
        return False

    send_markers = [
        "發", "发", "送", "來", "来", "給我", "给我",
        "丟", "扔", "出", "看看", "測試", "测试",
        "另外", "另一", "再來", "再来", "換", "换",
        "another", "different", "send",
    ]

    if _has_any(s, send_markers):
        return True

    # Commentary/praise about a sticker already seen is not a request to send one.
    comment_markers = [
        "好得意", "好可愛", "好可爱", "好靚", "好美", "好正", "好棒",
        "很可愛", "很可爱", "很得意", "很讚", "很赞",
        "這個", "这个", "呢個", "嗰個", "那個", "那个",
        "點解", "点解", "為什麼", "为什么", "點樣", "点样", "怎麼", "怎么",
    ]
    if _has_any(s, comment_markers):
        return False

    return len(s) <= 12


def classify_owner_intent(text: str, mode_value: str = "") -> Intent:
    s = normalize_owner_text(text)

    if _is_sticker_cancel(s):
        return Intent("sticker_cancel", 0.95, "negative sticker request")

    if _is_time_query(s):
        return Intent("time_query", 0.95, "time query")

    if is_sticker_send_request(s, mode_value=mode_value):
        return Intent("sticker_send", 0.9, "send sticker request")

    return Intent("normal_chat", 0.5, "fallback")
