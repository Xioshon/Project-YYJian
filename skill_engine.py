"""Lightweight, extensible skill layer (ROADMAP Phase 6 — the skills/plugins-parity answer).

Design goals from the owner (2026-07-16): add PRACTICAL capabilities; the model must know WHEN to
invoke which; keep it provider-agnostic so the project is publishable (skill handlers are pure
Python — only the router uses the already-env-configurable chat model).

Flow (cost-aware): a cheap keyword PREFILTER gates everything, so ordinary chatter never triggers
a model call. Only when a message plausibly wants a skill do we ask the chat model to pick the
skill and extract args (one small JSON call). Execution is deterministic Python. The caller then
lets YueYue phrase the outcome in-persona, so nothing is canned.

Adding a skill = append a Skill to SKILLS with a clear `when` (routing description) and a handler.
That is the whole extension surface — no code edits elsewhere.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from reminders import DEFAULT_REMINDER_STORE


@dataclass
class SkillContext:
    chat_id: str
    now: float


@dataclass
class SkillResult:
    # A neutral, factual note the persona layer turns into YueYue's own words. Never shown raw.
    note: str
    ok: bool = True


@dataclass
class Skill:
    name: str
    when: str                                   # routing description: what it does + when to use
    args_hint: str                              # what the model should extract
    handler: Callable[[dict, SkillContext], SkillResult]
    keywords: list[str] = field(default_factory=list)  # prefilter triggers


# ---------------------------------------------------------------------------- reminder skills

def _fmt_when(seconds_from_now: float, now: float) -> str:
    target = time.localtime(now + max(0, seconds_from_now))
    return time.strftime("%m月%d日 %H:%M", target)


def _skill_set_reminder(args: dict, ctx: SkillContext) -> SkillResult:
    text = str(args.get("what") or "").strip()
    try:
        delay = float(args.get("fire_in_seconds"))
    except (TypeError, ValueError):
        delay = -1.0
    if not text or delay < 0:
        return SkillResult("沒能弄清楚要提醒什麼、或幾點提醒——需要主人再說清楚一點", ok=False)
    if delay < 5:
        delay = 5.0
    fire_at = ctx.now + delay
    DEFAULT_REMINDER_STORE.add(ctx.chat_id, fire_at, text)
    return SkillResult(f"已經幫主人設好提醒：「{text}」，會在 {_fmt_when(delay, ctx.now)} 準時提醒你")


def _skill_list_reminders(args: dict, ctx: SkillContext) -> SkillResult:
    pending = DEFAULT_REMINDER_STORE.pending(ctx.chat_id)
    if not pending:
        return SkillResult("主人目前沒有任何待提醒的事")
    lines = [f"「{r.text}」（{_fmt_when(r.fire_at - ctx.now, ctx.now)}）" for r in pending[:8]]
    return SkillResult("主人還有這些提醒：" + "；".join(lines))


def _skill_cancel_reminder(args: dict, ctx: SkillContext) -> SkillResult:
    count = DEFAULT_REMINDER_STORE.cancel(ctx.chat_id)
    if not count:
        return SkillResult("本來就沒有待提醒的事，不用取消")
    return SkillResult(f"已經幫主人取消了 {count} 個提醒")


SKILLS: list[Skill] = [
    Skill(
        "set_reminder",
        when="主人要在某個時間點或某段時間後被提醒做某件事（設鬧鐘/提醒/叫我）",
        args_hint='{"what": "要提醒的內容", "fire_in_seconds": 距離現在幾秒後觸發的整數}',
        handler=_skill_set_reminder,
        keywords=["提醒", "叫我", "鬧鐘", "闹钟", "记得叫", "記得叫", "分鐘後", "分钟后", "小時後",
                  "小时后", "等下提", "待會提", "到點", "到点", "計時", "计时", "倒數", "倒计时"],
    ),
    Skill(
        "list_reminders",
        when="主人想知道自己有哪些待提醒/鬧鐘",
        args_hint="{}",
        handler=_skill_list_reminders,
        keywords=["有什麼提醒", "有什么提醒", "哪些提醒", "我的提醒", "還有什麼要提", "看提醒", "提醒列表"],
    ),
    Skill(
        "cancel_reminder",
        when="主人想取消/清掉提醒或鬧鐘",
        args_hint="{}",
        handler=_skill_cancel_reminder,
        keywords=["取消提醒", "取消鬧鐘", "取消闹钟", "不用提醒", "別提醒", "别提醒", "清掉提醒", "刪提醒", "删提醒"],
    ),
]

_SKILLS_BY_NAME = {s.name: s for s in SKILLS}


def _prefilter(message: str) -> list[Skill]:
    lowered = str(message or "").casefold()
    return [s for s in SKILLS if any(k.casefold() in lowered for k in s.keywords)]


def _route_prompt(message: str, candidates: list[Skill], now: float) -> str:
    now_str = time.strftime("%Y-%m-%d %H:%M (%A)", time.localtime(now))
    lines = [
        "你是技能路由器。判斷主人這句話是否要用下面某個技能，若是，回傳技能名和參數。",
        f"現在時間：{now_str}。",
        "技能：",
    ]
    for skill in candidates:
        lines.append(f"- {skill.name}：{skill.when}；參數 {skill.args_hint}")
    lines.append(
        "只回 JSON：{\"skill\": \"技能名 或 none\", \"args\": {...}}。"
        "fire_in_seconds 要換算成從現在起的秒數（例如「10分鐘後」=600，「明天早上8點」按現在時間算差幾秒）。"
        "如果只是閒聊、沒有明確要用技能，skill 給 none。"
    )
    lines.append(f"主人說：{message}")
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", str(text or ""), re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def try_skill(message: str, chat_id: str, provider: Any, chat_model: str = "", now: float | None = None) -> SkillResult | None:
    """Return a SkillResult if a skill handled the message, else None (fall through to normal chat).

    Provider-agnostic: `provider.chat` is whatever LLM the deployment configured. Only reached when
    the keyword prefilter already matched, so ordinary chat pays nothing."""
    candidates = _prefilter(message)
    if not candidates:
        return None
    now = time.time() if now is None else now
    try:
        kwargs = {"model": chat_model} if chat_model else {}
        response = provider.chat(
            [{"role": "user", "content": _route_prompt(message, candidates, now)}], [], **kwargs
        )
        decision = _extract_json(getattr(response, "content", "") or "")
    except Exception:
        return None
    name = str(decision.get("skill") or "").strip()
    skill = _SKILLS_BY_NAME.get(name)
    if not skill or skill not in candidates:
        return None
    args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
    try:
        return skill.handler(args, SkillContext(chat_id=str(chat_id), now=now))
    except Exception:
        return None
