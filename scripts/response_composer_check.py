
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import contextlib  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402

import response_composer  # noqa: E402
from response_composer import compose_fast_reply  # noqa: E402


class BrokenAgent:
    def chat(self, *args, **kwargs):
        return {"content": "我看不到時間，所以不知道。"}


class GoodAgent:
    def chat(self, prompt, *args, **kwargs):
        if "已驗證時間" in prompt:
            return {"content": "現在是 2026-06-24 10:30:00。"}
        if "系統已經選好圖片" in prompt:
            return {"content": "這張可以。"}
        return {"content": "好，先不發。"}


class LongStickerAgent:
    def chat(self, *args, **kwargs):
        return {"content": "ok " * 80}


class OverExplainingStickerAgent:
    def chat(self, *args, **kwargs):
        return {"content": "I cannot send this sticker, so please refresh and check again."}


class ContextStickyStickerAgent:
    def chat(self, *args, **kwargs):
        return {"content": "\u4e3b\u4eba\uff5e\u62ff\u53bb\uff0c\u525b\u9192\u5c31\u8166\u888b\u958b\u6d1e\u4e86\uff1f"}


class ExplodingAgent:
    def __init__(self):
        self.calls = 0

    def chat(self, *args, **kwargs):
        self.calls += 1
        return {"content": "stale model reply"}


class ProviderResponse:
    def __init__(self, content: str):
        self.content = content


class GeneratedGreetingProvider:
    def __init__(self, content: str):
        self.content = content
        self.calls = 0
        self.messages = []
        self.tools_seen = None

    def chat(self, messages, tools=None, *args, **kwargs):
        self.calls += 1
        self.messages.append(messages)
        self.tools_seen = tools
        return ProviderResponse(self.content)


class GeneratedGreetingAgent:
    def __init__(self, content: str):
        self.provider = GeneratedGreetingProvider(content)


BAD_GREETING_META = [
    "\u7b2c\u4e09\u6b21",
    "\u7b2c\u4e09\u676f",
    "\u8907\u8b80\u6a5f",
    "\u590d\u8bfb\u673a",
    "\u53c8\u8aaa\u4f60\u597d",
    "\u53c8\u8bf4\u4f60\u597d",
    "\u65e9\u4e0a\u5230\u665a\u4e0a\u90fd\u6253\u904e\u62db\u547c",
    "\u65e9\u4e0a\u5230\u665a\u4e0a\u90fd\u6253\u8fc7\u62db\u547c",
    "\u8166\u888b\u5361\u5728\u958b\u6a5f\u756b\u9762",
    "\u8111\u888b\u5361\u5728\u5f00\u673a\u753b\u9762",
    "\u9019\u4e00\u6b65\u9700\u8981\u4f60\u9ede\u982d",
    "\u63a5\u8457\u525b\u624d\u7684\u4efb\u52d9",
    "workflow",
    "execute_command",
]

BAD_PERSONA_STYLE = [
    "\u958b\u6a5f",
    "\u5f00\u673a",
    "\u5f85\u6a5f",
    "\u5f85\u673a",
    "\u8f09\u5165",
    "\u52a0\u8f7d",
    "\u4efb\u52d9",
    "\u4efb\u52a1",
    "\u884c\u7a0b",
    "\u6574\u7406",
    "\u5ba2\u670d",
    "\u9700\u8981\u6211\u5e6b\u4f60",
    "\u9700\u8981\u6211\u5e2e\u4f60",
    "\u558a\u9019\u9ebc\u751c",
    "\u558a\u8fd9\u4e48\u751c",
    "\u807d\u5230\u4e86\u5566",
    "\u542c\u5230\u4e86\u5566",
    "\u806f\u7d61\u5ba2\u670d",
    "\u8054\u7cfb\u5ba2\u670d",
    "\u770b\u5230\u4f60",
    "\u770b\u5230\u4f60\u4e86",
    "\u8aaa\u6b63\u4e8b",
    "\u8bf4\u6b63\u4e8b",
    "\u6c92\u8ff7\u8def",
    "\u6ca1\u8ff7\u8def",
]

BAD_CANNED_GREETING_STYLE = [
    "\u55e8\u5230\u5566",
    "\u5225\u88dd\u8def\u904e",
    "\u6293\u5230\u4e00\u96bb\u6253\u62db\u547c\u7684\u4eba",
    "\u5225\u558a\u90a3\u9ebc\u6b63\u7d93",
    "\u627e\u6708\u6708\u554a",
    "\u4eca\u5929\u5148\u653e\u4f60\u9032\u4f86",
    "\u558a\u9019\u9ebc\u751c",
    "\u55b5\u9019\u9ebc\u751c",
]

BAD_MIND_READING_GREETING_STYLE = [
    "\u770b\u51fa\u4f60\u5728\u60f3",
    "\u770b\u51fa\u4f60\u60f3",
    "\u4f60\u5728\u60f3\u4ec0\u9ebc",
    "\u4f60\u5728\u60f3\u4ec0\u4e48",
    "\u4e00\u773c\u5c31\u770b\u51fa",
    "\u6708\u6708\u770b\u51fa",
    "\u6708\u6708\u77e5\u9053\u4f60\u60f3",
    "\u662f\u4e0d\u662f\u60f3",
    "\u80af\u5b9a\u662f\u60f3",
]

