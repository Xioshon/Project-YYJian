from __future__ import annotations

import tempfile
from pathlib import Path

from agent_latency import InteractionMode, response_policy_for
from main import build_agent
from yueyue_v3.providers import ProviderResponse


class _SpyProvider:
    """Records the reasoning_effort each provider call receives."""

    def __init__(self):
        self.seen: list[tuple[bool, str]] = []

    def chat(self, messages, tools=None, model="", tool_choice="auto", reasoning_effort=""):
        self.seen.append((bool(tools), reasoning_effort))
        return ProviderResponse("好", "", [])

    def close(self):
        pass


def _run(policy_mode):
    spy = _SpyProvider()
    with tempfile.TemporaryDirectory() as tmp:
        agent = build_agent(provider_override=spy, state_dir=Path(tmp) / "v3")
        agent.chat("幫我讀取一下 workspace 裡的 README", response_policy=response_policy_for(policy_mode))
    return spy


def test_task_calls_request_high_reasoning():
    spy = _run(InteractionMode.TOOL_TASK)
    task_efforts = {effort for has_tools, effort in spy.seen if has_tools}
    assert task_efforts, "expected at least one tool-bearing task call"
    assert task_efforts <= {"high"}, f"task calls must carry high reasoning, saw {task_efforts}"


def test_persona_voice_calls_never_carry_reasoning():
    """The owner-facing reply and any chat/persona generation run on the chat voice model,
    which may be a different provider (e.g. MiniMax) that could reject an unknown
    reasoning_effort - so those calls must never carry it."""
    spy = _run(InteractionMode.TOOL_TASK)
    persona_efforts = {effort for has_tools, effort in spy.seen if not has_tools}
    assert persona_efforts <= {""}, f"persona/voice calls must not carry reasoning_effort, saw {persona_efforts}"


def test_pure_chat_turn_carries_no_reasoning():
    spy = _run(InteractionMode.CHAT)
    assert all(effort == "" for _, effort in spy.seen), spy.seen
