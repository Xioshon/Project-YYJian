from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict

from .models import SCHEMA_VERSION, RuntimeEvent, RuntimeState
from .storage import JsonlEventStore

Reducer = Callable[[RuntimeState, RuntimeEvent], RuntimeState]


class SingleWriterEventLoop:
    """Serialize state mutation while allowing background event submission."""

    def __init__(
        self,
        state: RuntimeState,
        reducer: Reducer,
        event_store: JsonlEventStore,
        persist: Callable[[RuntimeState], None] | None = None,
    ):
        self.state = state
        self.reducer = reducer
        self.event_store = event_store
        self._queue: queue.Queue[RuntimeEvent] = queue.Queue()
        self._writer_thread_id = threading.get_ident()
        self._lock = threading.RLock()
        self._persist = persist

    def submit(self, event: RuntimeEvent) -> None:
        self._queue.put(event)

    @contextmanager
    def writer_scope(self) -> Iterator[None]:
        """Lease the single writer role to one complete runtime turn."""
        with self._lock:
            previous_writer = self._writer_thread_id
            self._writer_thread_id = threading.get_ident()
            try:
                yield
            finally:
                self._writer_thread_id = previous_writer

    def apply(self, event: RuntimeEvent) -> RuntimeState:
        if threading.get_ident() != self._writer_thread_id:
            self.submit(event)
            return self.state
        with self._lock:
            if event.event_id in set(self.state.processed_event_ids):
                return self.state
            event_row = _compact_event_row(event)
            event_row["schema_version"] = SCHEMA_VERSION
            self.event_store.append(event_row)
            self.state = self.reducer(self.state, event)
            self.state.processed_event_ids = (self.state.processed_event_ids + [event.event_id])[-2000:]
            if self._persist:
                self._persist(self.state)
            return self.state

    def drain(self, maximum: int = 100) -> RuntimeState:
        if threading.get_ident() != self._writer_thread_id:
            raise RuntimeError("Only the runtime writer thread may drain events.")
        for _ in range(maximum):
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            self.apply(event)
        return self.state


def _compact_event_row(event: RuntimeEvent) -> dict:
    event_row = asdict(event)
    payload = dict(event_row.get("payload") or {})
    state = payload.pop("state", None)
    event_row["payload"] = payload
    if isinstance(state, dict):
        workflow = state.get("workflow") if isinstance(state.get("workflow"), dict) else {}
        steps = workflow.get("steps") if isinstance(workflow.get("steps"), list) else []
        index = int(workflow.get("current_step_index") or 0)
        step = steps[index] if 0 <= index < len(steps) and isinstance(steps[index], dict) else {}
        permission = state.get("permission") if isinstance(state.get("permission"), dict) else {}
        pending = permission.get("pending_action") if isinstance(permission.get("pending_action"), dict) else {}
        event_row["state_transition"] = {
            "workflow_id": str(workflow.get("workflow_id") or ""),
            "workflow_status": str(workflow.get("status") or ""),
            "current_step_id": str(step.get("step_id") or ""),
            "current_step_status": str(step.get("status") or ""),
            "permission_scope": str(permission.get("scope") or "none"),
            "pending_tool": str(pending.get("tool_name") or ""),
            "state_updated_at": state.get("updated_at"),
        }
    return event_row