BAD_ROLEPLAY_GREETING_STYLE = [
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
    "\u5077\u7b11\u4e2d",
    "\u7728\u773c\u4e2d",
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
]

BAD_ACK_GREETING_STYLE = [
    "\u6536\u5230",
    "\u6536\u5230\u4e86",
    "\u77e5\u9053\u5566",
    "\u77e5\u9053\u4e86",
    "\u61c2\u7684",
    "\u6708\u6708\u61c2",
    "\u6708\u6708\u77e5\u9053",
    "\u4e0d\u7528\u89e3\u91cb",
    "\u7279\u5730\u89e3\u91cb",
    "\u9019\u8072",
    "\u8fd9\u58f0",
    "\u9019\u53e5",
    "\u8fd9\u53e5",
    "\u9019\u500b\u62db\u547c",
    "\u8fd9\u4e2a\u62db\u547c",
    "\u6253\u62db\u547c",
    "\u554f\u5019",
    "\u95ee\u5019",
]

BAD_MESSAGE_CONTENT_GREETING_STYLE = [
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
]

BAD_PROCESSING_STYLE = [
    "\u9019\u500b\u6211\u6709\u770b\u5230",
    "\u8fd9\u4e2a\u6211\u6709\u770b\u5230",
    "\u6211\u6709\u770b\u5230",
    "\u6709\u770b\u5230",
    "\u6709\u63a5\u5230",
    "\u4e0d\u6703\u4e82\u8dd1\u504f",
    "\u4e0d\u4f1a\u4e71\u8dd1\u504f",
    "\u6211\u5728\u9019\u908a",
    "\u6211\u5728\u8fd9\u8fb9",
    "\u6211\u5728\u9019\u88e1",
    "\u6211\u5728\u8fd9\u91cc",
    "\u5148\u6162\u4e00\u9ede",
    "\u5148\u6162\u4e00\u70b9",
    "\u5148\u8b1b\u4e00\u9ede",
    "\u5148\u8bf4\u4e00\u70b9",
    "\u6211\u966a\u4f60",
    "\u4eca\u5929\u5148\u5b88\u8457\u4f60",
    "\u4eca\u5929\u5148\u5b88\u7740\u4f60",
    "\u55ef\uff0c\u5c31\u9019\u5f35",
    "\u55ef\uff0c\u5c31\u8fd9\u5f20",
    "\u9019\u5f35\u4e5f\u53ef\u4ee5",
    "\u8fd9\u5f20\u4e5f\u53ef\u4ee5",
]

BAD_COLD_FAST_REPLY_STYLE = [
    "\u6708\u6708\u807d\u8457",
    "\u5594\uff0c\u9019\u5f35\u6b78\u4f60",
    "\u9019\u5f35\u9084\u7b97\u80fd\u770b",
    "\u6211\u518d\u88dc\u767c\u4e00\u6b21",
    "\u5c3e\u5df4\u5148\u62ac\u4e00\u4e0b",
    "\u6562\u5acc\u68c4\u5c31\u6c92\u4e0b\u6b21",
    "\u8033\u6735\u501f\u4f60",
    "\u81ea\u5df1\u9001\u4e0a\u9580",
    "\u558a\u9019\u9ebc\u751c",
    "\u55b5\u9019\u9ebc\u751c\u5e79\u561b",
    "\u8166\u888b\u958b\u6d1e",
]

BAD_LIGHT_CATGIRL_STYLE = [
    "\u5c11\u8aaa\u6708\u6708\u5c0f\u6c23",
    "\u5c11\u8bf4\u6708\u6708\u5c0f\u6c14",
    "\u5c0f\u6c23",
    "\u5c0f\u6c14",
    "\u6562\u5acc\u68c4",
    "\u6562\u5acc\u5f03",
    "\u6c92\u4e0b\u6b21",
    "\u6ca1\u4e0b\u6b21",
    "\u624d\u4e0d\u662f\u7279\u5730\u6311\u7684",
    "\u624d\u4e0d\u662f\u7279\u5730\u6311\u7684",
    "\u53c8\u8981\u554a",
    "\u518d\u7d66\u4f60\u4e00\u6b21",
    "\u518d\u7ed9\u4f60\u4e00\u6b21",
    "\u8cde\u4f60",
    "\u8d4f\u4f60",
    "\u8d08\u4f60",
    "\u5634\u4e0a\u8aaa\u7d2f",
    "\u5634\u4e0a\u8bf4\u7d2f",
    "\u624b\u9084\u5728\u90a3\u908a\u78e8",
    "\u624b\u8fd8\u5728\u90a3\u8fb9\u78e8",
    "\u9017\u5f97\u9084\u633a\u771f",
    "\u9017\u5f97\u8fd8\u633a\u771f",
]


