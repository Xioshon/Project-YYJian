
from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any

from chat_text_sanitizers import _is_plain_greeting_text
from core_tools import env_value
from voice_contract import VOICE_REGISTER_ZH, voice_register_violation

_CACHE = Path(__file__).resolve().parent / "workspace" / "project_cache" / "recent_fast_replies.json"


plain_greeting_social_generation_enabled = True


def _load_recent() -> list[str]:
    try:
        data = json.loads(_CACHE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data][-20:]
    except Exception:
        pass
    return []


def _save_recent(text: str) -> None:
    try:
        recent = _load_recent()
        recent.append(str(text or ""))
        recent = recent[-20:]
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps(recent, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _as_content(reply: Any) -> str:
    if isinstance(reply, dict):
        return str(reply.get("content", "") or "")
    if hasattr(reply, "content"):
        return str(reply.content or "")
    return str(reply or "")


def _clean_line(text: str) -> str:
    text = str(text or "").strip()
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()

    # Keep it short. Fast-path replies should not become essays.
    if len(text) > 80:
        text = text[:80].rstrip() + "…"

    return text


def _cap_visible_text(text: str, limit: int = 48) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _has_overexplained_social_wording(text: str) -> bool:
    lower = str(text or "").casefold()
    bad = [
        "cannot send",
        "can't send",
        "cant send",
        "unable to send",
        "already sent",
        "please refresh",
        "refresh the chat",
        "try again",
        "only text",
        "text version",
        "as an ai",
        "i don't have the ability",
        "i do not have the ability",
        "i cannot see",
        "i can't see",
    ]
    return any(item in lower for item in bad)


GENERATED_GREETING_BAD_PHRASES = [
    "workflow",
    "permission",
    "execute_command",
    "tool",
    "這一步需要你點頭",
    "这一步需要你点头",
    "你回「可以」",
    "你回\"可以\"",
    "我才能繼續",
    "我才能继续",
    "接著剛才的任務",
    "接着刚才的任务",
    "任務",
    "任务",
    "行程",
    "需要我幫你",
    "需要我帮你",
    "整理今天",
    "開機",
    "开机",
    "待機",
    "待机",
    "載入",
    "加载",
    "第三次",
    "第三杯",
    "複讀機",
    "复读机",
    "又說你好",
    "又说你好",
    "早上到晚上",
    "喊這麼甜",
    "喊这么甜",
    "喵這麼甜",
    "喵这么甜",
    "聽到了啦",
    "听到了啦",
    "主人",
    "客服",
    "原來是打招呼",
    "原来是打招呼",
    "就這",
    "就这",
    "敷衍",
    "終於要說點特別",
    "终于要说点特别",
    "更好玩",
    "好玩的",
    "貼圖",
    "贴图",
    "表情包",
    "\u770b\u51fa\u4f60\u5728\u60f3",
    "\u770b\u51fa\u4f60\u60f3",
    "\u4f60\u5728\u60f3\u4ec0\u9ebc",
    "\u4f60\u5728\u60f3\u4ec0\u4e48",
    "\u4e00\u773c\u5c31\u770b\u51fa",
    "\u6708\u6708\u770b\u51fa",
    "\u6708\u6708\u77e5\u9053\u4f60\u60f3",
    "\u662f\u4e0d\u662f\u60f3",
    "\u80af\u5b9a\u662f\u60f3",
    "\uff08\u6311\u7709\uff09",
    "\uff08\u6b6a\u982d\uff09",
    "\uff08\u6b6a\u5934\uff09",
    "\uff08\u7728\u773c\uff09",
    "\uff08\u5077\u7b11\uff09",
    "\uff08\u7b11\uff09",
    "(\u6311\u7709)",
    "(\u6b6a\u982d)",
    "(\u6b6a\u5934)",
    "(\u7728\u773c)",
    "(\u5077\u7b11)",
    "(\u7b11)",
    "\u6311\u7709",
    "\u6b6a\u982d",
    "\u6b6a\u5934",
    "\u7b11\u800c\u4e0d\u8a9e",
    "\u7b11\u800c\u4e0d\u8bed",
    "\u7279\u5730\u89e3\u91cb",
    "\u9084\u7279\u5730",
    "\u8fd8\u7279\u5730",
    "\u4f60\u5728\u89e3\u91cb",
    "\u89e3\u91cb\u7d66\u6708\u6708",
    "\u89e3\u91ca\u7ed9\u6708\u6708",
    "\u6536\u5230\u5566",
    "\u6709\u5728\u807d",
    "\u6709\u5728\u542c",
    "\u6536\u5230",
    "\u6536\u5230\u4e86",
    "\u77e5\u9053\u5566",
    "\u77e5\u9053\u4e86",
    "\u61c2\u7684",
    "\u6708\u6708\u61c2",
    "\u6708\u6708\u77e5\u9053",
    "\u4e0d\u7528\u89e3\u91cb",
    "\u9019\u8072",
    "\u8fd9\u58f0",
    "\u9019\u53e5",
    "\u8fd9\u53e5",
    "\u9019\u500b\u62db\u547c",
    "\u8fd9\u4e2a\u62db\u547c",
    "\u554f\u5019",
    "\u95ee\u5019",
    "\u4e00\u53e5\u4f60\u597d",
    "\u4e00\u53e5hi",
    "\u4e00\u8072\u4f60\u597d",
    "\u4e00\u58f0\u4f60\u597d",
    "\u4e00\u500b\u4f60\u597d",
    "\u4e00\u4e2a\u4f60\u597d",
    "\u53ea\u8aaa\u4f60\u597d",
    "\u53ea\u8bf4\u4f60\u597d",
    "\u53ea\u6703\u4f60\u597d",
    "\u53ea\u4f1a\u4f60\u597d",
    "\u5c31\u60f3\u6253\u767c",
    "\u5c31\u60f3\u6253\u53d1",
    "\u6253\u767c\u6708\u6708",
    "\u6253\u53d1\u6708\u6708",
    "\u6577\u884d\u6708\u6708",
    "\u51c6\u4e86",
    "\u6e96\u4e86",
    "\u6279\u51c6",
    "\u6253\u62db\u547c",
    "\u770b\u5f97\u51fa\u4f86",
    "\u770b\u5f97\u51fa\u6765",
    "\u8ab0\u770b\u4e0d\u51fa\u4f86",
    "\u8c01\u770b\u4e0d\u51fa\u6765",
    "\u539f\u4f86\u662f",
    "\u539f\u6765\u662f",
    "\u53ea\u662f\u6253\u62db\u547c",
    "\u5077\u7b11\u4e2d",
    "\u7728\u773c\u4e2d",
    "\u6b6a\u982d\u4e2d",
    "\u6b6a\u5934\u4e2d",
    "\u5225\u6643",
    "\u5225\u5750",
    "\u5750\u90a3\u9ebc",
    "\u5750\u90a3\u4e48",
    "\u5750\u597d",
    "\u7ad9\u597d",
    "\u807d\u8457",
    "\u542c\u7740",
    "sticker:",
    "[sticker",
    "[表情包",
]


GENERATED_GREETING_TIME_RE = re.compile(
    r"(?:20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}|\d{1,2}:\d{2}|現在是|现在是|今天是星期|今天是\s*20\d{2})",
    re.I,
)


GENERATED_GREETING_STATUS_RE = re.compile(
    r"(?:\u5077\u7b11|\u7728\u773c|\u6b6a\u982d|\u6b6a\u5934|\u773c\u795e|\u5fae\u7b11).{0,3}\u4e2d(?:[\u3002\uff01\uff1f.!?~\uff5e])?$"
)

GENERATED_GREETING_STAGE_RE = re.compile(r"[\(\uff08][^\(\)\uff08\uff09\n\r]{1,8}[\)\uff09]")


def _compose_sticker_content(
    text: str,
    sticker_marker: str,
    fallback_options: list[str],
    *,
    limit: int = 48,
) -> tuple[str, str]:
    if not text or _has_overexplained_social_wording(text):
        text = random.choice(fallback_options)

    text = _cap_visible_text(text, limit)
    content = text
    if sticker_marker:
        content = f"{content}\n{sticker_marker}"
    return text, content


def _fallback_variation(kind: str, owner_prompt: str, facts: dict[str, Any], fallback: dict[str, str]) -> dict[str, str]:
    content = str((fallback or {}).get("content", "") or "")

    if kind == "time_query":
        exact_time = str(facts.get("exact_time") or content)
        options = [
            exact_time,
            exact_time.replace("現在是 ", "現在 "),
            exact_time.replace(" 喵～", ""),
        ]
        return {"content": random.choice([x for x in options if x.strip()])}

    return {"content": content}


def _fact_from_exact_time(exact_time: str, name: str) -> str:
    exact_time = str(exact_time or "")
    if name == "date":
        match = re.search(r"(20\d{2}-\d{2}-\d{2})", exact_time)
        return match.group(1) if match else ""
    if name == "time":
        match = re.search(r"\b(\d{1,2}:\d{2})(?::\d{2})?\b", exact_time)
        return match.group(1) if match else ""
    return ""


def _looks_like_recent_time_reply(text: str) -> bool:
    value = str(text or "")
    if not re.search(r"20\d{2}-\d{2}-\d{2}", value):
        return False
    return any(marker in value for marker in ["\u661f\u671f", "\u4eca\u5929", "\u73fe\u5728", "\u73b0\u5728"])


def _time_query_kind(owner_prompt: str) -> str:
    value = str(owner_prompt or "").casefold()
    daylight_markers = [
        "\u767d\u5929",
        "\u9ed1\u5929",
        "\u5916\u9762",
        "\u5929\u9ed1",
        "\u5929\u4eae",
    ]
    period_markers = [
        "\u665a\u4e0a",
        "\u591c\u665a",
        "\u51cc\u6668",
        "\u6df1\u591c",
        "\u4e0a\u5348",
        "\u4e0b\u5348",
        "\u4e2d\u5348",
        "\u65e9\u4e0a",
    ]
    time_markers = [
        "\u5e7e\u9ede",
        "\u51e0\u70b9",
        "\u6642\u9593",
        "\u65f6\u95f4",
        "what time",
        "current time",
    ]
    weekday_markers = ["\u661f\u671f", "\u79ae\u62dc", "\u793c\u62dc", "what day"]
    if any(marker in value for marker in daylight_markers):
        return "daylight"
    if any(marker in value for marker in period_markers):
        return "period"
    if any(marker in value for marker in time_markers):
        return "time"
    if any(marker in value for marker in weekday_markers):
        return "weekday"
    return "date"


def _compose_time_query_reply(owner_prompt: str, facts: dict[str, Any], fallback: dict[str, str], recent: list[str]) -> str:
    exact_time = str(facts.get("exact_time") or fallback.get("content") or "").strip()
    date_text = str(facts.get("date") or _fact_from_exact_time(exact_time, "date")).strip()
    time_text = str(facts.get("time") or _fact_from_exact_time(exact_time, "time")).strip()
    weekday = str(facts.get("weekday") or "").strip()
    period = str(facts.get("period") or "").strip()

    repeated = any(_looks_like_recent_time_reply(item) for item in recent[-6:])
    query_kind = _time_query_kind(owner_prompt)

    if not (date_text or time_text or weekday):
        return exact_time

    if query_kind == "daylight":
        dark_periods = {"\u665a\u4e0a", "\u51cc\u6668", "\u6df1\u591c"}
        light = "\u9ed1\u5929" if period in dark_periods else "\u767d\u5929"
        period_part = f"\u73fe\u5728\u662f{period}" if period else f"\u73fe\u5728\u662f {time_text}"
        return f"\u6309\u73fe\u5728\u6642\u9593\u4f86\u770b\uff0c{period_part}\uff0c\u5916\u9762\u61c9\u8a72\u7b97{light}\u3002"

    if query_kind == "period":
        if period:
            return f"\u73fe\u5728\u662f{period}\uff0c\u4e3b\u4eba\u6642\u9593\u611f\u6c92\u58de\u3002"
        if time_text:
            return f"\u73fe\u5728\u662f {time_text}\u3002"

    if repeated and time_text and date_text:
        tail = f"\uff0c{weekday}" if weekday else ""
        return f"\u525b\u525b\u4e5f\u5728\u6e2c\u9019\u500b\u5c0d\u5427\uff1f\u73fe\u5728\u662f {time_text}\uff0c\u65e5\u671f\u9084\u662f {date_text}{tail}\u3002"

    if query_kind == "time" and time_text:
        tail = f"\uff0c\u4eca\u5929\u662f{weekday}" if weekday else ""
        return f"\u73fe\u5728\u662f {time_text}{tail}\u3002"

    if query_kind == "weekday" and weekday:
        tail = f"\uff0c{date_text}" if date_text else ""
        return f"\u4eca\u5929\u662f{weekday}{tail}\u3002"

    if date_text and weekday:
        return f"\u4eca\u5929\u662f {date_text}\uff0c{weekday}\u3002"
    if date_text:
        return f"\u4eca\u5929\u662f {date_text}\u3002"
    if time_text:
        suffix = f"\uff0c{period}" if period else ""
        return f"\u73fe\u5728\u662f {time_text}{suffix}\u3002"
    return exact_time


is_plain_greeting = _is_plain_greeting_text


def _plain_greeting_options(owner_prompt: str) -> list[str]:
    value = str(owner_prompt or "").casefold()
    if "hi" in value:
        return [
            "\u55e8\uff0c\u7d42\u65bc\u60f3\u8d77\u6708\u6708\u4e86\u3002",
            "\u55ef\uff0c\u558a\u6708\u6708\u5e79\u561b\u3002",
            "\u55b5\uff0c\u4eca\u5929\u9084\u7b97\u4e56\u3002",
            "\u884c\u5427\uff0c\u6708\u6708\u5728\u9019\u3002",
            "\u54fc\uff0c\u4eca\u5929\u7b97\u4f60\u4e56\u3002",
        ]
    return [
        "\u55ef\u54fc\uff0c\u5192\u6ce1\u4e86\u3002",
        "\u5728\uff0c\u5225\u558a\u5f97\u50cf\u9ede\u540d\u3002",
        "\u55ef\uff0c\u4eca\u5929\u8f2a\u5230\u6708\u6708\u4e86\u3002",
        "\u8aaa\u5427\uff0c\u6708\u6708\u52c9\u5f37\u807d\u4e00\u4e0b\u3002",
        "\u55ef\uff0c\u53c8\u4f86\u6643\u4e00\u4e0b\u3002",
    ]


def _micro_plain_greeting_options(owner_prompt: str) -> list[str]:
    value = str(owner_prompt or "").casefold()
    if "hi" in value:
        openers = ["\u55e8", "\u55ef", "\u55b5", "\u884c\u5427", "\u54fc"]
        tails = [
            "\u6708\u6708\u5728\u9019\u3002",
            "\u7d42\u65bc\u60f3\u8d77\u6708\u6708\u4e86\u3002",
            "\u4eca\u5929\u9084\u7b97\u4e56\u3002",
            "\u627e\u6708\u6708\u5c31\u76f4\u8aaa\u3002",
            "\u5148\u7b97\u4f60\u4e56\u3002",
        ]
    else:
        openers = ["\u5728\u5566", "\u55ef\u54fc", "\u5728", "\u55ef", "\u8aaa\u5427"]
        tails = [
            "\u6708\u6708\u53c8\u6c92\u8dd1\u3002",
            "\u53c8\u5192\u51fa\u4f86\u4e86\u3002",
            "\u5225\u558a\u5f97\u50cf\u9ede\u540d\u3002",
            "\u4eca\u5929\u8f2a\u5230\u6708\u6708\u4e86\u3002",
            "\u6708\u6708\u52c9\u5f37\u807d\u4e00\u4e0b\u3002",
        ]
    options: list[str] = []
    for index, opener in enumerate(openers):
        tail = tails[index % len(tails)]
        separator = "\uff0c" if not opener.endswith(("\uff0c", "\u3002")) else ""
        options.append(f"{opener}{separator}{tail}")
    return options


def _is_valid_micro_plain_greeting(text: str, recent: list[str]) -> bool:
    value = str(text or "").strip()
    if not value or "\n" in value or "\r" in value:
        return False
    if len(value) > 36 or len(value) < 2:
        return False
    if "[sticker" in value.casefold() or "[\u8868\u60c5\u5305" in value:
        return False
    if GENERATED_GREETING_TIME_RE.search(value):
        return False
    if value.count("\u4e3b\u4eba") or value.count("\u55b5") > 1:
        return False
    if sum(value.count(mark) for mark in ["\u3002", "\uff01", "\uff1f", "!", "?"]) > 1:
        return False
    lowered = value.casefold()
    if any(phrase.casefold() in lowered for phrase in GENERATED_GREETING_BAD_PHRASES):
        return False
    if _has_overexplained_social_wording(value):
        return False
    if voice_register_violation(value):
        return False
    key = _social_similarity_key(value)
    if not key:
        return False
    recent_keys = {_social_similarity_key(item) for item in recent[-6:] if _social_similarity_key(item)}
    if key in recent_keys:
        return False
    family = _social_family_key(value)
    recent_families = {item for item in (_social_family_key(x) for x in recent[-8:]) if item}
    return not (family and family in recent_families)


def _compose_micro_plain_greeting(owner_prompt: str, recent: list[str]) -> str:
    options = _micro_plain_greeting_options(owner_prompt)
    candidates = [option for option in options if _is_valid_micro_plain_greeting(option, recent)]
    text = random.choice(candidates or options) if options else ""
    return text if _is_valid_micro_plain_greeting(text, recent) else ""


def _model_chat_without_tools(agent: Any, messages: list[dict[str, str]]) -> str:
    provider = getattr(agent, "provider", None)
    chat = getattr(provider, "chat", None)
    if not callable(chat):
        return ""
    try:
        # Fast-path persona lines follow the chat voice model (same override the v3 chat
        # turn uses), falling back to the provider default when YUEYUE_CHAT_MODEL is unset.
        # Checks real OS env first, then .env, via core_tools.env_value.
        return _as_content(chat(messages, [], model=env_value("YUEYUE_CHAT_MODEL").strip()))
    except TypeError:
        try:
            return _as_content(chat(messages, []))
        except Exception:
            return ""
    except Exception:
        return ""


def _generated_greeting_prompt(owner_prompt: str, recent: list[str]) -> list[dict[str, str]]:
    recent_lines = [str(item or "").strip() for item in recent[-4:] if str(item or "").strip()]
    system = (
        "你是月月，只回輕短寒暄。\n"
        "用繁體中文。一句話，28字以內，不換行。\n"
        f"{VOICE_REGISTER_ZH}\n"
        "語氣短、口語、俏皮、微傲嬌，有一點黏但不要曖昧。\n"
        "不要像客服或助理，不要提幫忙做事、點頭、繼續、日期時間、貼圖。\n"
        "不要提第三次、複讀機、舊測試、舊問候。\n"
        "不要用「就這」「原來只是在叫月月」「敷衍」這類冷場句。"
    )
    if recent_lines:
        system += "\n最近用過的語氣不要照抄：" + " / ".join(recent_lines)
    user = "情境：主人剛剛出現，輕輕叫了月月一下。只回一個自然短句，不要引用或提到他用了哪些字。"
    system += (
        "\n不要描述這是什麼訊息；直接自然回他。"
        "\n不要用角色狀態標籤，例如偷笑中、眨眼中、歪頭中。"
        "\n不要給沒有上下文的身體動作指令，例如別晃、坐好、站好。"
        "\n不要假裝讀心或推測隱藏意圖，例如一眼就看出、你在想什麼、是不是想月月。"
        "\n不要寫舞台指示或括號動作，不要描述月月的手勢、表情或狀態。"
        "\n不要評論使用者的訊息類型或說他在解釋；像真的短聊天那樣回一句。"
        "\n不要把回覆寫成收到或確認收件；不要說收到、知道啦、懂的。"
        "\n不要評論「這聲」「這句」「這個招呼」；直接像聊天接一句。"
        "\n不要說月月知道或月月懂使用者的意思。"
        "\n不要引用、重述、評分或判斷主人用了哪些字；不要分析輸入內容。"
        "\n不要用「准了」「批准」這類命令口吻。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _is_valid_generated_greeting(text: str, recent: list[str]) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if "\n" in value or "\r" in value:
        return False
    if len(value) > 28 or len(value) < 2:
        return False
    if GENERATED_GREETING_TIME_RE.search(value):
        return False
    lowered = value.casefold()
    if any(phrase.casefold() in lowered for phrase in GENERATED_GREETING_BAD_PHRASES):
        return False
    if GENERATED_GREETING_STATUS_RE.search(value):
        return False
    if GENERATED_GREETING_STAGE_RE.search(value):
        return False
    if sum(value.count(mark) for mark in ["。", "！", "？", "!", "?"]) > 1:
        return False
    if value.count("喵") > 1:
        return False
    if _has_overexplained_social_wording(value):
        return False
    if voice_register_violation(value):
        return False
    normalized = re.sub(r"[\s\u3000\uff0c,.\u3002!！?？~～]+", "", value.casefold())
    if normalized in {"hi", "hello", "嗨", "你好", "hi你好"}:
        return False
    key = _social_similarity_key(value)
    if not key:
        return False
    recent_keys = {_social_similarity_key(item) for item in recent[-6:] if _social_similarity_key(item)}
    if key in recent_keys:
        return False
    family = _social_family_key(value)
    recent_families = {item for item in (_social_family_key(x) for x in recent[-8:]) if item}
    return not (family and family in recent_families)


def _greeting_rejection_reason(text: str, recent: list[str]) -> str:
    """Name the first check a candidate fails, so the retry can tell the model WHAT to fix.
    ROADMAP P1: a named critique turns blocklist hits from silent discard (-> canned pool) into
    a self-correction signal - the model usually fixes a named problem on the second try."""
    value = str(text or "").strip()
    if not value or "\n" in value or "\r" in value or len(value) > 28:
        return "太長或換行了，一句28字以內"
    lowered = value.casefold()
    hits = [phrase for phrase in GENERATED_GREETING_BAD_PHRASES if phrase.casefold() in lowered]
    if hits:
        return "用了太熟口熟面的罐頭字眼（" + "、".join(hits[:2]) + "），換個自然說法"
    violation = voice_register_violation(value)
    if violation:
        return "字感不對（" + violation + "），用香港書面繁體，別用台味語尾或粵語口語字"
    if _social_similarity_key(value) and _social_similarity_key(value) in {
        _social_similarity_key(item) for item in recent[-6:]
    }:
        return "跟最近說過的太像，換個角度"
    return "語氣不自然，重寫一句更像月月本人的"


def _compose_generated_plain_greeting(agent: Any, owner_prompt: str, recent: list[str]) -> str:
    messages = _generated_greeting_prompt(owner_prompt, recent)
    for _ in range(2):
        raw = _model_chat_without_tools(agent, messages)
        if not raw:
            return ""
        text = str(raw or "").strip()
        text = re.sub(r"^['\"「『]+|['\"」』]+$", "", text).strip()
        if _is_valid_generated_greeting(text, recent):
            return text
        # Retry once with a named critique before surrendering to the pooled fallback.
        messages = [
            *messages[:-1],
            {
                "role": "user",
                "content": messages[-1]["content"]
                + f"\n你剛寫的「{text[:40]}」被拒：{_greeting_rejection_reason(text, recent)}。",
            },
        ]
    return ""


def _compose_generated_short_line(agent: Any, system: str, user: str, recent: list[str]) -> str:
    """Shared model-first path for short in-character one-liners (wake greetings, sticker
    send/resend banter). Falls back to "" on any failure so callers use their own pooled
    options - the pool is a last resort, not the default outcome."""
    recent_lines = [str(item or "").strip() for item in recent[-4:] if str(item or "").strip()]
    if recent_lines:
        system = system + "\n最近用過的語氣不要照抄：" + " / ".join(recent_lines)
    prompt = user
    for _ in range(2):
        raw = _model_chat_without_tools(agent, [{"role": "system", "content": system}, {"role": "user", "content": prompt}])
        if not raw:
            return ""
        text = str(raw or "").strip()
        text = re.sub(r"^['\"「『]+|['\"」』]+$", "", text).strip()
        if _is_valid_micro_plain_greeting(text, recent):
            return text
        prompt = user + f"\n你剛寫的「{text[:40]}」被拒：{_greeting_rejection_reason(text, recent)}。"
    return ""


def _social_similarity_key(text: str) -> str:
    value = str(text or "").casefold()
    value = re.sub(r"[\s\u3000\uff0c,.\u3002!！?？~～]+", "", value)
    for marker in ["hi", "hello", "\u6708\u6708", "\u4e3b\u4eba", "\u55b5"]:
        value = value.replace(marker.casefold(), "")
    return value


def _social_family_key(text: str) -> str:
    value = str(text or "").casefold()
    families = [
        ("\u7d42\u65bc", "\u7ec8\u4e8e"),
        ("\u770b\u5230\u4f60",),
        ("\u8aaa\u6b63\u4e8b", "\u8bf4\u6b63\u4e8b"),
        ("\u6c92\u8ff7\u8def", "\u6ca1\u8ff7\u8def"),
        ("\u6253\u62db\u547c",),
        ("\u53c8\u627e\u6708\u6708", "\u627e\u6708\u6708\u4e86"),
        ("\u5192\u6ce1",),
        ("\u9ede\u540d", "\u70b9\u540d"),
        ("\u60f3\u8d77\u6708\u6708",),
        ("\u807d\u8457", "\u542c\u7740", "\u8033\u6735\u501f\u4f60", "\u52c9\u5f37\u807d"),
        ("\u5c3e\u5df4",),
        ("\u9001\u4e0a\u9580", "\u9001\u4e0a\u95e8"),
        ("\u558a\u6708\u6708",),
        ("\u9ecf\u6708\u6708", "\u7c98\u6708\u6708"),
        ("\u5225\u6643",),
        ("\u8cde\u4f60",),
        ("\u501f\u4f60",),
        ("\u5c0f\u6c23", "\u5c0f\u6c14"),
        ("\u5225\u7728\u773c",),
    ]
    for family in families:
        if any(marker.casefold() in value for marker in family):
            return family[0].casefold()
    return ""


def _choose_social_reply(options: list[str], recent: list[str]) -> str:
    recent_keys = [_social_similarity_key(item) for item in recent[-6:]]
    recent_families = {family for family in (_social_family_key(item) for item in recent[-8:]) if family}
    candidates = [
        option
        for option in options
        if _social_similarity_key(option) and _social_similarity_key(option) not in recent_keys
        and not (_social_family_key(option) and _social_family_key(option) in recent_families)
    ]
    return random.choice(candidates or options) if options else ""


def _sticker_send_options() -> list[str]:
    return [
        "\u62ff\u53bb\uff0c\u611f\u8b1d\u6708\u6708\u5927\u767c\u6148\u60b2\u5566\uff5e",
        "\u558f\uff0c\u96e3\u5f97\u5927\u65b9\u4e00\u6b21",
        "\u63a5\u597d\u554a\uff0c\u4e0d\u51c6\u8aaa\u8b1d\u8b1d\u90fd\u4e0d\u6703",
        "\u9019\u5f35\u7d66\u4f60\uff0c\u958b\u5fc3\u4e86\u5427\uff1f",
        "\u770b\u5728\u4f60\u53ef\u6190\u7684\u4efd\u4e0a\uff0c\u7d66\u4f60\u5566",
    ]


def _sticker_resend_options() -> list[str]:
    return [
        "\u884c\u5566\uff0c\u518d\u88dc\u4e00\u6b21\u7d66\u4f60",
        "\u9019\u9ebc\u60f3\u8981\uff0c\u518d\u88dc\u4e00\u5f35",
        "\u597d\u5566\u597d\u5566\uff0c\u518d\u4f86\u4e00\u5f35",
        "\u7b97\u4f60\u904b\u6c23\u597d\uff0c\u518d\u88dc\u4e00\u5f35",
        "\u56c9\u5506\uff0c\u9019\u5f35\u7e3d\u53ef\u4ee5\u4e86\u5427",
    ]


def is_simple_wake_greeting(text: str) -> bool:
    value = str(text or "").casefold()
    value = re.sub(r"[\s\u3000\uff0c,.\u3002!！?？~～]+", "", value)
    for name in ["\u6708\u6708", "\u93c8\u581f\u6e40"]:
        value = value.replace(name.casefold(), "")
    greeting_markers = [
        "\u65e9\u4e0a\u597d",
        "\u65e9\u5b89",
        "\u65e9",
        "morning",
    ]
    wake_markers = [
        "\u525b\u9192",
        "\u521a\u9192",
        "\u525b\u7761\u9192",
        "\u521a\u7761\u9192",
        "\u9192\u4e86",
        "\u9192\u5566",
    ]
    return any(marker in value for marker in greeting_markers) and any(marker in value for marker in wake_markers)


def _period_bucket_for_wake(period: str) -> tuple[str, str]:
    value = str(period or "").casefold()
    if value in {"\u65e9\u4e0a", "morning"}:
        return "morning", "\u65e9\u4e0a"
    if value in {"\u4e0b\u5348", "afternoon"}:
        return "afternoon", "\u4e0b\u5348"
    if value in {"\u4e2d\u5348", "noon"}:
        return "noon", "\u4e2d\u5348"
    if value in {"\u665a\u4e0a", "evening"}:
        return "night", "\u665a\u4e0a"
    if value in {"\u51cc\u6668", "late night"}:
        return "night", "\u51cc\u6668"
    if value in {"\u6df1\u591c", "night"}:
        return "night", "\u6df1\u591c"
    return "", str(period or "").strip()


def _compose_wake_greeting_reply(
    agent: Any, owner_prompt: str, facts: dict[str, Any] | None = None, recent: list[str] | None = None
) -> str:
    facts = dict(facts or {})
    recent = recent or []
    bucket, label = _period_bucket_for_wake(str(facts.get("period") or ""))
    mismatched = bool(bucket and bucket != "morning")
    system = (
        "你是月月，主人剛打招呼說睡醒了/早安。\n"
        '用繁體中文回一句自然短句，20字以內，不換行，語氣傲嬌帶點關心，不要說教。'
        "\n不要提到系統、流程、時間感知這類詞。"
        f"\n{VOICE_REGISTER_ZH}"
    )
    if mismatched:
        system += f"\n主人說的是早安，但現在真實時段其實是「{label}」，不是早上。用真實時段自然吐槽他睡太久，不要順著他說早安。"
        user = f"主人剛打招呼：{owner_prompt}\n用真實時段自然回一句，不要真的說早安。"
    else:
        system += "\n" + '主人現在真的是剛睡醒的早上，正常回應早安、可以順帶叫他先喝水或別急著硬撐。'
        user = f"主人剛打招呼：{owner_prompt}\n自然回一句。"
    generated = _compose_generated_short_line(agent, system, user, recent)
    if generated:
        return generated
    if mismatched:
        return random.choice([
            f"\u90fd{label}\u4e86\u9084\u65e9\uff0c\u7761\u8ff7\u7cca\u4e86\u5427\u3002",
            f"\u73fe\u5728\u662f{label}\u5566\uff0c\u525b\u9192\u5c31\u5148\u5225\u901e\u5f37\u3002",
            f"{label}\u4e86\u9084\u558a\u65e9\uff0c\u5148\u6e05\u9192\u4e00\u4e0b\u3002",
        ])
    return random.choice([
        "\u9192\u5566\uff0c\u5148\u559d\u6c34\uff0c\u5225\u88dd\u6c92\u4e8b\u3002",
        "\u65e9\uff0c\u773c\u775b\u775c\u958b\u518d\u901e\u5f37\u3002",
        "\u9192\u4e86\u5c31\u6162\u9ede\uff0c\u5225\u4e00\u79d2\u88dd\u6e05\u9192\u3002",
        "\u65e9\u554a\uff0c\u5148\u7de9\u4e00\u4e0b\uff0c\u5225\u6025\u8457\u786c\u6490\u3002",
    ])


def _ask_model(agent: Any, prompt: str) -> str:
    try:
        reply = agent.chat(prompt, tool_callback=None, response_policy=None)
        return _clean_line(_as_content(reply))
    except Exception:
        return ""


def compose_fast_reply(
    agent: Any,
    *,
    kind: str,
    owner_prompt: str,
    facts: dict[str, Any] | None = None,
    fallback: dict[str, str] | None = None,
) -> dict[str, str]:
    facts = dict(facts or {})
    fallback = dict(fallback or {"content": ""})
    recent = _load_recent()

    if kind == "plain_greeting":
        if plain_greeting_social_generation_enabled:
            text = _compose_generated_plain_greeting(agent, owner_prompt, recent)
            source = "generated" if text else "fallback"
        else:
            text = _compose_micro_plain_greeting(owner_prompt, recent)
            if not _is_valid_micro_plain_greeting(text, recent):
                text = ""
            source = "micro_composer" if text else "fallback"
        if not text:
            text = _cap_visible_text(_choose_social_reply(_plain_greeting_options(owner_prompt), recent), 36)
        _save_recent(text)
        return {"content": text, "_composer_source": source}

    if kind == "wake_greeting":
        text = _cap_visible_text(_compose_wake_greeting_reply(agent, owner_prompt, facts, recent), 42)
        _save_recent(text)
        return {"content": text}

    if kind == "time_query":
        text = _compose_time_query_reply(owner_prompt, facts, fallback, recent).strip()
        reply = {"content": text} if text else _fallback_variation(kind, owner_prompt, facts, fallback)
        _save_recent(reply["content"])
        return reply

    if kind == "sticker_send":
        sticker_marker = str(facts.get("sticker_marker") or "").strip()

        generated = _compose_generated_short_line(
            agent, f'你是月月。你要把一張表情包發給主人，附一句自然的短話。用繁體中文，20字以內，不換行，語氣傲嬌但帶點寵溺，不要描述圖片內容，不要說這張圖片。{VOICE_REGISTER_ZH}', f'主人說：{owner_prompt}。自然回一句配這張表情包。', recent
        )
        text, content = _compose_sticker_content(
            generated,
            sticker_marker,
            _sticker_send_options(),
            limit=36,
        )

        _save_recent(text)
        return {"content": content}

    if kind == "sticker_resend":
        sticker_marker = str(facts.get("sticker_marker") or "").strip()

        generated = _compose_generated_short_line(
            agent, f'你是月月。主人要你再補發一次表情包，附一句自然的短話。用繁體中文，20字以內，不換行，語氣傲嬌但帶點寵溺，不要描述圖片內容，不要說這張圖片。{VOICE_REGISTER_ZH}', f'主人說：{owner_prompt}。自然回一句配這張再補發的表情包。', recent
        )
        text, content = _compose_sticker_content(
            generated,
            sticker_marker,
            _sticker_resend_options(),
            limit=36,
        )

        _save_recent(text)
        return {"content": content}

    if kind == "sticker_cancel":
        prompt = f"""你是月月。使用者表示不要發表情包，請自然回覆一句。

限制：
- 不要發圖。
- 不要說已經發了。
- 不要 Markdown。
- 30 字以內，避免固定模板。
- {VOICE_REGISTER_ZH}
- 不要重複最近用過的句子：{recent[-5:]}

使用者原句：{owner_prompt}
"""

        text = _ask_model(agent, prompt)

        bad = ["已經發", "已发", "[表情包", "發了", "发了"]
        if text and voice_register_violation(text):
            text = ""
        if not text or any(x in text for x in bad):
            text = random.choice([
                "好，這張先不發。",
                "收到，不丟這張。",
                "可以，這張先收起來。",
            ])

        _save_recent(text)
        return {"content": text}

    return fallback
