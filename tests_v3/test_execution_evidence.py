from __future__ import annotations

from yueyue_v3.models import ExecutionEvidence, GoalContract, RequestedOutput, StepContract
from yueyue_v3.runtime import _evidence_note, _report_value_grounded
from yueyue_v3.workflow import WorkflowEngine


def _workflow_with_evidence(*evidence: ExecutionEvidence):
    engine = WorkflowEngine()
    goal = GoalContract("count files", [RequestedOutput("count", "file count", True, "value", [])], ["c"])
    wf = engine.create(goal, [StepContract("step_1", "observe", "observe", "d", ["execute_python"])])
    for item in evidence:
        wf.evidence.append(item)
    return wf


def test_evidence_note_renders_tool_results_for_the_model():
    # Regression (gap battery 2026-07-15): after a permission round-trip the model never saw the
    # executed tool's output (transcript resets), so it re-requested the same tool 8 times in a
    # row, burning an owner approval each time. The evidence note is what puts results in view.
    wf = _workflow_with_evidence(
        ExecutionEvidence("step_1", "execute_python", "ok", "stdout: 91", {"stdout": "91"})
    )
    note = _evidence_note(wf)
    assert "execute_python" in note
    assert "91" in note


def test_evidence_note_empty_without_evidence():
    wf = _workflow_with_evidence()
    assert _evidence_note(wf) == ""


def test_report_result_rejects_fabricated_value():
    # Regression: the model reported "soul_core.md" as a grep answer when no such file exists in
    # any observation - prompt-side "never invent one" is not enforcement.
    wf = _workflow_with_evidence(
        ExecutionEvidence("step_1", "list_files", "ok", "personality.md, rules.md", {})
    )
    ok, why = _report_value_grounded({"results": [{"name": "file", "value": "soul_core.md"}]}, wf)
    assert not ok
    assert "soul_core.md" in why


def test_report_result_accepts_value_present_in_evidence():
    wf = _workflow_with_evidence(
        ExecutionEvidence("step_1", "execute_python", "ok", "stdout: 91", {"stdout": "91"})
    )
    ok, why = _report_value_grounded({"results": [{"name": "count", "value": 91}]}, wf)
    assert ok, why


def test_report_result_rejects_before_any_observation():
    wf = _workflow_with_evidence()
    ok, why = _report_value_grounded({"results": [{"name": "count", "value": 42}]}, wf)
    assert not ok


def test_evidence_captures_command_stdout():
    # Regression: execute_command's stdout never reached evidence facts, so the actual result
    # (e.g. a version number) was invisible to the model, the grounding gate, and output binding.
    from yueyue_v3.runtime import _evidence_from_result
    from yueyue_v3.tools import V3ToolResult

    wf = _workflow_with_evidence()
    result = V3ToolResult("ok", "Command completed.", data={"stdout": "Python 3.14.0", "returncode": 0})
    evidence = _evidence_from_result(wf, "execute_command", result)
    assert evidence.facts.get("stdout") == "Python 3.14.0"


def test_text_output_binding_prefers_stdout_over_generic_status():
    # Regression: 「找到了，結果是 Command completed.。」- the binder took the tool's generic
    # completion message as the requested value instead of the stdout that held the answer.
    from yueyue_v3.models import ExecutionEvidence as EE
    from yueyue_v3.models import GoalContract, RequestedOutput, StepContract
    from yueyue_v3.workflow import WorkflowEngine

    engine = WorkflowEngine()
    goal = GoalContract(
        "python version", [RequestedOutput("version", "python version", True, "text", [])], ["c"]
    )
    wf = engine.create(goal, [StepContract("step_1", "observe", "observe", "d", ["execute_command"])])
    wf.evidence.append(
        EE("step_1", "execute_command", "ok", "Command completed.", {"stdout": "Python 3.14.0"})
    )
    engine.verify(wf)
    assert "3.14" in str(wf.outputs.get("version", ""))
    assert "command completed" not in str(wf.outputs.get("version", "")).casefold()


def test_generic_status_binding_is_pattern_based():
    # "File list completed." slipped past the first enumerated blocklist within hours - the
    # rejection must be shape-based, not a phrase list.
    from yueyue_v3.workflow import _non_generic

    for generic in ["Command completed.", "File list completed.", "File written.", "Search completed."]:
        assert _non_generic(generic) == ""
    # Updated 2026-07-22: report_result's internal wording is also not an owner-facing answer -
    # the owner saw 「結果是 Recorded derived result: ...」. The named fact carries the real value.
    assert _non_generic("Recorded derived result: n=5") == ""
    for real in ["Python 3.14.3", "hello-yueyue-42", "19"]:
        assert _non_generic(real) == real