def _assert_tight_social_text(text: str, *, limit: int = 36) -> None:
    assert text.strip(), text
    assert "\n" not in text and "\r" not in text, text
    assert len(text) <= limit, text
    assert "[sticker:" not in text.casefold(), text
    assert "\u8868\u60c5\u5305:" not in text, text
    assert sum(text.count(mark) for mark in ["\u3002", "\uff01", "\uff1f", "!", "?"]) <= 1, text
    assert text.count("\u4e3b\u4eba") <= 1, text
    assert text.count("\u55b5") <= 1, text
    assert not ("\u4e3b\u4eba" in text and "\u55b5" in text), text
    for phrase in (
        BAD_GREETING_META
        + BAD_PERSONA_STYLE
        + BAD_CANNED_GREETING_STYLE
        + BAD_MIND_READING_GREETING_STYLE
        + BAD_ROLEPLAY_GREETING_STYLE
        + BAD_ACK_GREETING_STYLE
        + BAD_PROCESSING_STYLE
        + BAD_MESSAGE_CONTENT_GREETING_STYLE
        + BAD_COLD_FAST_REPLY_STYLE
        + BAD_LIGHT_CATGIRL_STYLE
    ):
        assert phrase.casefold() not in text.casefold(), text
    assert not text.startswith("\u5594\uff0c"), text


def _assert_tight_choice_pool(options: list[str], *, limit: int = 36, min_size: int = 5) -> None:
    assert options, options
    assert len(options) >= min_size, options
    assert sum(1 for option in options if "\u4e3b\u4eba" in option) <= 1, options
    assert sum(1 for option in options if "\u55b5" in option) <= 1, options
    for option in options:
        _assert_tight_social_text(str(option), limit=limit)


def _social_similarity_key(text: str) -> str:
    value = str(text or "").casefold()
    value = re.sub(r"[\s\u3000\uff0c,.\u3002!！?？~～]+", "", value)
    for marker in ["hi", "hello", "\u6708\u6708", "\u4e3b\u4eba", "\u55b5"]:
        value = value.replace(marker.casefold(), "")
    return value


def _assert_not_near_repeat(previous: str, current: str) -> None:
    prev_key = _social_similarity_key(previous)
    current_key = _social_similarity_key(current)
    assert prev_key and current_key, (previous, current)
    assert prev_key != current_key, (previous, current)
    shorter, longer = sorted([prev_key, current_key], key=len)
    assert not (len(shorter) >= 3 and shorter in longer), (previous, current)


response_composer._CACHE = Path(tempfile.gettempdir()) / "yueyue_response_composer_check_recent.json"
with contextlib.suppress(FileNotFoundError):
    response_composer._CACHE.unlink()

cases = [
    compose_fast_reply(
        BrokenAgent(),
        kind="time_query",
        owner_prompt="現在幾點",
        facts={"exact_time": "現在是 2026-06-24 10:30:00 喵～"},
        fallback={"content": "現在是 2026-06-24 10:30:00 喵～"},
    ),
    compose_fast_reply(
        GoodAgent(),
        kind="sticker_send",
        owner_prompt="發個表情包",
        facts={"sticker_marker": "[表情包: Acting cute.png]", "sticker_name": "Acting cute.png"},
        fallback={"content": "給你\n[表情包: Acting cute.png]"},
    ),
    compose_fast_reply(
        GoodAgent(),
        kind="sticker_cancel",
        owner_prompt="不要發這個表情包",
        facts={},
        fallback={"content": "好，這張先不發。"},
    ),
]

assert "10:30" in cases[0]["content"], cases[0]
assert not re.search(r"20\d{2}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{2}", cases[0]["content"]), cases[0]
assert "[表情包: Acting cute.png]" in cases[1]["content"], cases[1]
assert "[表情包:" not in cases[2]["content"], cases[2]

long_sticker = compose_fast_reply(
    LongStickerAgent(),
    kind="sticker_send",
    owner_prompt="send a sticker",
    facts={"sticker_marker": "[sticker: Acting cute.png]", "sticker_name": "Acting cute.png"},
    fallback={"content": "ok\n[sticker: Acting cute.png]"},
)

overexplained_sticker = compose_fast_reply(
    OverExplainingStickerAgent(),
    kind="sticker_send",
    owner_prompt="send a sticker",
    facts={"sticker_marker": "[sticker: Acting cute.png]", "sticker_name": "Acting cute.png"},
    fallback={"content": "ok\n[sticker: Acting cute.png]"},
)

context_sticky_sticker = compose_fast_reply(
    ContextStickyStickerAgent(),
    kind="sticker_send",
    owner_prompt="\u767c\u500b\u8868\u60c5\u5305",
    facts={"sticker_marker": "[sticker: Acting cute.png]", "sticker_name": "Acting cute.png"},
    fallback={"content": "ok\n[sticker: Acting cute.png]"},
)

