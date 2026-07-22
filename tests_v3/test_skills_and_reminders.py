from __future__ import annotations

import tempfile

from reminders import ReminderStore
from skill_engine import SkillContext, execute_skill, skill_tools


def _ctx(now: float = 1000.0) -> SkillContext:
    return SkillContext(chat_id="telegram", now=now)


def test_catalog_shape_is_model_ready():
    # Every skill must present a model-usable spec: name, a WHEN-style description, JSON schema.
    tools = skill_tools()
    assert len(tools) >= 20
    for tool in tools:
        assert tool.name and tool.description
        assert tool.parameters.get("type") == "object"


def test_unknown_skill_fails_softly():
    out = execute_skill("no_such_skill", {}, _ctx())
    assert not out.ok


def test_quick_calc_and_safety():
    assert "= 5888" in execute_skill("quick_calc", {"expression": "128*46"}, _ctx()).note
    assert execute_skill("quick_calc", {"expression": "sqrt(16)"}, _ctx()).note.endswith("= 4")
    # code injection must be rejected, not executed
    assert not execute_skill("quick_calc", {"expression": "__import__('os').getcwd()"}, _ctx()).ok


def test_unit_convert_families_and_temperature():
    assert "8.047" in execute_skill("unit_convert", {"value": 5, "from_unit": "mile", "to_unit": "km"}, _ctx()).note
    assert "212.0" in execute_skill("unit_convert", {"value": 100, "from_unit": "C", "to_unit": "F"}, _ctx()).note
    assert not execute_skill("unit_convert", {"value": 1, "from_unit": "斤", "to_unit": "km"}, _ctx()).ok


def test_date_calc_days_until(monkeypatch):
    import datetime
    now = datetime.datetime(2026, 7, 16, 12, 0).timestamp()
    note = execute_skill("date_calc", {"target_date": "2026-07-20"}, _ctx(now)).note
    assert "4 天" in note


def test_fun_skills_are_wellformed():
    note = execute_skill("dice_roll", {"sides": 20, "count": 2}, _ctx()).note
    assert "D20" in note and "合計" in note
    assert execute_skill("coin_flip", {}, _ctx()).note
    assert "選中了" in execute_skill("decide_pick", {"options": ["A", "B", "C"]}, _ctx()).note
    assert not execute_skill("decide_pick", {"options": ["only-one"]}, _ctx()).ok
    assert "首選" in execute_skill("eat_decider", {}, _ctx()).note


def test_daily_fortune_is_stable_within_a_day():
    a = execute_skill("daily_fortune", {}, _ctx(1_800_000_000.0)).note
    b = execute_skill("daily_fortune", {}, _ctx(1_800_000_000.0 + 3600)).note
    assert a == b  # same day, same 籤


def test_rock_paper_scissors_verdicts():
    note = execute_skill("rock_paper_scissors", {"owner_move": "石頭"}, _ctx()).note
    assert any(k in note for k in ["平手", "月月贏了", "主人贏了"])


def test_notes_todo_expense_roundtrip(monkeypatch, tmp_path):
    import local_store
    import skill_engine as se
    notes = local_store.ListStore("t_notes", directory=str(tmp_path))
    todos = local_store.ListStore("t_todos", directory=str(tmp_path))
    expenses = local_store.ListStore("t_exp", directory=str(tmp_path))
    monkeypatch.setattr(se, "NOTES", notes)
    monkeypatch.setattr(se, "TODOS", todos)
    monkeypatch.setattr(se, "EXPENSES", expenses)

    assert execute_skill("note_add", {"text": "牛奶沒了"}, _ctx()).ok
    assert "牛奶" in execute_skill("note_list", {"query": "牛奶"}, _ctx()).note
    assert execute_skill("todo_add", {"text": "寫周報"}, _ctx()).ok
    assert "寫周報" in execute_skill("todo_list", {}, _ctx()).note
    assert "完成了" in execute_skill("todo_done", {"which": "周報"}, _ctx()).note
    assert "全部做完" in execute_skill("todo_list", {}, _ctx()).note
    import time as _time

    now = _time.time()
    assert execute_skill("expense_log", {"amount": 60, "item": "午餐", "category": "餐飲"}, _ctx()).ok
    # summary window is computed from ctx.now, records carry real created_at - use real now
    summary = execute_skill("expense_summary", {"period": "month"}, SkillContext("telegram", now)).note
    assert "60" in summary


