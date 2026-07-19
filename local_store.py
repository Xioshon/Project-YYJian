"""Tiny JSON-backed named list stores (notes / todos / expenses) — pure local, provider-free.

One generic ListStore keeps each feature's records under workspace/memory/<name>.json with a
threading lock and atomic-enough write-through. Records are plain dicts with created_at added;
each skill handler decides its own record shape. Deliberately boring: durability and zero
dependencies beat cleverness for a publishable companion agent.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

ROOT_DIR = os.path.abspath(os.getenv("YUEYUE_ROOT_DIR") or os.path.dirname(__file__))
STORE_DIR = os.path.join(ROOT_DIR, "workspace", "memory")


class ListStore:
    def __init__(self, name: str, directory: str = STORE_DIR, cap: int = 2000):
        self.path = os.path.join(directory, f"{name}.json")
        self.cap = cap
        self._lock = threading.Lock()
        self.items: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, list):
                self.items = [item for item in raw if isinstance(item, dict)][-self.cap :]
        except Exception:
            self.items = []

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(self.items[-self.cap :], handle, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add(self, record: dict) -> dict:
        record = dict(record)
        record.setdefault("id", uuid.uuid4().hex[:10])
        record.setdefault("created_at", time.time())
        with self._lock:
            self.items.append(record)
            self._save()
        return record

    def all(self) -> list[dict]:
        with self._lock:
            return list(self.items)

    def update(self, item_id: str, **changes) -> bool:
        with self._lock:
            for item in self.items:
                if item.get("id") == item_id:
                    item.update(changes)
                    self._save()
                    return True
        return False

    def remove(self, item_id: str) -> bool:
        with self._lock:
            before = len(self.items)
            self.items = [item for item in self.items if item.get("id") != item_id]
            if len(self.items) != before:
                self._save()
                return True
        return False


NOTES = ListStore("notes")
TODOS = ListStore("todos")
EXPENSES = ListStore("expenses")
