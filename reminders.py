"""Scheduled reminders — provider-agnostic productivity core (ROADMAP Phase 6).

A real "remind me at X" that fires at the requested wall-clock time, independent of the presence
engine's throttling/quiet-hours (those govern *proactive chit-chat*; a reminder the owner asked
for must fire on time). Pure Python + JSONL persistence: no LLM, no SiliconFlow, survives restart.
The LLM is only used upstream to parse a natural request into (fire_at, text); firing is deterministic.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

ROOT_DIR = os.path.abspath(os.getenv("YUEYUE_ROOT_DIR") or os.path.dirname(__file__))
REMINDER_FILE = os.path.join(ROOT_DIR, "workspace", "project_cache", "reminders.json")


@dataclass
class Reminder:
    chat_id: str
    fire_at: float          # unix timestamp
    text: str               # what to remind about (the owner's own words)
    created_at: float = field(default_factory=time.time)
    fired: bool = False
    repeat_every: float = 0.0  # seconds; >0 = recurring (daily meds, weekly review) - re-arms on fire
    reminder_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


class ReminderStore:
    def __init__(self, path: str = REMINDER_FILE):
        self.path = path
        self._lock = threading.Lock()
        self.reminders: list[Reminder] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
            known = set(Reminder.__dataclass_fields__)
            self.reminders = [
                Reminder(**{k: v for k, v in item.items() if k in known})
                for item in raw
                if isinstance(item, dict) and item.get("text")
            ]
        except Exception:
            self.reminders = []
        # Drop fired reminders older than a day so the file cannot grow forever.
        cutoff = time.time() - 86400
        self.reminders = [r for r in self.reminders if not (r.fired and r.created_at < cutoff)]

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump([asdict(r) for r in self.reminders], handle, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add(self, chat_id: str, fire_at: float, text: str, repeat_every: float = 0.0) -> Reminder:
        reminder = Reminder(
            chat_id=str(chat_id), fire_at=float(fire_at), text=str(text)[:300],
            repeat_every=max(0.0, float(repeat_every or 0.0)),
        )
        with self._lock:
            self.reminders.append(reminder)
            self._save()
        return reminder

    def due(self, now: float | None = None) -> list[Reminder]:
        now = time.time() if now is None else now
        with self._lock:
            return [r for r in self.reminders if not r.fired and r.fire_at <= now]

    def mark_fired(self, reminder_id: str) -> None:
        """One-shot reminders retire; recurring ones re-arm for the next occurrence (skipping any
        occurrences already in the past, so a laptop asleep for two days does not backlog-spam)."""
        now = time.time()
        with self._lock:
            for reminder in self.reminders:
                if reminder.reminder_id != reminder_id:
                    continue
                if reminder.repeat_every > 0:
                    while reminder.fire_at <= now:
                        reminder.fire_at += reminder.repeat_every
                else:
                    reminder.fired = True
            self._save()

    def pending(self, chat_id: str) -> list[Reminder]:
        with self._lock:
            return sorted(
                (r for r in self.reminders if not r.fired and str(r.chat_id) == str(chat_id)),
                key=lambda r: r.fire_at,
            )

    def cancel(self, chat_id: str, reminder_id: str = "") -> int:
        """Cancel a specific pending reminder, or (no id) all pending for the chat. Returns count."""
        with self._lock:
            before = sum(1 for r in self.reminders if not r.fired and str(r.chat_id) == str(chat_id))
            for reminder in self.reminders:
                if reminder.fired or str(reminder.chat_id) != str(chat_id):
                    continue
                if not reminder_id or reminder.reminder_id == reminder_id:
                    reminder.fired = True
            after = sum(1 for r in self.reminders if not r.fired and str(r.chat_id) == str(chat_id))
            self._save()
            return before - after


class ReminderScheduler:
    """Background thread that fires due reminders via a callback. The callback composes and sends
    the owner-facing line (so firing stays persona-consistent and transport-agnostic)."""

    def __init__(self, store: ReminderStore, fire_callback: Callable[[Reminder], None], interval: float = 20.0):
        self.store = store
        self.fire_callback = fire_callback
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="YueYueReminderScheduler", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                for reminder in self.store.due():
                    try:
                        self.fire_callback(reminder)
                    finally:
                        self.store.mark_fired(reminder.reminder_id)
            except Exception as exc:  # noqa: BLE001
                print(f"[reminder scheduler warning] {exc}")

    def stop(self) -> None:
        self._stop.set()


DEFAULT_REMINDER_STORE = ReminderStore()
