from __future__ import annotations

import datetime
import time

import pytest

import main
from agent_latency import InteractionMode
from agent_short_context import DEFAULT_SHORT_CONTEXT_BUFFER, ShortContextTurn


def _late_night_timestamp(hour: int = 3) -> float:
    """A timestamp guaranteed to fall inside the late-night pacing window in local time."""
    return datetime.datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0).timestamp()


def _daytime_timestamp(hour: int = 14) -> float:
    return datetime.datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0).timestamp()


@pytest.fixture
def seeded_chat_id():
    """A disposable chat id with no recorded turns, so rapid-turn pacing never accidentally
    triggers in tests that are not specifically testing it."""
    chat_id = "test_reply_pacing_chat"
    DEFAULT_SHORT_CONTEXT_BUFFER.turns.pop(chat_id, None)
    yield chat_id
    DEFAULT_SHORT_CONTEXT_BUFFER.turns.pop(chat_id, None)


def test_urgent_signal_always_skips_pacing_even_in_chat_mode(seeded_chat_id, monkeypatch):
    """Mandatory safety valve: an urgent/help-seeking signal in the owner's message must get an
    instant reply, even though every other pacing condition is stacked to force a delay."""
    monkeypatch.setattr(main, "_reply_pacing_probability", lambda: 1.0)
    now = _late_night_timestamp()
    for urgent_text in ["救命啊怎麼辦", "緊急！需要幫忙", "SOS 出事了", "help me now"]:
        delay = main.reply_pacing_delay_seconds(seeded_chat_id, urgent_text, InteractionMode.CHAT, now=now)
        assert delay == 0.0, f"urgent text must never be delayed: {urgent_text!r}"


def test_low_mood_signal_always_skips_pacing_even_in_chat_mode(seeded_chat_id, monkeypatch):
    """Same mandatory safety valve, for low-mood signals (reusing agent_presence's own
    LOW_MOOD_MARKERS definition, so the two systems agree on what counts as low mood)."""
    monkeypatch.setattr(main, "_reply_pacing_probability", lambda: 1.0)
    now = _late_night_timestamp()
    for low_mood_text in ["今天好累好崩潰", "壓力好大快撐不住了", "I'm so stressed"]:
        delay = main.reply_pacing_delay_seconds(seeded_chat_id, low_mood_text, InteractionMode.CHAT, now=now)
        assert delay == 0.0, f"low-mood text must never be delayed: {low_mood_text!r}"


def test_pacing_can_trigger_for_ordinary_chat_at_late_night(seeded_chat_id, monkeypatch):
    """Proves the feature actually fires when nothing blocks it: ordinary text, late night,
    forced probability, no urgent/low-mood signal, CHAT mode."""
    monkeypatch.setattr(main, "_reply_pacing_probability", lambda: 1.0)
    now = _late_night_timestamp()
    delay = main.reply_pacing_delay_seconds(seeded_chat_id, "今天天氣不錯欸", InteractionMode.CHAT, now=now)
    assert delay > 0.0
    low, high = main._REPLY_PACING_DELAY_RANGE_SECONDS
    assert low <= delay <= high


def test_pacing_can_trigger_from_rapid_recent_turns(seeded_chat_id, monkeypatch):
    """Rapid back-and-forth is the other contextual trigger, independent of time of day."""
    monkeypatch.setattr(main, "_reply_pacing_probability", lambda: 1.0)
    now = _daytime_timestamp()
    DEFAULT_SHORT_CONTEXT_BUFFER.turns[seeded_chat_id] = [
        ShortContextTurn(chat_id=seeded_chat_id, text=f"turn {i}", created_at=now - 30 * i)
        for i in range(main._REPLY_PACING_RAPID_TURN_COUNT)
    ]
    delay = main.reply_pacing_delay_seconds(seeded_chat_id, "又來啦哈哈", InteractionMode.CHAT, now=now)
    assert delay > 0.0


def test_pacing_never_fires_without_a_contextual_trigger(seeded_chat_id, monkeypatch):
    """Daytime, no rapid turns: even with probability forced to 1.0, there is no contextual
    reason to pace, so the delay must stay 0."""
    monkeypatch.setattr(main, "_reply_pacing_probability", lambda: 1.0)
    now = _daytime_timestamp()
    delay = main.reply_pacing_delay_seconds(seeded_chat_id, "今天天氣不錯欸", InteractionMode.CHAT, now=now)
    assert delay == 0.0


@pytest.mark.parametrize(
    "mode",
    [InteractionMode.TOOL_TASK, InteractionMode.VISION_TASK, InteractionMode.SCREEN_OBSERVE, None],
)
def test_pacing_never_fires_outside_chat_social_modes(seeded_chat_id, monkeypatch, mode):
    """Structural gate: task-oriented modes (and the None default used by every fast-path
    reply) can never be paced, even at forced probability=1.0 during late night."""
    monkeypatch.setattr(main, "_reply_pacing_probability", lambda: 1.0)
    now = _late_night_timestamp()
    delay = main.reply_pacing_delay_seconds(seeded_chat_id, "幫我打開設定看一下", mode, now=now)
    assert delay == 0.0


def test_pacing_probability_env_override(monkeypatch):
    monkeypatch.setenv(main._REPLY_PACING_PROBABILITY_ENV, "0.4")
    assert main._reply_pacing_probability() == pytest.approx(0.4)
    monkeypatch.setenv(main._REPLY_PACING_PROBABILITY_ENV, "not-a-number")
    assert main._reply_pacing_probability() == main._DEFAULT_REPLY_PACING_PROBABILITY


def test_reply_pacing_delay_is_fast_by_default(seeded_chat_id):
    """Sanity check the real default: an ordinary daytime, low-frequency turn must resolve
    quickly (no delay), so this test itself does not hang the suite."""
    start = time.monotonic()
    delay = main.reply_pacing_delay_seconds(seeded_chat_id, "嗨", InteractionMode.CHAT, now=_daytime_timestamp())
    assert delay == 0.0
    assert time.monotonic() - start < 1.0
