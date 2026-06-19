import json
import os
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as day_time
from typing import Any, Callable

from agent_hooks import emit_trace
from agent_llm import SiliconFlowAdapter
from core_tools import PROJECT_CACHE_DIR


PRESENCE_MODE_ENV = "YUEYUE_PRESENCE_MODE"
PRESENCE_DAILY_LIMIT_ENV = "YUEYUE_PRESENCE_DAILY_LIMIT"
PRESENCE_MIN_INTERVAL_ENV = "YUEYUE_PRESENCE_MIN_INTERVAL_MINUTES"
PRESENCE_QUIET_HOURS_ENV = "YUEYUE_PRESENCE_QUIET_HOURS"
PRESENCE_OWNER_AWAKE_WINDOW_ENV = "YUEYUE_PRESENCE_OWNER_AWAKE_MINUTES"
PRESENCE_STALE_TASK_MINUTES_ENV = "YUEYUE_PRESENCE_STALE_TASK_MINUTES"
PRESENCE_TICK_MINUTES_ENV = "YUEYUE_PRESENCE_TICK_MINUTES"
PRESENCE_ICEBREAK_AFTER_ENV = "YUEYUE_PRESENCE_ICEBREAK_AFTER_MINUTES"

DEFAULT_PRESENCE_MODE = "notify"
DEFAULT_DAILY_LIMIT = 8
DEFAULT_MIN_INTERVAL_MINUTES = 75
DEFAULT_QUIET_HOURS = "23:30-08:00"
DEFAULT_OWNER_AWAKE_WINDOW_MINUTES = 30
DEFAULT_STALE_TASK_MINUTES = 120
DEFAULT_TICK_MINUTES = 45
DEFAULT_ICEBREAK_AFTER_MINUTES = 6 * 60

PRESENCE_STATE_FILE = os.path.join(PROJECT_CACHE_DIR, "presence_state.json")
PRESENCE_CANDIDATES_FILE = os.path.join(PROJECT_CACHE_DIR, "presence_candidates.jsonl")
PRESENCE_HEALTH_FILE = os.path.join(PROJECT_CACHE_DIR, "presence_health.json")
PRESENCE_DEBUG_FILE = os.path.join(PROJECT_CACHE_DIR, "presence_debug.jsonl")
PRESENCE_COMPOSER_DEBUG_FILE = os.path.join(PROJECT_CACHE_DIR, "presence_composer_debug.jsonl")
PRESENCE_TOPIC_HISTORY_FILE = os.path.join(PROJECT_CACHE_DIR, "presence_topic_history.jsonl")