def test_planner_backfills_read_tools_for_empty_steps():
    # A step whose allowed_tools were all filtered out (hallucinated/social tools) is a dead-end
    # ("No safe capability"). _parse must backfill the safe read-only set instead.
    from yueyue_v3.planning import GoalPlannerV3

    planner = GoalPlannerV3(provider=None, tool_names=lambda: ["read_file", "list_files", "search_in_files", "write_file"])
    raw = {
        "objective": "find which file mentions X",
        "requested_outputs": [{"name": "filename", "description": "the file", "evidence_kind": "text"}],
        "success_criteria": ["filename reported"],
        "steps": [
            {
                "name": "search",
                "kind": "act",
                "done_condition": "found",
                "allowed_tools": ["grep", "react_to_message"],  # none survive the domain filter
            },
            {
                "name": "report",
                "kind": "reply",
                "done_condition": "told the owner",
                "allowed_tools": [],  # reply steps legitimately carry no tools - must stay untouched
            },
        ],
    }
    planned = planner._parse(raw, "find which file mentions X", ["read_file", "list_files", "search_in_files", "write_file"])
    assert planned is not None
    step = planned.steps[0]
    assert step.allowed_tools, "empty-tool step must be backfilled, not stored as a dead-end"
    assert set(step.allowed_tools) <= {"read_file", "list_files", "search_in_files"}
    assert step.kind == "observe"
    # the reply step keeps its no-tools shape
    assert planned.steps[1].kind == "reply"
    assert planned.steps[1].allowed_tools == []


def test_list_payload_tools_reach_evidence():
    # search_in_files returns a bare list; it must not vanish from evidence (the found filenames
    # were never visible to the model, the grounding gate, or output binding).
    from yueyue_v3.runtime import _evidence_from_result
    from yueyue_v3.tools import V3ToolResult

    wf = _workflow_with_evidence()
    result = V3ToolResult(
        "ok", "Search completed.", data=[{"file": "personality.md", "line": 3, "text": "雌小鬼"}]
    )
    evidence = _evidence_from_result(wf, "search_in_files", result)
    assert "personality.md" in str(evidence.facts.get("results", ""))


def test_empty_text_value_does_not_complete_the_goal():
    # Regression: 「找到了，結果是 。」- all-generic evidence produced "" and the goal completed.
    from yueyue_v3.models import ExecutionEvidence as EE
    from yueyue_v3.models import GoalContract, RequestedOutput, StepContract
    from yueyue_v3.workflow import WorkflowEngine

    engine = WorkflowEngine()
    goal = GoalContract("find file", [RequestedOutput("filename", "the file", True, "text", [])], ["c"])
    wf = engine.create(goal, [StepContract("step_1", "observe", "observe", "d", ["search_in_files"])])
    wf.evidence.append(EE("step_1", "search_in_files", "ok", "Search completed.", {}))
    decision = engine.verify(wf)
    assert not decision.goal_satisfied
    assert "filename" in decision.missing_outputs


def test_goal_not_satisfied_while_action_steps_pending():
    # Regression: a write-append-verify task completed after the FIRST write because the read-back
    # bound an output value; pending act steps must hold the goal open.
    from yueyue_v3.models import ExecutionEvidence as EE
    from yueyue_v3.models import GoalContract, RequestedOutput, StepContract
    from yueyue_v3.workflow import WorkflowEngine

    engine = WorkflowEngine()
    goal = GoalContract(
        "write step1 then append step2", [RequestedOutput("content", "final content", True, "text", [])], ["c"]
    )
    wf = engine.create(
        goal,
        [
            StepContract("step_1", "write step1", "act", "written", ["write_file", "read_file"]),
            StepContract("step_2", "append step2", "act", "appended", ["write_file", "read_file"]),
            StepContract("step_3", "read back", "observe", "confirmed", ["read_file"]),
        ],
    )
    wf.evidence.append(EE("step_1", "write_file", "ok", "File written.", {}))
    wf.evidence.append(EE("step_1", "read_file", "ok", "step1", {"text": "step1"}))
    decision = engine.verify(wf)
    assert not decision.goal_satisfied, "goal must stay open while append step is pending"