assert "[sticker: Acting cute.png]" in long_sticker["content"], long_sticker
assert len(long_sticker["content"].splitlines()[0]) <= 48, long_sticker
assert "[sticker: Acting cute.png]" in overexplained_sticker["content"], overexplained_sticker
assert "cannot send" not in overexplained_sticker["content"].casefold(), overexplained_sticker
assert "refresh" not in overexplained_sticker["content"].casefold(), overexplained_sticker
assert "this one fits" not in overexplained_sticker["content"].casefold(), overexplained_sticker
assert "sending this one" not in overexplained_sticker["content"].casefold(), overexplained_sticker
assert "this one works" not in overexplained_sticker["content"].casefold(), overexplained_sticker
assert "here, this one" not in overexplained_sticker["content"].casefold(), overexplained_sticker
assert "[sticker: Acting cute.png]" in context_sticky_sticker["content"], context_sticky_sticker
assert "\u525b\u9192" not in context_sticky_sticker["content"], context_sticky_sticker
assert "\u8166\u888b\u958b\u6d1e" not in context_sticky_sticker["content"], context_sticky_sticker
assert "\u4e3b\u4eba\uff5e\u62ff\u53bb" not in context_sticky_sticker["content"], context_sticky_sticker
assert len(context_sticky_sticker["content"].splitlines()[0]) <= 36, context_sticky_sticker
_assert_tight_choice_pool(response_composer._sticker_send_options())
_assert_tight_choice_pool(response_composer._sticker_resend_options())
for option in response_composer._sticker_send_options() + response_composer._sticker_resend_options():
    for phrase in BAD_LIGHT_CATGIRL_STYLE + BAD_PROCESSING_STYLE:
        assert phrase.casefold() not in option.casefold(), option
    for phrase in [
        "\u55cf",
        "\u9019\u5f35\u6709\u9ede\u4e56",
        "\u8fd9\u5f20\u6709\u70b9\u4e56",
        "\u6536\u597d",
        "\u518d\u7d66\u4f60\u770b\u4e00\u4e0b",
        "\u518d\u7ed9\u4f60\u770b\u4e00\u4e0b",
        "\u63db\u500b\u5c0f\u5c0f\u7684\u7d66\u4f60",
        "\u6362\u4e2a\u5c0f\u5c0f\u7684\u7ed9\u4f60",
        "\u4e0d\u5435\u4f60",
    ]:
        assert phrase.casefold() not in option.casefold(), option

resend_agent = ExplodingAgent()
resend_reply = compose_fast_reply(
    resend_agent,
    kind="sticker_resend",
    owner_prompt="\u518d\u767c\u4e00\u6b21",
    facts={"sticker_marker": "[sticker: Acting cute.png]", "sticker_name": "Acting cute.png"},
    fallback={"content": "\u6211\u518d\u88dc\u767c\u4e00\u6b21\u3002\n[sticker: Acting cute.png]"},
)
assert "[sticker: Acting cute.png]" in resend_reply["content"], resend_reply
_assert_tight_social_text(resend_reply["content"].splitlines()[0], limit=36)
assert resend_agent.calls == 0, resend_agent.calls

# sticker_send/sticker_resend now try model generation first (agent.provider.chat),
# same pattern as plain_greeting - verify generated text is used when valid, and the
# pool fallback still kicks in when generation fails validation.
generated_send_agent = GeneratedGreetingAgent("接好嘍，這張給你啦。")
generated_send_reply = compose_fast_reply(
    generated_send_agent,
    kind="sticker_send",
    owner_prompt="發個表情包",
    facts={"sticker_marker": "[sticker: Acting cute.png]", "sticker_name": "Acting cute.png"},
    fallback={"content": "ok\n[sticker: Acting cute.png]"},
)
assert generated_send_agent.provider.calls == 1, generated_send_agent.provider.calls
assert generated_send_reply["content"] == "接好嘍，這張給你啦。\n[sticker: Acting cute.png]", generated_send_reply

generated_resend_agent = GeneratedGreetingAgent("好啦，再補一次給你。")
generated_resend_reply = compose_fast_reply(
    generated_resend_agent,
    kind="sticker_resend",
    owner_prompt="再發一次",
    facts={"sticker_marker": "[sticker: Acting cute.png]", "sticker_name": "Acting cute.png"},
    fallback={"content": "ok\n[sticker: Acting cute.png]"},
)
assert generated_resend_agent.provider.calls == 1, generated_resend_agent.provider.calls
assert generated_resend_reply["content"] == "好啦，再補一次給你。\n[sticker: Acting cute.png]", generated_resend_reply

# A model reply that fails validation (too long) must fall through to the pooled
# options, not be used verbatim.
invalid_send_agent = GeneratedGreetingAgent("ok " * 80)
invalid_send_reply = compose_fast_reply(
    invalid_send_agent,
    kind="sticker_send",
    owner_prompt="發個表情包",
    facts={"sticker_marker": "[sticker: Acting cute.png]", "sticker_name": "Acting cute.png"},
    fallback={"content": "ok\n[sticker: Acting cute.png]"},
)
assert invalid_send_reply["content"] in [
    f"{option}\n[sticker: Acting cute.png]" for option in response_composer._sticker_send_options()
], invalid_send_reply