@dataclass
class PresenceConfig:
    mode: str = DEFAULT_PRESENCE_MODE
    daily_limit: int = DEFAULT_DAILY_LIMIT
    min_interval_minutes: int = DEFAULT_MIN_INTERVAL_MINUTES
    quiet_hours: str = DEFAULT_QUIET_HOURS
    owner_awake_window_minutes: int = DEFAULT_OWNER_AWAKE_WINDOW_MINUTES
    stale_task_minutes: int = DEFAULT_STALE_TASK_MINUTES
    tick_minutes: int = DEFAULT_TICK_MINUTES
    icebreak_after_minutes: int = DEFAULT_ICEBREAK_AFTER_MINUTES

    @classmethod
    def from_env(cls) -> "PresenceConfig":
        mode = (os.getenv(PRESENCE_MODE_ENV) or DEFAULT_PRESENCE_MODE).strip().casefold()
        if mode not in {"off", "shadow", "notify"}:
            mode = DEFAULT_PRESENCE_MODE
        return cls(
            mode=mode,
            daily_limit=_int_env(PRESENCE_DAILY_LIMIT_ENV, DEFAULT_DAILY_LIMIT, minimum=0, maximum=20),
            min_interval_minutes=_int_env(PRESENCE_MIN_INTERVAL_ENV, DEFAULT_MIN_INTERVAL_MINUTES, minimum=1, maximum=24 * 60),
            quiet_hours=(os.getenv(PRESENCE_QUIET_HOURS_ENV) or DEFAULT_QUIET_HOURS).strip() or DEFAULT_QUIET_HOURS,
            owner_awake_window_minutes=_int_env(PRESENCE_OWNER_AWAKE_WINDOW_ENV, DEFAULT_OWNER_AWAKE_WINDOW_MINUTES, minimum=5, maximum=180),
            stale_task_minutes=_int_env(PRESENCE_STALE_TASK_MINUTES_ENV, DEFAULT_STALE_TASK_MINUTES, minimum=15, maximum=24 * 60),
            tick_minutes=_int_env(PRESENCE_TICK_MINUTES_ENV, DEFAULT_TICK_MINUTES, minimum=5, maximum=24 * 60),
            icebreak_after_minutes=_int_env(PRESENCE_ICEBREAK_AFTER_ENV, DEFAULT_ICEBREAK_AFTER_MINUTES, minimum=60, maximum=24 * 60),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PresenceState:
    mode: str = DEFAULT_PRESENCE_MODE
    last_candidate_at: float = 0.0
    last_sent_at: float = 0.0
    daily_count: int = 0
    daily_date: str = ""
    suppressed_count: int = 0
    candidate_count: int = 0
    shadow_count: int = 0
    sent_count: int = 0
    last_reason: str = ""
    last_suppression_reason: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PresenceState":
        state = cls()
        for key in cls.__dataclass_fields__:
            if key in payload:
                setattr(state, key, payload[key])
        return state

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PresenceCandidate:
    candidate_id: str
    chat_id: str
    kind: str
    message: str
    reason: str
    confidence: float
    risk: str = "low"
    send_after: float = 0.0
    cooldown_key: str = ""
    status: str = "shadow"
    suppression_reason: str = ""
    owner_likely_awake: bool = False
    created_at: str = field(default_factory=lambda: _now_iso())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PresenceOpportunity:
    chat_id: str
    kind: str
    reason: str
    confidence: float
    quiet_now: bool
    owner_likely_awake: bool
    context_summary: str
    created_at: str = field(default_factory=lambda: _now_iso())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PresenceMessageCandidate:
    should_send: bool
    message: str = ""
    message_type: str = "none"
    topic_source: str = ""
    confidence: float = 0.0
    reason: str = ""
    raw_content: str = ""
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PresenceQualityDecision:
    allow: bool
    reason: str
    score: float = 0.0
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PresenceDecision:
    status: str
    reason: str
    candidate: PresenceCandidate | None = None
    debug: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {"status": self.status, "reason": self.reason, "debug": self.debug}
        if self.candidate:
            payload["candidate"] = self.candidate.to_dict()
        return payload


class PresenceComposer:
    """Model-backed composer for proactive companionship messages."""

    def __init__(
        self,
        llm: Any | None = None,
        *,
        debug_file: str = PRESENCE_COMPOSER_DEBUG_FILE,
        topic_history_file: str = PRESENCE_TOPIC_HISTORY_FILE,
        model: str | None = None,
    ):
        self.llm = llm
        self.debug_file = debug_file
        self.topic_history_file = topic_history_file
        self.model = model or os.getenv("YUEYUE_PRESENCE_COMPOSER_MODEL", "").strip() or "deepseek-ai/DeepSeek-V4-Pro"

    def compose(
        self,
        opportunity: PresenceOpportunity,
        short_context: dict[str, Any],
        recent_candidates: list[PresenceCandidate] | None = None,
    ) -> tuple[PresenceMessageCandidate, PresenceQualityDecision]:
        recent_candidates = recent_candidates or []
        try:
            draft = self._call_model(opportunity, short_context, recent_candidates)
        except Exception as exc:
            draft = PresenceMessageCandidate(
                should_send=False,
                reason=f"composer_error: {str(exc)[:180]}",
                model=self.model,
            )
            quality = PresenceQualityDecision(False, "composer_error", checks={"error": str(exc)[:300]})
            self._append_debug({"event": "compose_failed", "opportunity": opportunity.to_dict(), "draft": draft.to_dict(), "quality": quality.to_dict()})
            return draft, quality

        quality = self.quality_gate(draft, opportunity, recent_candidates)
        if not quality.allow:
            self._append_debug({"event": "quality_rejected", "opportunity": opportunity.to_dict(), "draft": draft.to_dict(), "quality": quality.to_dict()})
            return draft, quality
        self._append_debug({"event": "quality_allowed", "opportunity": opportunity.to_dict(), "draft": draft.to_dict(), "quality": quality.to_dict()})
        return draft, quality

    def record_sent(self, candidate: PresenceMessageCandidate, opportunity: PresenceOpportunity, sent_at: float | None = None) -> None:
        payload = {
            "ts": _iso_from_timestamp(time.time() if sent_at is None else sent_at),
            "message": candidate.message,
            "message_type": candidate.message_type,
            "topic_source": candidate.topic_source,
            "reason": candidate.reason,
            "opportunity": opportunity.to_dict(),
        }
        os.makedirs(os.path.dirname(self.topic_history_file), exist_ok=True)
        with open(self.topic_history_file, "a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def recent_topic_history(self, limit: int = 20) -> list[dict[str, Any]]:
        return _read_jsonl_file(self.topic_history_file)[-max(1, limit) :]

    def quality_gate(
        self,
        draft: PresenceMessageCandidate,
        opportunity: PresenceOpportunity,
        recent_candidates: list[PresenceCandidate] | None = None,
    ) -> PresenceQualityDecision:
        message = " ".join((draft.message or "").split())
        recent_candidates = recent_candidates or []
        recent_sent = self.recent_topic_history(limit=12)
        checks = {
            "length": len(message),
            "should_send": draft.should_send,
            "confidence": draft.confidence,
            "topic_source": draft.topic_source,
        }
        if not draft.should_send:
            return PresenceQualityDecision(False, draft.reason or "composer_declined", checks=checks)
        if len(message) < 8:
            return PresenceQualityDecision(False, "message_too_short", checks=checks)
        if len(message) > 180:
            return PresenceQualityDecision(False, "message_too_long", checks=checks)
        lowered = message.casefold()
        if any(marker in lowered for marker in BLAND_MESSAGE_MARKERS):
            if opportunity.reason not in {"recent_low_mood", "recent_url_or_media_topic", "recent_social_mood"}:
                return PresenceQualityDecision(False, "generic_checkin_without_context", checks=checks)
        if any(marker in lowered for marker in FORMAL_MESSAGE_MARKERS):
            return PresenceQualityDecision(False, "too_formal_or_notification_like", checks=checks)
        if any(marker in lowered for marker in TOOL_PROMISE_MARKERS):
            return PresenceQualityDecision(False, "tool_or_task_promise_not_allowed", checks=checks)
        for item in recent_candidates:
            if _similar_text(message, item.message):
                checks["duplicate_candidate"] = item.message
                return PresenceQualityDecision(False, "duplicate_recent_candidate", checks=checks)
        for item in recent_sent:
            old_message = str(item.get("message") or "")
            old_topic = str(item.get("topic_source") or "")
            if _similar_text(message, old_message):
                checks["duplicate_sent"] = old_message
                return PresenceQualityDecision(False, "duplicate_recent_sent_message", checks=checks)
            if draft.topic_source and old_topic and draft.topic_source == old_topic:
                checks["duplicate_topic_source"] = old_topic
                return PresenceQualityDecision(False, "duplicate_recent_topic", checks=checks)
        score = min(1.0, max(0.2, float(draft.confidence or 0.0)))
        return PresenceQualityDecision(True, "quality_pass", score=score, checks=checks)

    def _call_model(
        self,
        opportunity: PresenceOpportunity,
        short_context: dict[str, Any],
        recent_candidates: list[PresenceCandidate],
    ) -> PresenceMessageCandidate:
        llm = self.llm or SiliconFlowAdapter(model=self.model, timeout=25.0, max_retries=1)
        prompt = self._build_prompt(opportunity, short_context, recent_candidates)
        response = llm.chat_with_tools(
            [
                {"role": "system", "content": PRESENCE_COMPOSER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            [],
        )
        raw = str(response.get("content", "") if isinstance(response, dict) else "")
        payload = _extract_json_object(raw)
        if not payload:
            return PresenceMessageCandidate(should_send=False, reason="invalid_model_json", raw_content=raw, model=self.model)
        return PresenceMessageCandidate(
            should_send=bool(payload.get("should_send")),
            message=str(payload.get("message") or "").strip(),
            message_type=str(payload.get("message_type") or "chat").strip() or "chat",
            topic_source=str(payload.get("topic_source") or "").strip(),
            confidence=_safe_float(payload.get("confidence"), 0.0),
            reason=str(payload.get("reason") or "").strip(),
            raw_content=raw,
            model=self.model,
        )

    def _build_prompt(self, opportunity: PresenceOpportunity, short_context: dict[str, Any], recent_candidates: list[PresenceCandidate]) -> str:
        context_text = _flatten_context_text(short_context)
        turns = short_context.get("turns") if isinstance(short_context, dict) else []
        compact_turns: list[dict[str, Any]] = []
        if isinstance(turns, list):
            for item in turns[-8:]:
                if isinstance(item, dict):
                    compact_turns.append(
                        {
                            "text": str(item.get("text") or "")[:160],
                            "topic": str(item.get("topic") or "")[:80],
                            "emotion": str(item.get("emotion") or item.get("mood") or "")[:60],
                            "assistant_summary": str(item.get("assistant_summary") or "")[:140],
                        }
                    )
        recent_sent = self.recent_topic_history(limit=8)
        recent_candidate_messages = [item.message for item in recent_candidates[-5:] if item.message]
        payload = {
            "opportunity": opportunity.to_dict(),
            "latest_context_text": context_text[:1200],
            "recent_turns": compact_turns,
            "recent_sent_presence": recent_sent,
            "recent_candidate_messages": recent_candidate_messages,
            "requirements": [
                "Only send if you have something specific and worth replying to.",
                "Avoid generic check-ins unless recent context clearly justifies care.",
                "For long_silence_icebreak, open a light new topic instead of asking why the owner did not reply.",
                "One short Traditional Chinese message, warm girlfriend-like companion tone.",
                "No tool promises, no system/report tone, no pressure to reply.",
                "Return JSON only.",
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _append_debug(self, payload: dict[str, Any]) -> None:
        payload = {"ts": _now_iso(), **payload}
        os.makedirs(os.path.dirname(self.debug_file), exist_ok=True)
        with open(self.debug_file, "a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


class PresenceEngine:
    """Adaptive, low-frequency proactive companionship control plane."""

    def __init__(
        self,
        state_file: str = PRESENCE_STATE_FILE,
        candidates_file: str = PRESENCE_CANDIDATES_FILE,
        health_file: str = PRESENCE_HEALTH_FILE,
        debug_file: str = PRESENCE_DEBUG_FILE,
        config: PresenceConfig | None = None,
        composer: PresenceComposer | None = None,
    ):
        self.state_file = state_file
        self.candidates_file = candidates_file
        self.health_file = health_file
        self.debug_file = debug_file
        self.config = config or PresenceConfig.from_env()
        self.composer = composer or PresenceComposer()

    def load_state(self) -> PresenceState:
        try:
            with open(self.state_file, "r", encoding="utf-8") as file:
                payload = json.load(file)
            if isinstance(payload, dict):
                return PresenceState.from_dict(payload)
        except Exception:
            pass
        return PresenceState(mode=self.config.mode, daily_date=_today_key())

    def save_state(self, state: PresenceState) -> None:
        state.mode = self.config.mode
        state.updated_at = _now_iso()
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as file:
            json.dump(state.to_dict(), file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")

    def evaluate(
        self,
        chat_id: int | str,
        short_context: dict[str, Any] | None = None,
        session_summary: str = "",
        session_updated_at: float | None = None,
        now: float | None = None,
        force: bool = False,
        source: str = "post_turn",
    ) -> PresenceDecision:
        now = time.time() if now is None else now
        short_context = short_context or {}
        state = self.load_state()
        self._roll_daily_counter(state, now)

        owner_awake = self.owner_likely_awake(short_context, now)
        task_active, task_debug = self._task_is_active(session_summary, session_updated_at, now)
        quiet_now = _in_quiet_hours(now, self.config.quiet_hours)
        debug = {
            "ts": _iso_from_timestamp(now),
            "source": source,
            "mode": self.config.mode,
            "chat_id": str(chat_id),
            "quiet_hours": self.config.quiet_hours,
            "quiet_now": quiet_now,
            "owner_likely_awake": owner_awake,
            "task_active": task_active,
            "task_debug": task_debug,
            "daily_count": state.daily_count,
            "daily_limit": self.config.daily_limit,
            "last_candidate_age_minutes": _age_minutes(state.last_candidate_at, now),
            "last_sent_age_minutes": _age_minutes(state.last_sent_at, now),
            "context_summary": self.context_summary(short_context),
        }

        if self.config.mode == "off":
            return self._suppress(state, "presence_disabled", now, debug)
        if not force and quiet_now and not owner_awake:
            return self._suppress(state, "quiet_hours_no_recent_interaction", now, debug)
        if not force and task_active:
            return self._suppress(state, "task_or_permission_active", now, debug)
        if not force and state.last_candidate_at and now - float(state.last_candidate_at) < self.config.min_interval_minutes * 60:
            return self._suppress(state, "cooldown", now, debug)
        if self.config.mode == "notify" and not force and state.daily_count >= self.config.daily_limit:
            return self._suppress(state, "daily_limit", now, debug)

        kind, reason, confidence = self._select_candidate(short_context, now)
        opportunity = PresenceOpportunity(
            chat_id=str(chat_id),
            kind=kind,
            reason=reason,
            confidence=confidence,
            quiet_now=quiet_now,
            owner_likely_awake=owner_awake,
            context_summary=debug["context_summary"],
        )
        candidate = PresenceCandidate(
            candidate_id=f"presence_{int(now * 1000)}",
            chat_id=str(chat_id),
            kind=kind,
            message="",
            reason=reason,
            confidence=confidence,
            send_after=now,
            cooldown_key=f"{chat_id}:{kind}",
            status="shadow" if self.config.mode == "shadow" else "ready",
            owner_likely_awake=owner_awake,
        )
        self._append_candidate(candidate)
        state.last_candidate_at = now
        state.candidate_count += 1
        state.last_reason = reason
        if candidate.status == "shadow":
            state.shadow_count += 1
        self.save_state(state)
        self.write_health(state)
        debug.update({"decision": candidate.status, "reason": reason, "candidate": candidate.to_dict(), "opportunity": opportunity.to_dict()})
        self._append_debug(debug)
        emit_trace("presence.candidate", session_id="presence", turn_id=0, **candidate.to_dict())
        return PresenceDecision(candidate.status, reason, candidate, debug)

    def tick(
        self,
        chat_id: int | str,
        short_context: dict[str, Any] | None,
        session_summary: str = "",
        session_updated_at: float | None = None,
        send_callback: Callable[[str], Any] | None = None,
        now: float | None = None,
        random_value: float | None = None,
        force: bool = False,
    ) -> PresenceDecision:
        now = time.time() if now is None else now
        decision = self.evaluate(
            chat_id,
            short_context=short_context,
            session_summary=session_summary,
            session_updated_at=session_updated_at,
            now=now,
            force=force,
            source="scheduler_tick",
        )
        if decision.status != "ready" or not decision.candidate:
            return decision
        probability = 1.0 if force else self.trigger_probability(short_context or {}, decision.debug)
        roll = random.random() if random_value is None else random_value
        decision.debug.update({"trigger_probability": round(probability, 4), "random_value": round(roll, 4)})
        if roll > probability:
            decision.candidate.status = "deferred"
            decision.reason = "random_gate_deferred"
            decision.debug.update({"decision": "deferred", "reason": decision.reason})
            self._append_debug(decision.debug)
            return decision
        if self.config.mode != "notify" or send_callback is None:
            return decision

        opportunity = self._opportunity_from_decision(chat_id, decision, short_context or {})
        try:
            draft, quality = self.composer.compose(opportunity, short_context or {}, self.recent_candidates(limit=8))
        except Exception as exc:
            draft = PresenceMessageCandidate(should_send=False, reason=f"composer_error: {str(exc)[:180]}")
            quality = PresenceQualityDecision(False, "composer_error", checks={"error": str(exc)[:300]})
        decision.debug.update({"composer": draft.to_dict(), "quality": quality.to_dict()})
        if not quality.allow or not draft.message:
            decision.candidate.status = "composer_rejected"
            decision.reason = quality.reason
            decision.debug.update({"decision": "composer_rejected", "reason": quality.reason})
            self._append_debug(decision.debug)
            emit_trace("presence.composer_rejected", session_id="presence", turn_id=0, reason=quality.reason, chat_id=str(chat_id))
            return decision

        decision.candidate.message = draft.message
        decision.candidate.kind = draft.message_type or decision.candidate.kind
        decision.candidate.reason = draft.reason or decision.candidate.reason
        decision.candidate.confidence = quality.score or draft.confidence or decision.candidate.confidence
        send_callback(decision.candidate.message)
        state = self.load_state()
        self._roll_daily_counter(state, now)
        state.daily_count += 1
        state.sent_count += 1
        state.last_sent_at = now
        state.last_reason = decision.candidate.reason
        decision.candidate.status = "sent"
        self.save_state(state)
        self.write_health(state)
        self.composer.record_sent(draft, opportunity, sent_at=now)
        decision.debug.update({"decision": "sent", "sent": True, "message": decision.candidate.message})
        self._append_debug(decision.debug)
        emit_trace("presence.sent", session_id="presence", turn_id=0, chat_id=str(chat_id), kind=decision.candidate.kind, reason=decision.candidate.reason)
        return decision

    def owner_likely_awake(self, short_context: dict[str, Any], now: float | None = None) -> bool:
        now = time.time() if now is None else now
        latest = self.latest_interaction_at(short_context)
        if not latest:
            return False
        return now - latest <= self.config.owner_awake_window_minutes * 60

    def latest_interaction_at(self, short_context: dict[str, Any]) -> float:
        candidates: list[float] = []
        for key in ("created_at", "last_interaction_at"):
            value = short_context.get(key)
            if isinstance(value, (int, float)):
                candidates.append(float(value))
        turns = short_context.get("turns")
        if isinstance(turns, list):
            for item in turns:
                if isinstance(item, dict) and isinstance(item.get("created_at"), (int, float)):
                    candidates.append(float(item["created_at"]))
        return max(candidates) if candidates else 0.0

    def context_summary(self, short_context: dict[str, Any]) -> str:
        text = _flatten_context_text(short_context)
        text = " ".join(text.split())
        return text[:240]

    def trigger_probability(self, short_context: dict[str, Any], debug: dict[str, Any] | None = None) -> float:
        text = _flatten_context_text(short_context).casefold()
        probability = 0.18
        if debug and debug.get("owner_likely_awake"):
            probability += 0.2
        if any(marker in text for marker in LOW_MOOD_MARKERS):
            probability += 0.22
        if any(marker in text for marker in URL_MARKERS):
            probability += 0.12
        if any(marker in text for marker in SOCIAL_MARKERS):
            probability += 0.08
        if debug and debug.get("reason") == "long_silence_icebreak":
            probability += 0.1
        return max(0.05, min(0.85, probability))

    def recent_candidates(self, limit: int = 5) -> list[PresenceCandidate]:
        rows = self._read_jsonl(self.candidates_file)
        candidates: list[PresenceCandidate] = []
        for payload in rows[-max(1, limit) :]:
            try:
                candidates.append(PresenceCandidate(**{key: value for key, value in payload.items() if key in PresenceCandidate.__dataclass_fields__}))
            except Exception:
                continue
        return candidates

    def recent_debug(self, limit: int = 5) -> list[dict[str, Any]]:
        return self._read_jsonl(self.debug_file)[-max(1, limit) :]

    def health(self) -> dict[str, Any]:
        state = self.load_state()
        return {
            "status": "pass",
            "mode": self.config.mode,
            "daily_limit": self.config.daily_limit,
            "min_interval_minutes": self.config.min_interval_minutes,
            "quiet_hours": self.config.quiet_hours,
            "owner_awake_window_minutes": self.config.owner_awake_window_minutes,
            "stale_task_minutes": self.config.stale_task_minutes,
            "tick_minutes": self.config.tick_minutes,
            "icebreak_after_minutes": self.config.icebreak_after_minutes,
            "candidate_count": int(state.candidate_count or 0),
            "shadow_count": int(state.shadow_count or 0),
            "suppressed_count": int(state.suppressed_count or 0),
            "daily_count": int(state.daily_count or 0),
            "sent_count": int(state.sent_count or 0),
            "last_reason": state.last_reason,
            "last_suppression_reason": state.last_suppression_reason,
            "updated_at": state.updated_at,
            "candidates_file": self.candidates_file,
            "debug_file": self.debug_file,
            "composer_enabled": True,
            "composer_model": self.composer.model,
            "composer_debug_file": self.composer.debug_file,
            "topic_history_file": self.composer.topic_history_file,
        }

    def write_health(self, state: PresenceState | None = None) -> dict[str, Any]:
        health = self.health() if state is None else {
            "status": "pass",
            "mode": self.config.mode,
            "daily_limit": self.config.daily_limit,
            "min_interval_minutes": self.config.min_interval_minutes,
            "quiet_hours": self.config.quiet_hours,
            "owner_awake_window_minutes": self.config.owner_awake_window_minutes,
            "stale_task_minutes": self.config.stale_task_minutes,
            "tick_minutes": self.config.tick_minutes,
            "icebreak_after_minutes": self.config.icebreak_after_minutes,
            "candidate_count": int(state.candidate_count or 0),
            "shadow_count": int(state.shadow_count or 0),
            "suppressed_count": int(state.suppressed_count or 0),
            "daily_count": int(state.daily_count or 0),
            "sent_count": int(state.sent_count or 0),
            "last_reason": state.last_reason,
            "last_suppression_reason": state.last_suppression_reason,
            "updated_at": state.updated_at,
            "candidates_file": self.candidates_file,
            "debug_file": self.debug_file,
            "composer_enabled": True,
            "composer_model": self.composer.model,
            "composer_debug_file": self.composer.debug_file,
            "topic_history_file": self.composer.topic_history_file,
        }
        os.makedirs(os.path.dirname(self.health_file), exist_ok=True)
        with open(self.health_file, "w", encoding="utf-8") as file:
            json.dump(health, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
        return health

    def _select_candidate(self, short_context: dict[str, Any], now: float | None = None) -> tuple[str, str, float]:
        text = _flatten_context_text(short_context).casefold()
        if any(marker in text for marker in LOW_MOOD_MARKERS):
            return "care", "recent_low_mood", 0.76
        if any(marker in text for marker in URL_MARKERS):
            return "followup", "recent_url_or_media_topic", 0.68
        if any(marker in text for marker in SOCIAL_MARKERS):
            return "social", "recent_social_mood", 0.64
        now = time.time() if now is None else now
        latest = self.latest_interaction_at(short_context)
        if latest and now - latest >= self.config.icebreak_after_minutes * 60:
            return "icebreak", "long_silence_icebreak", 0.58
        return "soft_ping", "idle_checkin", 0.55

    def _opportunity_from_decision(self, chat_id: int | str, decision: PresenceDecision, short_context: dict[str, Any]) -> PresenceOpportunity:
        payload = decision.debug.get("opportunity") if isinstance(decision.debug, dict) else None
        if isinstance(payload, dict):
            try:
                return PresenceOpportunity(**{key: value for key, value in payload.items() if key in PresenceOpportunity.__dataclass_fields__})
            except Exception:
                pass
        candidate = decision.candidate
        return PresenceOpportunity(
            chat_id=str(chat_id),
            kind=candidate.kind if candidate else "soft_ping",
            reason=decision.reason,
            confidence=candidate.confidence if candidate else 0.5,
            quiet_now=bool(decision.debug.get("quiet_now")) if isinstance(decision.debug, dict) else False,
            owner_likely_awake=bool(decision.debug.get("owner_likely_awake")) if isinstance(decision.debug, dict) else False,
            context_summary=self.context_summary(short_context),
        )

    def _message_for(self, kind: str, reason: str, quiet_now: bool = False, owner_likely_awake: bool = False) -> str:
        prefix = "我看你剛剛好像還醒著，所以小聲冒一下泡：" if quiet_now and owner_likely_awake else ""
        if kind == "care":
            return prefix + "主人，月月剛剛有點想來看看你還好不好。要是累了就先歇一下，我在旁邊待命～"
        if kind == "followup":
            return prefix + "主人，剛剛那個連結我還惦記著。你想接著聊的話，月月可以陪你看下去～"
        if kind == "social":
            return prefix + "月月剛剛有點想丟個小反應，但先乖乖輕聲問一句：還要繼續鬧嗎～"
        return prefix + "主人，月月剛剛有點想找你說句話。沒事的話我就繼續乖乖待命～"

    def _task_is_active(self, session_summary: str, session_updated_at: float | None, now: float) -> tuple[bool, dict[str, Any]]:
        text = (session_summary or "").casefold()
        markers = ["active_task", "awaiting_permission", "awaiting_validation", "blocked", "等待授權", "等待验证", "等待驗證"]
        active = any(marker in text for marker in markers)
        age_minutes = _age_minutes(session_updated_at or 0.0, now)
        stale = bool(active and session_updated_at and now - session_updated_at > self.config.stale_task_minutes * 60)
        return bool(active and not stale), {"active": active, "stale": stale, "age_minutes": age_minutes}

    def _roll_daily_counter(self, state: PresenceState, now: float) -> None:
        today = _today_key(now)
        if state.daily_date != today:
            state.daily_date = today
            state.daily_count = 0

    def _suppress(self, state: PresenceState, reason: str, now: float, debug: dict[str, Any]) -> PresenceDecision:
        state.suppressed_count += 1
        state.last_reason = reason
        state.last_suppression_reason = reason
        self.save_state(state)
        self.write_health(state)
        debug.update({"decision": "suppressed", "reason": reason})
        self._append_debug(debug)
        emit_trace("presence.suppressed", session_id="presence", turn_id=0, reason=reason, mode=self.config.mode)
        return PresenceDecision("suppressed", reason, debug=debug)

    def _append_candidate(self, candidate: PresenceCandidate) -> None:
        os.makedirs(os.path.dirname(self.candidates_file), exist_ok=True)
        with open(self.candidates_file, "a", encoding="utf-8") as file:
            file.write(json.dumps(candidate.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")

    def _append_debug(self, payload: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.debug_file), exist_ok=True)
        with open(self.debug_file, "a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def _read_jsonl(self, path: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as file:
                for line in file:
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(payload, dict):
                        rows.append(payload)
        except Exception:
            return []
        return rows


LOW_MOOD_MARKERS = ["難受", "难受", "累", "壓力", "压力", "失敗", "失败", "sad", "tired", "stressed", "stress"]
URL_MARKERS = ["http://", "https://", "youtube", "bilibili", "douyin", "抖音", "影片", "視頻", "视频", "連結", "链接"]
SOCIAL_MARKERS = ["貼圖", "贴图", "表情包", "鬥圖", "斗图", "可愛", "可爱", "哈哈", "笑死"]


# Canonical Unicode-safe markers used by Phase B3. These intentionally
# override older mojibake literals above without changing historical trace data.
LOW_MOOD_MARKERS = ["難受", "累", "壓力", "失敗", "崩潰", "煩", "焦慮", "sad", "tired", "stressed", "stress"]
URL_MARKERS = ["http://", "https://", "youtube", "bilibili", "douyin", "抖音", "影片", "視頻", "連結", "鏈接", "url"]
SOCIAL_MARKERS = ["貼圖", "表情包", "鬥圖", "斗图", "可愛", "可爱", "哈哈", "笑死", "萌"]
BLAND_MESSAGE_MARKERS = ["你還好嗎", "你还好吗", "今天怎麼樣", "今天怎么样", "在嗎", "在吗", "記得休息", "记得休息"]
FORMAL_MESSAGE_MARKERS = ["通知", "提醒您", "根據目前", "系统", "系統", "待命狀態", "presence", "runtime", "任務狀態"]
TOOL_PROMISE_MARKERS = ["我幫你跑", "我帮你跑", "我去執行", "我去执行", "工具", "截圖", "截图", "命令"]

PRESENCE_COMPOSER_SYSTEM_PROMPT = """你是月月的主動陪伴訊息 composer。
你的任務不是發通知，而是判斷此刻有沒有一句值得主人回的短消息。
人格方向：親近、熟、偏女友/伴侶式陪伴，可以可愛、好奇、輕微調侃，但不要客服腔、報告腔、任務腔。
如果沒有具體語境或好話題，請輸出 should_send=false。
如果 reason 是 long_silence_icebreak，代表主人很久沒回；可以開一個輕、小、有趣的新話題，但不要質問、不要查崗、不要讓人有壓力。
只輸出 JSON，格式：
{"should_send": true/false, "message": "繁體中文短句", "message_type": "followup|playful|care|share|icebreak|none", "topic_source": "短話題來源", "confidence": 0.0-1.0, "reason": "簡短原因"}
"""


def load_presence_health(path: str = PRESENCE_HEALTH_FILE) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
        if isinstance(payload, dict):
            if "composer_enabled" not in payload:
                return PresenceEngine(health_file=path).write_health()
            return payload
    except Exception:
        pass
    return PresenceEngine(health_file=path).write_health()


def _flatten_context_text(context: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("primary_text", "summary", "topic", "mood", "url", "assistant_summary"):
        value = context.get(key)
        if isinstance(value, str):
            parts.append(value)
    turns = context.get("turns")
    if isinstance(turns, list):
        for item in turns[-5:]:
            if isinstance(item, dict):
                for key in ("text", "assistant_summary", "topic", "emotion"):
                    value = item.get(key)
                    if isinstance(value, str):
                        parts.append(value)
    return "\n".join(parts)


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(float(raw))
    except Exception:
        return default
    return max(minimum, min(maximum, value))


def _today_key(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return _iso_from_timestamp(time.time())


def _iso_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%dT%H:%M:%S%z")


def _age_minutes(value: float | None, now: float) -> float | None:
    if not value:
        return None
    return round(max(0.0, now - float(value)) / 60.0, 2)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _extract_json_object(text: str) -> dict[str, Any]:
    if not text:
        return {}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(0))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _similar_text(a: str, b: str) -> bool:
    left = re.sub(r"\s+", "", (a or "").casefold())
    right = re.sub(r"\s+", "", (b or "").casefold())
    if not left or not right:
        return False
    if left == right or left in right or right in left:
        return True
    left_terms = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", left))
    right_terms = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", right))
    if not left_terms or not right_terms:
        return False
    overlap = len(left_terms & right_terms) / max(1, min(len(left_terms), len(right_terms)))
    return overlap >= 0.75


def _read_jsonl_file(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as file:
            for line in file:
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if isinstance(payload, dict):
                    rows.append(payload)
    except Exception:
        return []
    return rows


def _in_quiet_hours(now: float, spec: str) -> bool:
    try:
        start_raw, end_raw = [part.strip() for part in spec.split("-", 1)]
        start = _parse_day_time(start_raw)
        end = _parse_day_time(end_raw)
    except Exception:
        return False
    current = datetime.fromtimestamp(now).time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def _parse_day_time(value: str) -> day_time:
    hour, minute = value.split(":", 1)
    return day_time(hour=int(hour), minute=int(minute))


DEFAULT_PRESENCE_ENGINE = PresenceEngine()
