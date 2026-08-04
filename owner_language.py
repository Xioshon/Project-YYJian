"""Single source of truth for the owner's LANGUAGE SETTING (繁體 / 簡體 / English).

Why this module exists
----------------------
YueYue used to decide her output script by GUESSING: detect whether the owner's current
message looked Simplified, mirror it, then patch the result with a hand-grown correction
table. That is the same growing-blocklist disease this project keeps removing - and it is
where the archaic 喫 came from (2026-07-23), because a guessed Traditional conversion
carries a regional bias the guesser cannot know about.

The cure is subtraction, not another table: the owner STATES their language once, and each
language gets its OWN persona material. Then nothing has to be detected, mirrored, or
guessed - YueYue simply writes in the language she was told to write in.

Storage follows the existing convention rather than inventing one: the setting lives in
workspace/memory/profile.json under basic_info.language, the same file core_tools.
real_update_profile already owns and the same file ContextCompiler already loads into every
system prompt. Unset is a first-class value ("" - the owner never chose), and it deliberately
keeps the legacy detect-and-mirror behaviour so nobody's experience changes until they ask.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

TRADITIONAL = "zh-Hant"
SIMPLIFIED = "zh-Hans"
ENGLISH = "en"

# The default an unset owner is treated as. Deliberately Traditional: that is what every
# existing conversation already gets, and stage A must not change anyone's experience.
DEFAULT_LANGUAGE = TRADITIONAL

# English is declared here (owner asked for the interface now, the content later) but has no
# persona material yet, so it is not selectable until those files exist.
SUPPORTED_LANGUAGES = (TRADITIONAL, SIMPLIFIED)

DISPLAY_NAMES = {
    TRADITIONAL: "繁體中文",
    SIMPLIFIED: "简体中文",
    ENGLISH: "English",
}

# Every spelling the owner (or the model relaying the owner's words) might plausibly hand us.
# This is a NORMALISER, not a detector: it maps explicit names to codes and returns "" for
# anything it does not recognise. It never inspects message content to infer a language.
_ALIASES = {
    TRADITIONAL: ("zh-hant", "zh_hant", "zh-tw", "zh_tw", "zh-hk", "zh_hk", "hant", "tw", "hk",
                  "繁", "繁體", "繁体", "繁中", "繁體中文", "繁体中文", "正體", "正体", "正體中文",
                  "traditional", "traditional chinese"),
    SIMPLIFIED: ("zh-hans", "zh_hans", "zh-cn", "zh_cn", "zh-sg", "zh_sg", "hans", "cn",
                 "簡", "简", "簡體", "简体", "簡中", "简中", "簡體中文", "简体中文",
                 "simplified", "simplified chinese"),
    ENGLISH: ("en", "en-us", "en_us", "en-gb", "eng", "英", "英文", "英語", "英语",
              "english"),
}

_ALIAS_TO_CODE = {alias: code for code, aliases in _ALIASES.items() for alias in aliases}


def normalize_language(value: str) -> str:
    """Map an owner-supplied language name to a canonical code, or "" if unrecognised."""
    key = str(value or "").strip().casefold().replace(" ", "")
    if not key:
        return ""
    # Retry with spaces for the multi-word English aliases stripped above.
    return _ALIAS_TO_CODE.get(key) or _ALIAS_TO_CODE.get(str(value or "").strip().casefold(), "")


def profile_path(root: str | Path | None = None) -> Path:
    """workspace/memory/profile.json under the given root (or the live project root).

    With no explicit root this DEFERS to core_tools.PROFILE_FILE rather than re-deriving the
    project root from YUEYUE_ROOT_DIR. That matters because core_tools.real_update_profile - the
    model-callable tool - writes to that exact constant: two independent copies of the
    root-resolution rule can drift, and a drift here is silent and nasty (the setting writes to
    one file while the system prompt reads another, so switching language appears to work and
    changes nothing). Live check 2026-08-04 hit precisely that split. The local computation stays
    as a fallback so this module remains importable and testable on its own.
    """
    if root:
        return Path(root) / "workspace" / "memory" / "profile.json"
    try:
        from core_tools import PROFILE_FILE

        return Path(PROFILE_FILE)
    except Exception:
        base = Path(os.getenv("YUEYUE_ROOT_DIR") or Path(__file__).resolve().parent)
        return base / "workspace" / "memory" / "profile.json"


def read_language(root: str | Path | None = None) -> str:
    """The owner's CHOSEN language code, or "" when they have never chosen one.

    "" is meaningful and must not be collapsed into the default by callers that care about
    the difference: an unset owner keeps the legacy detect-and-mirror path, a set owner does
    not. Anything unreadable or unrecognised (a corrupt file, a value the model wrote through
    update_profile) degrades to "" rather than raising - a broken profile must never take chat
    down.
    """
    try:
        raw = json.loads(profile_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return ""
    if not isinstance(raw, dict):
        return ""
    basic = raw.get("basic_info")
    value = basic.get("language") if isinstance(basic, dict) else None
    code = normalize_language(str(value or ""))
    return code if code in SUPPORTED_LANGUAGES else ""


def resolve_language(root: str | Path | None = None) -> str:
    """The language to actually write in - the owner's choice, or the default when unset."""
    return read_language(root) or DEFAULT_LANGUAGE


def write_language(value: str, root: str | Path | None = None) -> str:
    """Persist a language choice. Returns the canonical code, or "" if the value was rejected.

    Read-modify-write on the same profile.json shape core_tools.real_update_profile uses, so
    the two writers cannot disagree about the file's structure.
    """
    code = normalize_language(value)
    if code not in SUPPORTED_LANGUAGES:
        return ""
    path = profile_path(root)
    profile: dict = {}
    try:
        if path.exists() and path.stat().st_size:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                profile = loaded
    except (OSError, ValueError, UnicodeError):
        profile = {}
    profile.setdefault("basic_info", {})
    profile.setdefault("preferences", [])
    profile.setdefault("important_facts", [])
    if not isinstance(profile["basic_info"], dict):
        profile["basic_info"] = {}
    profile["basic_info"]["language"] = code
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return code


def display_name(code: str) -> str:
    return DISPLAY_NAMES.get(code, DISPLAY_NAMES[DEFAULT_LANGUAGE])


def locale_suffix(code: str) -> str:
    """Filename infix for a language's persona material. Traditional keeps the bare filename
    (personality.md) because it is the source the other locales are generated FROM."""
    return "" if code in {"", TRADITIONAL} else f".{code}"


def reply_language_directive(code: str) -> str:
    """The one line that tells the model which language to write the reply in.

    Stated in the target language itself - a Simplified instruction written in Traditional is
    a mixed signal, and mixed signals are exactly what the old mirroring path kept losing to.
    """
    if code == SIMPLIFIED:
        return (
            "You are YueYue. 用简体中文回复主人，这是主人自己设定的语言。"
            "全程使用简体字，不要写繁体字，也不要在同一句里混用两种字体。"
        )
    if code == ENGLISH:
        return "You are YueYue. Reply to your owner in natural English - this is the language they set."
    return (
        "You are YueYue. 用繁體中文回覆主人，這是主人設定的語言。"
        "全程使用繁體字，不要寫簡體字，也不要在同一句裡混用兩種字體。"
    )
