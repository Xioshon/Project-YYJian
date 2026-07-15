from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class AtomicJsonStore(Generic[T]):
    """Crash-safe JSON storage with a last-known-good backup."""

    def __init__(self, path: str | Path, factory: Callable[[], T], decoder: Callable[[dict[str, Any]], T]):
        self.path = Path(path)
        self.backup_path = self.path.with_suffix(self.path.suffix + ".bak")
        self.factory = factory
        self.decoder = decoder
        self._lock = threading.RLock()

    def load(self) -> T:
        with self._lock:
            for candidate in (self.path, self.backup_path):
                try:
                    if candidate.exists() and candidate.stat().st_size:
                        with candidate.open("r", encoding="utf-8") as handle:
                            raw = json.load(handle)
                        return self.decoder(raw if isinstance(raw, dict) else {})
                except (OSError, ValueError, TypeError):
                    continue
            return self.factory()

    def save(self, value: T) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = asdict(value) if is_dataclass(value) else value
            temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if self.path.exists():
                backup_tmp = self.backup_path.with_name(f".{self.backup_path.name}.{uuid.uuid4().hex}.tmp")
                with self.path.open("rb") as source, backup_tmp.open("wb") as target:
                    target.write(source.read())
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(backup_tmp, self.backup_path)
            os.replace(temporary, self.path)


class JsonlEventStore:
    # Runtime events embed full state snapshots, so this file grows quickly on a
    # long-running bot. One rotated generation keeps recent forensics without
    # letting the live file grow without bound.
    _ROTATE_BYTES = 50 * 1024 * 1024

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def append(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if self.path.exists() and self.path.stat().st_size > self._ROTATE_BYTES:
                    os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
            except OSError:
                pass
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def read(self, limit: int = 1000) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self._lock, self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(item, dict):
                    rows.append(item)
        return rows[-max(1, limit) :]