# Production default is model-generated greetings (see below). This block explicitly
# forces the flag off to keep the micro-composer fallback path itself under test,
# regardless of what the module-level default is.
flag_before_disabled_block = response_composer.plain_greeting_social_generation_enabled
response_composer.plain_greeting_social_generation_enabled = False
try:
    exploding_agent = ExplodingAgent()

    captured_greeting_pools = []
    original_choice = response_composer.random.choice

    def _capture_first(options):
        pool = [str(item) for item in options]
        captured_greeting_pools.append(pool)
        return pool[0]

    response_composer.random.choice = _capture_first
    try:
        for greeting in [
            "\u4f60\u597d",
            "hi",
            "hi\u4f60\u597d",
            "hi\u4f60\u597d\u6708\u6708",
        ]:
            greeting_reply = compose_fast_reply(
                exploding_agent,
                kind="plain_greeting",
                owner_prompt=greeting,
                facts={},
                fallback={"content": ""},
            )
            _assert_tight_social_text(greeting_reply["content"])
    finally:
        response_composer.random.choice = original_choice

    assert captured_greeting_pools, captured_greeting_pools
    for pool in captured_greeting_pools:
        _assert_tight_choice_pool(pool, min_size=1)
    _assert_tight_choice_pool(response_composer._plain_greeting_options("\u4f60\u597d"))
    _assert_tight_choice_pool(response_composer._plain_greeting_options("hi\u4f60\u597d\u6708\u6708"))
    assert set(captured_greeting_pools[0]).isdisjoint(set(captured_greeting_pools[-1])), captured_greeting_pools
    assert exploding_agent.calls == 0, exploding_agent.calls

    valid_generated_agent = GeneratedGreetingAgent("\u55ef\u54fc\uff0c\u4eca\u5929\u7b97\u4f60\u4e56\u3002")
    micro_default_reply = compose_fast_reply(
        valid_generated_agent,
        kind="plain_greeting",
        owner_prompt="\u4f60\u597d",
        facts={},
        fallback={"content": ""},
    )
    assert valid_generated_agent.provider.calls == 0, valid_generated_agent.provider.calls
    assert micro_default_reply.get("_composer_source") == "micro_composer", micro_default_reply
    _assert_tight_social_text(micro_default_reply["content"])
finally:
    response_composer.plain_greeting_social_generation_enabled = flag_before_disabled_block

# Production default: model-generated greetings are on. Verify that's actually true today,
# so this check fails loudly if someone flips it back off without updating this file.
assert response_composer.plain_greeting_social_generation_enabled is True

original_generation_flag = response_composer.plain_greeting_social_generation_enabled
response_composer.plain_greeting_social_generation_enabled = True
try:
    valid_generated_agent = GeneratedGreetingAgent("\u55ef\u54fc\uff0c\u4eca\u5929\u7b97\u4f60\u4e56\u3002")
    valid_generated = compose_fast_reply(
        valid_generated_agent,
        kind="plain_greeting",
        owner_prompt="\u4f60\u597d",
        facts={},
        fallback={"content": ""},
    )
finally:
    response_composer.plain_greeting_social_generation_enabled = original_generation_flag
assert valid_generated["content"] == "\u55ef\u54fc\uff0c\u4eca\u5929\u7b97\u4f60\u4e56\u3002", valid_generated
assert valid_generated.get("_composer_source") == "generated", valid_generated
assert valid_generated_agent.provider.calls == 1, valid_generated_agent.provider.calls
assert valid_generated_agent.provider.tools_seen == [], valid_generated_agent.provider.tools_seen
prompt_text = "\n".join(str(item.get("content", "")) for item in valid_generated_agent.provider.messages[0])
assert "execute_command" not in prompt_text, prompt_text
assert "permission" not in prompt_text.casefold(), prompt_text
assert "\u9019\u4e00\u6b65\u9700\u8981\u4f60\u9ede\u982d" not in prompt_text, prompt_text
assert "\u4e0d\u8981\u8aaa\u6536\u5230" in prompt_text, prompt_text
assert "\u4e0d\u8981\u5f15\u7528" in prompt_text, prompt_text
assert "\u4e3b\u4eba\u525b\u525b\u53ea\u662f\u5728\u6253\u62db\u547c" not in prompt_text, prompt_text
assert "\uff1a\u4f60\u597d" not in prompt_text, prompt_text
_assert_tight_social_text(valid_generated["content"])