def test_action_arguments_are_groundable_evidence():
    # Regression: the model wrote "hello-yueyue-42" via write_file, then honestly reported that
    # value - and the grounding gate rejected it because write_file's result never echoes content.
    from yueyue_v3.runtime import _evidence_from_result
    from yueyue_v3.tools import V3ToolResult

    wf = _workflow_with_evidence()
    result = V3ToolResult("ok", "File written.", data={})
    evidence = _evidence_from_result(
        wf, "write_file", result, {"filename": "project/a.txt", "content": "hello-yueyue-42"}
    )
    assert evidence.facts.get("arg_content") == "hello-yueyue-42"
    wf.evidence.append(evidence)
    ok, why = _report_value_grounded({"results": [{"name": "content", "value": "hello-yueyue-42"}]}, wf)
    assert ok, why


def test_text_binding_never_binds_a_facts_json_blob():
    # Regression: list_files evidence got its facts dumped as the "file content" output
    # (「找到了，結果是 results ["project\gaptest...」).
    from yueyue_v3.models import ExecutionEvidence as EE
    from yueyue_v3.models import GoalContract, RequestedOutput, StepContract
    from yueyue_v3.workflow import WorkflowEngine

    engine = WorkflowEngine()
    goal = GoalContract("read the file", [RequestedOutput("content", "file content", True, "text", [])], ["c"])
    wf = engine.create(goal, [StepContract("step_1", "observe", "observe", "d", ["read_file", "list_files"])])
    wf.evidence.append(
        EE("step_1", "list_files", "ok", "File list completed.", {"results": '["a.txt", "b.txt"]'})
    )
    decision = engine.verify(wf)
    assert not decision.goal_satisfied
    assert "content" in decision.missing_outputs
    # once a real read exists, the earlier junk is skipped and the real text binds
    wf.evidence.append(EE("step_1", "read_file", "ok", "hello-yueyue-42", {"text": "hello-yueyue-42"}))
    decision = engine.verify(wf)
    assert wf.outputs.get("content") == "hello-yueyue-42"


def test_file_act_step_gets_no_screenshot_verification():
    # Regression 2026-07-20: every act step had capture_screen appended, so a file-write task
    # screenshotted the desktop and stalled. Only genuine desktop/UI acts get screen verification.
    from yueyue_v3.planning import GoalPlannerV3

    domain = ["write_file", "read_file", "execute_python", "get_screen_ui", "capture_screen"]
    planner = GoalPlannerV3(provider=None, tool_names=lambda: domain)
    raw = {
        "objective": "在下載夾建 Hello.txt 寫入內容",
        "requested_outputs": [{"name": "content", "description": "file content", "evidence_kind": "text"}],
        "success_criteria": ["file exists with content"],
        "steps": [
            {"name": "write", "kind": "act", "done_condition": "written", "allowed_tools": ["write_file"]},
            {"name": "read back", "kind": "observe", "done_condition": "confirmed", "allowed_tools": ["read_file"]},
        ],
    }
    planned = planner._parse(raw, raw["objective"], domain)
    write_step = planned.steps[0]
    assert "capture_screen" not in write_step.allowed_tools
    assert "get_screen_ui" not in write_step.allowed_tools


def test_desktop_act_step_keeps_screenshot_verification():
    from yueyue_v3.planning import GoalPlannerV3

    domain = ["click_ui_element", "get_screen_ui", "capture_screen"]
    planner = GoalPlannerV3(provider=None, tool_names=lambda: domain)
    raw = {
        "objective": "點一下暫停按鈕",
        "requested_outputs": [{"name": "state", "description": "screen", "evidence_kind": "screen_state"}],
        "success_criteria": ["paused"],
        "steps": [
            {"name": "click pause", "kind": "act", "done_condition": "clicked",
             "allowed_tools": ["click_ui_element"], "required_facts": ["paused"]},
            {"name": "confirm", "kind": "observe", "done_condition": "seen", "allowed_tools": ["get_screen_ui"]},
        ],
    }
    planned = planner._parse(raw, raw["objective"], domain)
    assert "capture_screen" in planned.steps[0].allowed_tools


def test_multi_task_split_enqueues_independent_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("YUEYUE_TASK_SPLIT", "1")
    from yueyue_v3.models import TurnEnvelope, TurnMode
    from yueyue_v3.providers import ProviderResponse, ScriptedProvider
    from yueyue_v3.runtime import YueYueRuntimeV3

    (tmp_path / "workspace" / "brain").mkdir(parents=True)
    (tmp_path / "workspace" / "brain" / "personality.md").write_text("月月", encoding="utf-8")
    (tmp_path / "workspace" / "brain" / "rules.md").write_text("守規矩", encoding="utf-8")
    provider = ScriptedProvider([ProviderResponse('["建立 Hello.txt", "建立自我介紹.txt"]', "", [])])
    rt = YueYueRuntimeV3(tmp_path, provider, state_dir=tmp_path / "v3")

    turn = TurnEnvelope("owner", "幫我建 Hello.txt，再建自我介紹.txt", TurnMode.TASK)
    first = rt._maybe_split_tasks(turn)
    assert first == "建立 Hello.txt"
    assert rt.state.task_queue == ["建立自我介紹.txt"]


