from __future__ import annotations

import tempfile

from reminders import ReminderStore
from skill_engine import _prefilter, try_skill


class RouteProvider:
    """Scripted skill-router: returns a fixed JSON decision, records that it was called."""

    def __init__(self, decision_json: str):
        self.decision_json = decision_json
        self.calls = 0

    def chat(self, messages, tools, **kwargs):
        self.calls += 1

        class R:
            content = self.decision_json

        return R()


def _store(tmp) -> ReminderStore:
    return ReminderStore(path=str(tmp / "reminders.json"))


def test_prefilter_gates_normal_chat_from_any_model_call():
    # Ordinary chatter must not match any skill keyword -> no candidates -> no model call at all.
    assert _prefilter("月月你今天好可愛") == []
    assert _prefilter("今天天氣真好啊") == []
    assert _prefilter("十分鐘後提醒我喝水")  # a real reminder request matches


def test_normal_chat_never_calls_router(monkeypatch):
    provider = RouteProvider('{"skill":"none"}')
    out = try_skill("陪我聊聊天嘛", "telegram", provider)
    assert out is None
    assert provider.calls == 0  # prefilter short-circuited before any API cost


def test_set_reminder_creates_a_scheduled_reminder(monkeypatch, tmp_path):
    import reminders as rmod
    import skill_engine as smod

    store = _store(tmp_path)
    monkeypatch.setattr(rmod, "DEFAULT_REMINDER_STORE", store)
    monkeypatch.setattr(smod, "DEFAULT_REMINDER_STORE", store)

    provider = RouteProvider('{"skill":"set_reminder","args":{"what":"喝水","fire_in_seconds":600}}')
    out = try_skill("十分鐘後提醒我喝水", "telegram", provider, now=1000.0)
    assert out is not None and out.ok
    pending = store.pending("telegram")
    assert len(pending) == 1
    assert pending[0].text == "喝水"
    assert abs(pending[0].fire_at - 1600.0) < 1


def test_reminder_fires_when_due_and_only_once():
    store = ReminderStore(path=str(tempfile.mkdtemp() + "/r.json"))
    store.add("telegram", fire_at=1000.0, text="開會")
    assert store.due(now=999.0) == []          # not yet
    due = store.due(now=1001.0)
    assert len(due) == 1
    store.mark_fired(due[0].reminder_id)
    assert store.due(now=1001.0) == []          # fired once, never again


def test_reminder_survives_reload():
    path = tempfile.mkdtemp() + "/r.json"
    store = ReminderStore(path=path)
    store.add("telegram", fire_at=9e12, text="生日")
    reloaded = ReminderStore(path=path)
    assert [r.text for r in reloaded.pending("telegram")] == ["生日"]


def test_cancel_clears_pending():
    store = ReminderStore(path=str(tempfile.mkdtemp() + "/r.json"))
    store.add("telegram", fire_at=9e12, text="A")
    store.add("telegram", fire_at=9e12, text="B")
    assert store.cancel("telegram") == 2
    assert store.pending("telegram") == []


def test_bad_reminder_args_report_failure(monkeypatch, tmp_path):
    import reminders as rmod
    import skill_engine as smod

    store = _store(tmp_path)
    monkeypatch.setattr(rmod, "DEFAULT_REMINDER_STORE", store)
    monkeypatch.setattr(smod, "DEFAULT_REMINDER_STORE", store)

    provider = RouteProvider('{"skill":"set_reminder","args":{"what":"","fire_in_seconds":-1}}')
    out = try_skill("提醒我", "telegram", provider, now=1000.0)
    assert out is not None and not out.ok
    assert store.pending("telegram") == []