invalid_generated_cases = [
    "\u9019\u4e00\u6b65\u9700\u8981\u4f60\u9ede\u982d\uff0c\u6211\u624d\u80fd\u7e7c\u7e8c\u3002",
    "\u55ef\uff0c\u6708\u6708\u5728\u3002\n\u4f60\u8aaa\u5427\u3002",
    "\u6708\u6708\u5728\u9019\u88e1\u5462\uff0c\u4eca\u5929\u8981\u597d\u597d\u966a\u4f60\u8aaa\u8a71\uff0c\u4e0d\u8981\u518d\u5230\u8655\u4e82\u8dd1\u4e86\uff0c\u4e5f\u4e0d\u8981\u4e00\u76f4\u88dd\u4f5c\u6c92\u770b\u5230\u6708\u6708\u3002",
    "\u73fe\u5728\u662f 2026-06-25 20:14:18 \u4e2d\u570b\u6a19\u6e96\u6642\u9593\u3002",
    "\u55b2\uff0c\u5c31\u8fd9\uff1f",
    "\u4f60\u597d\uff1f",
    "\u4f60\u597d \u5594\uff5e\u539f\u4f86\u662f\u6253\u62db\u547c\u554a\uff0c\u6708\u6708\u9084\u4ee5\u70ba\u662f\u4ec0\u9ebc\u5927\u4e8b\u5462\uff0c\u7d50\u679c\u5c31\u9019\u9ebc\u6577\u884d\u4e00\u53e5\u3002",
    "\u6708\u6708\u9084\u4ee5\u70ba\u6703\u6709\u66f4\u597d\u73a9\u7684\u5462\uff5e",
    "\u54fc\uff0c\u8ab0\u770b\u4e0d\u51fa\u4f86\u4f60\u5728\u6253\u62db\u547c\u554a\uff5e",
    "\u543c\uff5e\u6708\u6708\u770b\u5f97\u51fa\u4f86\u5566\uff0c\u5077\u7b11\u4e2d\u3002",
    "\u597d\u5566\uff0c\u5225\u6643\uff0c\u6708\u6708\u5728\u3002",
    "\u5225\u5750\u90a3\u9ebc\u76f4\uff0c\u6708\u6708\u807d\u8457\u3002",
    "\u7ad9\u597d\uff0c\u6708\u6708\u624d\u8981\u56de\u4f60\u3002",
    "\u55b5\uff0c\u6b6a\u982d\u4e2d\u3002",
    "\u55ef\uff0c\u7728\u773c\u4e2d\u3002",
    "\u54fc\uff0c\u6708\u6708\u4e00\u773c\u5c31\u770b\u51fa\u4f60\u5728\u60f3\u4ec0\u9ebc\u5566\u3002",
    "\u6708\u6708\u77e5\u9053\u4f60\u60f3\u627e\u6211\u3002",
    "\u662f\u4e0d\u662f\u60f3\u6708\u6708\u4e86\uff1f",
    "\u80af\u5b9a\u662f\u60f3\u6708\u6708\u624d\u4f86\u7684\u5427\u3002",
    "\u55e8\uff5e\u6536\u5230\u5566\uff0c\u6708\u6708\u6709\u5728\u807d\u3002\uff08\u6311\u7709\uff09",
    "\uff08\u6311\u7709\uff09",
    "\uff08\u6b6a\u982d\uff09",
    "\u55ef\uff0c\u6708\u6708\u5077\u7b11\u4e2d\u3002",
    "\u54fc\uff0c\u9084\u7279\u5730\u89e3\u91cb\uff0c\u6708\u6708\u77e5\u9053\u5566\uff5e",
    "\u6536\u5230\u5566\uff0c\u6708\u6708\u6709\u5728\u807d\u3002",
    "\u4f60\u597d\u5440\uff5e\u6708\u6708\u6536\u5230\u4e86\u3002",
    "\u6536\u5230\uff0c\u9019\u8072\u7b97\u4f60\u4e56\u3002",
    "\u77e5\u9053\u5566\uff5e\u9019\u8072\u4e0d\u7528\u89e3\u91cb\uff0c\u6708\u6708\u61c2\u7684\u3002",
    "\u6708\u6708\u77e5\u9053\u3002",
    "\u6708\u6708\u61c2\u3002",
    "\u9019\u8072\u6708\u6708\u6536\u4e0b\u4e86\u3002",
    "\u9019\u53e5\u6708\u6708\u5148\u8a18\u4e0b\u3002",
    "\u9019\u500b\u62db\u547c\u6708\u6708\u807d\u5230\u4e86\u3002",
    "\u554f\u5019\u6536\u5230\u4e86\u3002",
    "\u54fc\uff0c\u4e00\u53e5\u4f60\u597d\u5c31\u60f3\u6253\u767c\u6708\u6708\uff1f",
    "\u4e00\u53e5hi\u5c31\u5920\u4e86\u55ce\uff1f",
    "\u4e00\u8072\u4f60\u597d\u5c31\u60f3\u6df7\u904e\u53bb\u554a\uff1f",
    "\u4e00\u500b\u4f60\u597d\u5c31\u6253\u767c\u6708\u6708\uff1f",
    "\u53ea\u8aaa\u4f60\u597d\u5c31\u8dd1\uff1f",
    "\u53ea\u6703\u4f60\u597d\u55ce\uff1f",
    "\u5225\u6577\u884d\u6708\u6708\u3002",
    "\u54fc\uff0c\u6708\u6708\u51c6\u4e86\u3002",
    "\u6279\u51c6\u4f60\u51fa\u73fe\u3002",
]
for bad_generated in invalid_generated_cases:
    original_generation_flag = response_composer.plain_greeting_social_generation_enabled
    response_composer.plain_greeting_social_generation_enabled = True
    try:
        bad_agent = GeneratedGreetingAgent(bad_generated)
        fallback_reply = compose_fast_reply(
            bad_agent,
            kind="plain_greeting",
            owner_prompt="hi\u4f60\u597d\u6708\u6708",
            facts={},
            fallback={"content": ""},
        )
    finally:
        response_composer.plain_greeting_social_generation_enabled = original_generation_flag
    assert fallback_reply.get("_composer_source") == "fallback", fallback_reply
    assert bad_agent.provider.calls == 1, bad_agent.provider.calls
    assert fallback_reply["content"] != bad_generated, fallback_reply
    _assert_tight_social_text(fallback_reply["content"])