def test_single_task_is_not_split(tmp_path, monkeypatch):
    monkeypatch.setenv("YUEYUE_TASK_SPLIT", "1")
    from yueyue_v3.models import TurnEnvelope, TurnMode
    from yueyue_v3.providers import ProviderResponse, ScriptedProvider
    from yueyue_v3.runtime import YueYueRuntimeV3

    (tmp_path / "workspace" / "brain").mkdir(parents=True)
    (tmp_path / "workspace" / "brain" / "personality.md").write_text("月月", encoding="utf-8")
    (tmp_path / "workspace" / "brain" / "rules.md").write_text("守規矩", encoding="utf-8")
    # sequential steps of ONE task -> model returns single-element array -> no split
    provider = ScriptedProvider([ProviderResponse('["建立檔案並寫入 step1 再追加 step2"]', "", [])])
    rt = YueYueRuntimeV3(tmp_path, provider, state_dir=tmp_path / "v3")
    turn = TurnEnvelope("owner", "建立檔案寫入 step1，然後追加 step2", TurnMode.TASK)
    assert rt._maybe_split_tasks(turn) == turn.text
    assert rt.state.task_queue == []


def test_command_act_step_verifies_on_success():
    # Root cause of the 7-minute retry loop (live 2026-07-21): ACTION_SOURCES was desktop-only, so
    # an act step driven by execute_command could never verify - the file was created on the first
    # try and the model kept retrying because the workflow said "action has not succeeded yet".
    from yueyue_v3.models import ExecutionEvidence as EE
    from yueyue_v3.models import GoalContract, RequestedOutput, StepContract
    from yueyue_v3.workflow import WorkflowEngine

    engine = WorkflowEngine(require_semantic_actions=True)
    goal = GoalContract("建立 Hello.txt", [RequestedOutput("done", "d", True, "text", [])], ["c"])
    wf = engine.create(goal, [
        StepContract("step_1", "建檔", "act", "檔案建立", ["execute_command"]),
        StepContract("step_2", "讀回", "observe", "內容確認", ["read_file"]),
    ])
    engine.add_evidence(wf, EE("step_1", "execute_command", "ok", "Command completed.", {"returncode": 0}))
    engine.verify(wf)
    assert wf.steps[0].status.value == "verified", "a successful command IS the act's evidence"


def test_ui_act_step_still_needs_a_fresh_look():
    # The loosening must not leak to UI actions: a click still requires post-action observation.
    from yueyue_v3.models import ExecutionEvidence as EE
    from yueyue_v3.models import GoalContract, RequestedOutput, StepContract
    from yueyue_v3.workflow import WorkflowEngine

    engine = WorkflowEngine(require_semantic_actions=True)
    goal = GoalContract("按暫停", [RequestedOutput("s", "d", True, "screen_state", ["paused"])], ["c"])
    wf = engine.create(goal, [StepContract("step_1", "click", "act", "paused", ["click_ui_element"])])
    wf.evidence.append(EE("step_1", "click_ui_element", "ok", "clicked", {}))
    satisfied, reason = engine._verify_step(wf.steps[0], wf.evidence)
    assert not satisfied and "observation" in reason.lower()


def test_repeated_identical_call_counts_as_no_progress():
    # The old guard compared progress signatures, which changed on every retry because each
    # attempt appended evidence - so an identical command could loop forever.
    import json as _json
    a = f"execute_command:{_json.dumps({'command': 'echo hi'}, sort_keys=True)[:400]}"
    b = f"execute_command:{_json.dumps({'command': 'echo hi'}, sort_keys=True)[:400]}"
    c = f"execute_command:{_json.dumps({'command': 'echo bye'}, sort_keys=True)[:400]}"
    assert a == b and a != c