def test_set_reminder_with_repeat(monkeypatch, tmp_path):
    import reminders as rmod
    import skill_engine as se
    store = rmod.ReminderStore(path=str(tmp_path / "r.json"))
    monkeypatch.setattr(rmod, "DEFAULT_REMINDER_STORE", store)
    monkeypatch.setattr(se, "DEFAULT_REMINDER_STORE", store)
    out = execute_skill(
        "set_reminder", {"what": "喝水", "fire_in_seconds": 60, "repeat_every_seconds": 3600}, _ctx(1000.0)
    )
    assert out.ok and "週期" in out.note
    # recurring re-arms instead of retiring, skipping missed occurrences
    r = store.pending("telegram")[0]
    store.mark_fired(r.reminder_id)
    nxt = store.pending("telegram")[0]
    assert nxt.fire_at > 1060.0 and not nxt.fired


def test_reminder_fires_when_due_and_only_once():
    store = ReminderStore(path=str(tempfile.mkdtemp() + "/r.json"))
    store.add("telegram", fire_at=1000.0, text="開會")
    assert store.due(now=999.0) == []
    due = store.due(now=1001.0)
    assert len(due) == 1
    store.mark_fired(due[0].reminder_id)
    assert store.due(now=2000.0) == []


def test_reminder_survives_reload():
    path = tempfile.mkdtemp() + "/r.json"
    ReminderStore(path=path).add("telegram", fire_at=9e12, text="生日")
    assert [r.text for r in ReminderStore(path=path).pending("telegram")] == ["生日"]


def test_current_time_reports_period():
    import datetime
    morning = datetime.datetime(2026, 7, 20, 6, 30).timestamp()
    note = execute_skill("current_time", {}, _ctx(morning)).note
    assert "06:30" in note and "清晨" in note
    night = datetime.datetime(2026, 7, 20, 23, 30).timestamp()
    assert "深夜" in execute_skill("current_time", {}, _ctx(night)).note


def test_list_tasks_reads_runtime_snapshot(monkeypatch):
    import skill_engine as se
    monkeypatch.setattr(se, "RUNTIME_INTROSPECT", lambda: {
        "active": {"objective": "數 py 檔案", "status": "running"},
        "queued": ["查天氣", "寫周報"],
    })
    note = execute_skill("list_tasks", {}, _ctx()).note
    assert "數 py 檔案" in note and "查天氣" in note


def test_task_queue_enqueues_and_drains(tmp_path):
    from yueyue_v3.models import GoalContract, RequestedOutput, StepContract, WorkflowStatus
    from yueyue_v3.providers import ProviderResponse, ScriptedProvider
    from yueyue_v3.runtime import YueYueRuntimeV3

    (tmp_path / "workspace" / "brain").mkdir(parents=True)
    (tmp_path / "workspace" / "brain" / "personality.md").write_text("月月", encoding="utf-8")
    (tmp_path / "workspace" / "brain" / "rules.md").write_text("守規矩", encoding="utf-8")
    provider = ScriptedProvider([ProviderResponse("好", "", []) for _ in range(8)])
    rt = YueYueRuntimeV3(tmp_path, provider, state_dir=tmp_path / "v3")

    # busy workflow -> incoming task queues
    goal = GoalContract("數檔案", [RequestedOutput("n", "d", True, "value", [])], ["c"])
    wf = rt.workflow_engine.create(goal, [StepContract("s1", "s", "observe", "d", ["list_files"])])
    wf.status = WorkflowStatus.AWAITING_PERMISSION
    import copy
    with rt.events.writer_scope():
        state = copy.deepcopy(rt.state)
        state.workflow = wf
        rt._replace_state(state, "test.seed", "t0")
    from yueyue_v3.models import TurnEnvelope, TurnMode
    reply = rt.process_turn(TurnEnvelope("owner", "幫我查一下天氣預報", TurnMode.TASK))
    assert rt.state.task_queue == ["幫我查一下天氣預報"]
    assert reply