original_micro = response_composer._compose_micro_plain_greeting
try:
    response_composer._compose_micro_plain_greeting = lambda owner_prompt, recent: "\u6536\u5230"
    forced_fallback = compose_fast_reply(
        ExplodingAgent(),
        kind="plain_greeting",
        owner_prompt="\u4f60\u597d",
        facts={},
        fallback={"content": ""},
    )
finally:
    response_composer._compose_micro_plain_greeting = original_micro
assert forced_fallback.get("_composer_source") == "fallback", forced_fallback
_assert_tight_social_text(forced_fallback["content"])

no_provider_agent = ExplodingAgent()
fallback_without_provider = compose_fast_reply(
    no_provider_agent,
    kind="plain_greeting",
    owner_prompt="\u4f60\u597d",
    facts={},
    fallback={"content": ""},
)
# Production default has generation enabled: an agent with no `.provider` attribute
# (ExplodingAgent only exposes `.chat` directly) falls straight through to the pooled
# fallback without ever reaching `agent.chat`.
assert fallback_without_provider.get("_composer_source") == "fallback", fallback_without_provider
assert no_provider_agent.calls == 0, no_provider_agent.calls
_assert_tight_social_text(fallback_without_provider["content"])

with contextlib.suppress(FileNotFoundError):
    response_composer._CACHE.unlink()

sequence_agent = ExplodingAgent()
sequence_choices = []


def _always_first(options):
    pool = [str(item) for item in options]
    sequence_choices.append(pool)
    return pool[0]


response_composer.random.choice = _always_first
try:
    greeting_sequence = [
        compose_fast_reply(
            sequence_agent,
            kind="plain_greeting",
            owner_prompt=prompt,
            facts={},
            fallback={"content": ""},
        )["content"]
        for prompt in [
            "\u4f60\u597d",
            "hi\u4f60\u597d\u6708\u6708",
            "\u4f60\u597d",
        ]
    ]
finally:
    response_composer.random.choice = original_choice

assert len(greeting_sequence) == 3, greeting_sequence
for reply in greeting_sequence:
    _assert_tight_social_text(reply)
_assert_not_near_repeat(greeting_sequence[0], greeting_sequence[1])
_assert_not_near_repeat(greeting_sequence[1], greeting_sequence[2])
_assert_not_near_repeat(greeting_sequence[0], greeting_sequence[2])
assert sequence_agent.calls == 0, sequence_agent.calls

response_composer._CACHE.write_text(
    "[\n"
    '  "\u5728\u554a\uff0c\u7b28\u86cb\u7d42\u65bc\u51fa\u73fe\u4e86\u3002",\n'
    '  "Hi\uff0c\u6708\u6708\u770b\u5230\u4f60\u4e86\u3002",\n'
    '  "\u55ef\u54fc\uff0chi\u5b8c\u4e86\u8aaa\u6b63\u4e8b\u3002",\n'
    '  "\u6708\u6708\u5728\uff0c\u4eca\u5929\u6c92\u8ff7\u8def\u561b\u3002"\n'
    "]",
    encoding="utf-8",
)

response_composer.random.choice = _always_first
try:
    family_guard_reply = compose_fast_reply(
        sequence_agent,
        kind="plain_greeting",
        owner_prompt="hi\u4f60\u597d\u6708\u6708",
        facts={},
        fallback={"content": ""},
    )["content"]
finally:
    response_composer.random.choice = original_choice

_assert_tight_social_text(family_guard_reply)
assert "\u7d42\u65bc" not in family_guard_reply, family_guard_reply
assert "\u7ec8\u4e8e" not in family_guard_reply, family_guard_reply

assert getattr(response_composer, "is_simple_wake_greeting", lambda text: False)(
    "\u65e9\u4e0a\u597d\uff0c\u525b\u9192"
)
wake_reply = compose_fast_reply(
    exploding_agent,
    kind="wake_greeting",
    owner_prompt="\u65e9\u4e0a\u597d\uff0c\u525b\u9192",
    facts={"period": "\u65e9\u4e0a"},
    fallback={"content": ""},
)
_assert_tight_social_text(wake_reply["content"], limit=42)
assert exploding_agent.calls == 0, exploding_agent.calls

