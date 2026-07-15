from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

from .storage import AtomicJsonStore


def _decode_ledger(raw: dict) -> dict[str, float]:
    entries = raw.get("entries") if isinstance(raw.get("entries"), dict) else {}
    return {str(key): float(value) for key, value in entries.items()}


class RenderLedger:
    """Persistent at-most-once claims for Telegram render artifacts."""

    def __init__(self, path: str | Path, max_entries: int = 4000):
        self.store = AtomicJsonStore(path, dict, _decode_ledger)
        self.max_entries = max(100, int(max_entries))
        self._entries = self.store.load()
        self._lock = threading.RLock()

    @staticmethod
    def key(event_id: str, kind: str, identity: str) -> str:
        payload = f"{event_id}\0{kind}\0{identity}".encode("utf-8", errors="replace")
        return hashlib.sha256(payload).hexdigest()

    def claim(self, key: str) -> bool:
        with self._lock:
            if key in self._entries:
                return False
            self._entries[key] = time.time()
            if len(self._entries) > self.max_entries:
                self._entries = dict(sorted(self._entries.items(), key=lambda item: item[1])[-self.max_entries :])
            self._save()
            return True

    def release(self, key: str) -> None:
        with self._lock:
            if self._entries.pop(key, None) is not None:
                self._save()

    def _save(self) -> None:
        self.store.save({"schema_version": 3, "entries": self._entries})