def test_remember_this_writes_to_memory(monkeypatch):
    import skill_engine as se
    written = []
    monkeypatch.setattr(se, "MEMORY_WRITE", lambda fact, source: written.append((fact, source)))
    out = execute_skill("remember_this", {"fact": "主人喜歡吃辣", "source": "我超愛吃辣"}, _ctx())
    assert out.ok and "記住了" in out.note
    assert written == [("主人喜歡吃辣", "我超愛吃辣")]


def test_remember_this_empty_fact_fails():
    out = execute_skill("remember_this", {"fact": ""}, _ctx())
    assert not out.ok


def test_blocked_workflow_is_terminal_everywhere(tmp_path):
    # Live 2026-07-22: a vetoed-but-finished task haunted every 「現在有什麼任務」 answer.
    # BLOCKED must read as terminal: no pending note, no introspect active, queue drains past it.
    import copy

    from yueyue_v3.models import GoalContract, RequestedOutput, StepContract, TurnEnvelope, TurnMode, WorkflowStatus
    from yueyue_v3.providers import ProviderResponse, ScriptedProvider
    from yueyue_v3.runtime import YueYueRuntimeV3

    (tmp_path / "workspace" / "brain").mkdir(parents=True)
    (tmp_path / "workspace" / "brain" / "personality.md").write_text("月月", encoding="utf-8")
    (tmp_path / "workspace" / "brain" / "rules.md").write_text("守規矩", encoding="utf-8")
    provider = ScriptedProvider([ProviderResponse("好", "", []) for _ in range(6)])
    rt = YueYueRuntimeV3(tmp_path, provider, state_dir=tmp_path / "v3")

    goal = GoalContract("建立 Hello.txt", [RequestedOutput("c", "d", True, "text", [])], ["c"])
    wf = rt.workflow_engine.create(goal, [StepContract("s1", "s", "act", "d", ["execute_command"])])
    wf.status = WorkflowStatus.BLOCKED
    with rt.events.writer_scope():
        state = copy.deepcopy(rt.state)
        state.workflow = wf
        rt._replace_state(state, "test.seed", "t0")

    # blocked reads as terminal: the note must say "nothing running", never "in progress"
    note = rt._pending_task_note()
    assert "沒有任何任務" in note
    assert "進行中" not in note
    import skill_engine as se
    snap = se.RUNTIME_INTROSPECT()
    assert snap["active"] is None
    # a NEW task while blocked starts fresh instead of queueing behind the corpse
    with rt.events.writer_scope():
        reply = rt._process_turn(TurnEnvelope("owner", "幫我建立另一個檔案 test2.txt", TurnMode.TASK))
    assert rt.state.task_queue == []
    assert reply


def test_idle_note_carries_the_last_finished_task(tmp_path):
    # Task turns are never written to short-term chat context, so 「剛剛那個做完了嗎」 had nothing
    # to answer from and she said 「月月不記得剛剛有什麼任務」 about work just completed.
    import copy

    from yueyue_v3.models import GoalContract, RequestedOutput, StepContract, WorkflowStatus
    from yueyue_v3.providers import ProviderResponse, ScriptedProvider
    from yueyue_v3.runtime import YueYueRuntimeV3

    (tmp_path / "workspace" / "brain").mkdir(parents=True)
    (tmp_path / "workspace" / "brain" / "personality.md").write_text("月月", encoding="utf-8")
    (tmp_path / "workspace" / "brain" / "rules.md").write_text("守規矩", encoding="utf-8")
    rt = YueYueRuntimeV3(tmp_path, ScriptedProvider([ProviderResponse("好", "", [])]), state_dir=tmp_path / "v3")

    goal = GoalContract("在下載路徑建 SpeedTest.txt", [RequestedOutput("c", "d", True, "text", [])], ["c"])
    wf = rt.workflow_engine.create(goal, [StepContract("s1", "s", "act", "d", ["write_file"])])
    wf.status = WorkflowStatus.COMPLETED
    with rt.events.writer_scope():
        state = copy.deepcopy(rt.state)
        state.workflow = wf
        rt._replace_state(state, "test.seed", "t0")

    note = rt._pending_task_note()
    assert "沒有任何任務" in note
    assert "SpeedTest.txt" in note and "剛剛才做完" in note and "不要說不記得" in note