for non_morning_period in ["\u4e0b\u5348", "\u665a\u4e0a", "\u51cc\u6668"]:
    wrong_time_wake_reply = compose_fast_reply(
        exploding_agent,
        kind="wake_greeting",
        owner_prompt="\u65e9\u4e0a\u597d\uff0c\u525b\u9192",
        facts={
            "period": non_morning_period,
            "contradiction": "temporal contradiction: user greeting suggests morning, but current period differs.",
        },
        fallback={"content": ""},
    )
    wrong_time_content = wrong_time_wake_reply["content"]
    _assert_tight_social_text(wrong_time_content, limit=42)
    assert non_morning_period in wrong_time_content or "\u4e0d\u662f\u65e9\u4e0a" in wrong_time_content, wrong_time_wake_reply
    assert not wrong_time_content.startswith("\u65e9"), wrong_time_wake_reply
    assert not any(mark in wrong_time_content[:-1] for mark in ["\u3002", "\uff01", "\uff1f", "!", "?", "~"]), wrong_time_wake_reply
    assert "\u6839\u64da" not in wrong_time_content and "\u7576\u524d\u6642\u9593" not in wrong_time_content, wrong_time_wake_reply
    assert "\u9700\u8981\u4f60\u9ede\u982d" not in wrong_time_content, wrong_time_wake_reply
    assert "execute_command" not in wrong_time_content, wrong_time_wake_reply
assert exploding_agent.calls == 0, exploding_agent.calls

deterministic_time = compose_fast_reply(
    exploding_agent,
    kind="time_query",
    owner_prompt="\u73fe\u5728\u662f\u4ec0\u9ebc\u65e5\u671f\uff1f",
    facts={
        "exact_time": "\u73fe\u5728\u662f 2026-06-28 17:46:13 HKT\uff0c\u4eca\u5929\u662f\u661f\u671f\u65e5\u3002",
        "date": "2026-06-28",
        "time": "17:46",
        "time_with_seconds": "17:46:13",
        "weekday": "\u661f\u671f\u65e5",
        "period": "\u4e0b\u5348",
        "timezone": "HKT",
    },
    fallback={"content": ""},
)
assert "2026-06-28" in deterministic_time["content"], deterministic_time
assert "\u661f\u671f\u65e5" in deterministic_time["content"], deterministic_time
assert not re.search(r"20\d{2}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{2}", deterministic_time["content"]), deterministic_time
assert "2026-06-25 20:14:18" not in deterministic_time["content"], deterministic_time
assert exploding_agent.calls == 0, exploding_agent.calls

repeated_time = compose_fast_reply(
    exploding_agent,
    kind="time_query",
    owner_prompt="\u5e7e\u9ede",
    facts={
        "exact_time": "\u73fe\u5728\u662f 2026-06-28 17:47:08 HKT\uff0c\u4eca\u5929\u662f\u661f\u671f\u65e5\u3002",
        "date": "2026-06-28",
        "time": "17:47",
        "time_with_seconds": "17:47:08",
        "weekday": "\u661f\u671f\u65e5",
        "period": "\u4e0b\u5348",
        "timezone": "HKT",
    },
    fallback={"content": ""},
)
assert "17:47" in repeated_time["content"], repeated_time
assert "2026-06-25 20:14:18" not in repeated_time["content"], repeated_time
assert not re.search(r"20\d{2}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{2}", repeated_time["content"]), repeated_time
assert exploding_agent.calls == 0, exploding_agent.calls

night_period = compose_fast_reply(
    exploding_agent,
    kind="time_query",
    owner_prompt="\u73fe\u5728\u662f\u665a\u4e0a\u55ce",
    facts={
        "exact_time": "\u73fe\u5728\u662f 2026-06-29 01:30:00 HKT\uff0c\u4eca\u5929\u662f\u661f\u671f\u4e00\u3002",
        "date": "2026-06-29",
        "time": "01:30",
        "time_with_seconds": "01:30:00",
        "weekday": "\u661f\u671f\u4e00",
        "period": "\u51cc\u6668",
        "timezone": "HKT",
    },
    fallback={"content": ""},
)
assert "\u51cc\u6668" in night_period["content"] or "\u665a\u4e0a" in night_period["content"], night_period
assert "\u9700\u8981\u4f60\u9ede\u982d" not in night_period["content"], night_period
assert "execute_command" not in night_period["content"], night_period
assert not re.search(r"20\d{2}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{2}", night_period["content"]), night_period

daylight_period = compose_fast_reply(
    exploding_agent,
    kind="time_query",
    owner_prompt="\u5916\u9762\u662f\u9ed1\u5929\u9084\u662f\u767d\u5929",
    facts={
        "exact_time": "\u73fe\u5728\u662f 2026-06-29 15:20:00 HKT\uff0c\u4eca\u5929\u662f\u661f\u671f\u4e00\u3002",
        "date": "2026-06-29",
        "time": "15:20",
        "time_with_seconds": "15:20:00",
        "weekday": "\u661f\u671f\u4e00",
        "period": "\u4e0b\u5348",
        "timezone": "HKT",
    },
    fallback={"content": ""},
)
assert "\u767d\u5929" in daylight_period["content"], daylight_period
assert "\u770b\u4e0d\u5230" not in daylight_period["content"], daylight_period
assert "2026-06-25 20:14:18" not in daylight_period["content"], daylight_period

print("PASS response_composer_check")
