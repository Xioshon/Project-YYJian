from __future__ import annotations

import tempfile
import time
from pathlib import Path

from yueyue_v3.models import GoalContract, RequestedOutput, StepContract, WorkflowStatus
from yueyue_v3.providers import ProviderResponse, ScriptedProvider
from yueyue_v3.runtime import YueYueRuntimeV3


def _runtime() -> YueYueRuntimeV3:
    tmp = tempfile.mkdtemp()
    (Path(tmp) / "workspace" / "brain").mkdir(parents=True, exist_ok=True)
    (Path(tmp) / "workspace" / "memory").mkdir(parents=True, exist_ok=True)
    (Path(tmp) / "workspace" / "brain" / "personality.md").write_text("月月", encoding="utf-8")
    (Path(tmp) / "workspace" / "brain" / "rules.md").write_text("守規矩", encoding="utf-8")
    (Path(tmp) / "workspace" / "memory" / "profile.json").write_text("{}", encoding="utf-8")
    provider = ScriptedProvider([ProviderResponse("好", "", []) for _ in range(20)])
    return YueYueRuntimeV3(tmp, provider, state_dir=Path(tmp) / "v3")


def test_stale_awaiting_workflow_is_treated_as_expired():
    rt = _runtime()
    goal = GoalContract("x", [RequestedOutput("n", "d", True, "value", [])], ["c"])
    wf = rt.workflow_engine.create(goal, [StepContract("step_1", "s", "observe", "d", ["list_files"])])
    wf.status = WorkflowStatus.AWAITING_PERMISSION
    wf.updated_at = time.time() - 60 * 60  # 1 hour ago
    rt.state.workflow = wf

    assert rt._workflow_is_stale(rt.state.workflow) is True


def test_fresh_awaiting_workflow_is_not_expired():
    rt = _runtime()
    goal = GoalContract("x", [RequestedOutput("n", "d", True, "value", [])], ["c"])
    wf = rt.workflow_engine.create(goal, [StepContract("step_1", "s", "observe", "d", ["list_files"])])
    wf.status = WorkflowStatus.AWAITING_PERMISSION
    wf.updated_at = time.time() - 30  # 30 seconds ago
    rt.state.workflow = wf

    assert rt._workflow_is_stale(rt.state.workflow) is False


def test_completed_workflow_is_never_stale():
    rt = _runtime()
    goal = GoalContract("x", [RequestedOutput("n", "d", True, "value", [])], ["c"])
    wf = rt.workflow_engine.create(goal, [StepContract("step_1", "s", "observe", "d", ["list_files"])])
    wf.status = WorkflowStatus.COMPLETED
    wf.updated_at = time.time() - 60 * 60
    rt.state.workflow = wf

    assert rt._workflow_is_stale(rt.state.workflow) is False


def test_stale_workflow_does_not_hijack_a_later_chat_turn():
    # End-to-end: a stale awaiting-permission workflow must be cleared so an unrelated chat
    # message is processed fresh (not swallowed as a permission reply / task continuation).
    rt = _runtime()
    goal = GoalContract("數檔案", [RequestedOutput("n", "d", True, "value", [])], ["c"])
    wf = rt.workflow_engine.create(goal, [StepContract("step_1", "s", "observe", "d", ["execute_command"])])
    wf.status = WorkflowStatus.AWAITING_PERMISSION
    wf.updated_at = time.time() - 60 * 60
    rt.state.workflow = wf
    rt.state.permission.pending_action = None

    reply = rt.chat("今天天氣不錯欸")
    # After the turn the stale workflow must be gone (expired), not lingering.
    assert rt.state.workflow is None
    assert isinstance(reply.get("content"), str) and reply["content"]
