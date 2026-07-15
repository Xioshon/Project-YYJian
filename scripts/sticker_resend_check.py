
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from response_composer import compose_fast_reply  # noqa: E402
from sticker_assets import find_sticker_asset  # noqa: E402
from sticker_flow import direct_sticker_resend_reply  # noqa: E402

BAD_TELEGRAM_ASSET = "73AEB6631CB30FBFCC0B820316F8F1EF.gif"


class BadAgent:
    def chat(self, *args, **kwargs):
        return {"content": "我剛剛明明傳了，刷新看看？"}


class EnglishBadAgent:
    def chat(self, *args, **kwargs):
        return {"content": "I already sent it, but if you cannot see it, refresh the chat and try again."}


reply = compose_fast_reply(
    BadAgent(),
    kind="sticker_resend",
    owner_prompt="我看不到，再發一次",
    facts={
        "sticker_marker": "[表情包: Acting cute.png]",
        "sticker_name": "Acting cute.png",
    },
    fallback={"content": "我再補發一次。\n[表情包: Acting cute.png]"},
)

assert "[表情包: Acting cute.png]" in reply["content"], reply
assert "明明" not in reply["content"], reply
assert "刷新" not in reply["content"], reply

english_reply = compose_fast_reply(
    EnglishBadAgent(),
    kind="sticker_resend",
    owner_prompt="I cannot see it, resend the sticker",
    facts={
        "sticker_marker": "[sticker: Acting cute.png]",
        "sticker_name": "Acting cute.png",
    },
    fallback={"content": "again\n[sticker: Acting cute.png]"},
)

assert "[sticker: Acting cute.png]" in english_reply["content"], english_reply
assert len(english_reply["content"].splitlines()[0]) <= 48, english_reply
assert "already sent" not in english_reply["content"].casefold(), english_reply
assert "refresh" not in english_reply["content"].casefold(), english_reply
assert "again, here" not in english_reply["content"].casefold(), english_reply
assert "one more time" not in english_reply["content"].casefold(), english_reply
assert "sending it again" not in english_reply["content"].casefold(), english_reply
assert "got it, again" not in english_reply["content"].casefold(), english_reply

direct_reply = direct_sticker_resend_reply(
    "\u518d\u767c\u4e00\u6b21",
    [BAD_TELEGRAM_ASSET, "tg_photo_2022_1781693747.jpg"],
)
direct_content = direct_reply["content"]
marker_match = re.search(r"\[(?:\u8868\u60c5\u5305|\u8cbc\u5716|\u8d34\u56fe|sticker):\s*([^\]]+)\]", direct_content, re.I)
if marker_match:
    marker_name = marker_match.group(1).strip()
    assert marker_name != "tg_photo_2022_1781693747.jpg", direct_reply
    assert marker_name != BAD_TELEGRAM_ASSET, direct_reply
    assert find_sticker_asset(marker_name), direct_reply
else:
    assert "\u767c" not in direct_content and "\u88dc\u767c" not in direct_content, direct_reply

print("PASS sticker_resend_check")