def test_write_file_reaches_absolute_paths_but_never_system_dirs(tmp_path):
    # Live 2026-07-21/22: forcing shell redirects for the Downloads folder failed with
    # "'>' is not recognized". write_file is permission-gated, so an absolute path outside the
    # workspace is allowed - direct writes have no shell quoting to get wrong.
    import os

    from core_tools import is_protected_system_path, real_write_file

    target = tmp_path / "sub" / "Hello.txt"
    result = real_write_file(str(target), "Hello World!")
    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "Hello World!"

    system_target = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "evil.txt")
    assert is_protected_system_path(system_target)
    assert real_write_file(system_target, "x").status == "error"
    assert not is_protected_system_path(str(tmp_path / "ok.txt"))


def test_permission_wording_avoids_the_stiff_idiom():
    # Owner 2026-07-22: 「點頭」 reads oddly. Owner-FACING canned lines must not use it (the two
    # remaining source hits are the instructions telling the model to avoid it, which is correct).
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[1] / "yueyue_v3" / "runtime.py"
    offenders = [
        line.strip()
        for line in source.read_text(encoding="utf-8").splitlines()
        if "點頭" in line and not any(hint in line for hint in ("別用", "NOT use", "idiom"))
    ]
    assert not offenders, f"owner-facing canned lines still say 點頭: {offenders}"


def test_report_result_value_binds_without_internal_wording():
    # Live 2026-07-22: the owner saw 「結果是 Recorded derived result: Hello.txt 最終內容=...」.
    # The exactly-named fact must win over report_result's internal summary.
    from yueyue_v3.models import ExecutionEvidence as EE
    from yueyue_v3.models import GoalContract, RequestedOutput, StepContract
    from yueyue_v3.workflow import WorkflowEngine, _non_generic

    assert _non_generic("Recorded derived result: x=1") == ""

    engine = WorkflowEngine()
    goal = GoalContract("建檔", [RequestedOutput("final_content", "d", True, "text", [])], ["c"])
    wf = engine.create(goal, [StepContract("step_1", "s", "observe", "d", ["read_file"])])
    engine.add_evidence(wf, EE(
        "step_1", "report_result", "ok",
        "Recorded derived result: final_content=Hello World!",
        {"final_content": "Hello World!"},
    ))
    engine.verify(wf)
    assert wf.outputs.get("final_content") == "Hello World!"
    assert "Recorded" not in str(wf.outputs.get("final_content"))


def test_grounded_task_answer_survives_the_social_gate():
    # Live 2026-07-22: 「現在有什麼任務」 got its honest answer guillotined to just 「有兩個任務喔：」
    # one turn, then replaced by the 「走神」 canned line the next. A reply REPORTING a real
    # runtime fact the owner directly asked for must survive intact.
    from yueyue_v3.runtime import _chat_reply_violates_social_policy

    listing = (
        "有兩件\n"
        "一件在等你說可以：建 Hello.txt\n"
        "還有一件排著：建 月月見自我介紹.txt"
    )
    assert _chat_reply_violates_social_policy(listing, "現在有什麼任務") is True
    assert _chat_reply_violates_social_policy(listing, "現在有什麼任務", grounded=True) is False
    # ungrounded smalltalk keeps the tight ceiling - grounding is not a blanket bypass
    rambling = "今天天氣真好呀\n月月剛剛在曬太陽\n主人要不要一起\n然後再去散步"
    assert _chat_reply_violates_social_policy(rambling, "在幹嘛", grounded=True) is True


def test_taiwan_particle_is_repaired_not_rejected():
    # The particle is a mechanical slip, not a wrong answer - repairing it beats burning a
    # regeneration and landing on the canned fallback (which is what the owner actually saw).
    from voice_contract import repair_taiwan_particles, voice_register_violation

    assert voice_register_violation("目前沒有正在進行的任務喔", allow_simplified=False)
    repaired = repair_taiwan_particles("目前沒有正在進行的任務喔")
    assert repaired == "目前沒有正在進行的任務"
    assert not voice_register_violation(repaired, allow_simplified=False)
    assert repair_taiwan_particles("是耶，月月也這麼覺得") == "是誒，月月也這麼覺得"
    # mid-word occurrences must survive untouched
    assert repair_taiwan_particles("聖誕跟耶誕是同一個") == "聖誕跟耶誕是同一個"


def test_watchdog_stall_alert_is_in_voice_not_a_crash_log():
    import inspect

    import agent_watchdog

    # Only CODE lines matter - comments explaining the old wording are documentation, not output.
    code = "\n".join(
        line
        for line in inspect.getsource(agent_watchdog).splitlines()
        if not line.lstrip().startswith("#")
    )
    for leak in ("診斷已存檔", "訊息處理卡住", "自己重啟恢復"):
        assert leak not in code, f"raw diagnostic wording still owner-facing: {leak}"
