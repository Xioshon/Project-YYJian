from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .models import (
    ActionEnvelope,
    ExecutionEvidence,
    GoalContract,
    PermissionState,
    RequestedOutput,
    StepContract,
)
from .permissions import PermissionController
from .workflow import WorkflowEngine


@dataclass(frozen=True)
class ReplayCase:
    name: str
    check: Callable[[], tuple[bool, str]]


@dataclass(frozen=True)
class ReplayResult:
    name: str
    passed: bool
    message: str


def build_regression_corpus() -> list[ReplayCase]:
    return [
        ReplayCase("menu_percentage_is_not_five_hour_quota", _menu_percentage_not_goal),
        ReplayCase("tail_ui_fact_advances_observation", _tail_fact_advances),
        ReplayCase("contextual_five_hour_quota_completes", _contextual_quota_completes),
        ReplayCase("traditional_continue_approves_pending_action", _traditional_continue_approves),
    ]


def run_replay_corpus(cases: list[ReplayCase]) -> list[ReplayResult]:
    results: list[ReplayResult] = []
    for case in cases:
        try:
            passed, message = case.check()
        except Exception as exc:
            passed, message = False, f"{type(exc).__name__}: {exc}"
        results.append(ReplayResult(case.name, bool(passed), str(message)))
    return results


def _workflow():
    engine = WorkflowEngine()
    goal = GoalContract(
        "查看五小時剩餘用量",
        [RequestedOutput("五小時剩餘用量", "五小時限制的剩餘百分比", True, "percentage", ["五小時"])],
        ["取得五小時剩餘百分比"],
        "guarded",
    )
    steps = [
        StepContract("observe", "找到剩餘用量入口", "observe", "看見剩餘用量", ["get_screen_ui"], ["剩餘用量"]),
        StepContract("read", "讀取五小時用量", "verify", "取得五小時百分比", ["get_screen_ui"], ["五小時"]),
    ]
    workflow = engine.create(goal, steps)
    engine.approve(workflow)
    return engine, workflow


def _menu_percentage_not_goal() -> tuple[bool, str]:
    engine, workflow = _workflow()
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "observe",
            "get_screen_ui",
            "ok",
            "剩餘用量 1%",
            {"visible_text": ["剩餘用量 1%"]},
            observation_revision="r1",
        ),
    )
    decision = engine.verify(workflow)
    return not decision.goal_satisfied, decision.reason


def _tail_fact_advances() -> tuple[bool, str]:
    engine, workflow = _workflow()
    visible = [*(f"無關控件 {index}" for index in range(300)), "剩餘用量 1%"]
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "observe",
            "get_screen_ui",
            "ok",
            "structured observation",
            {"visible_text": visible},
            observation_revision="r1",
        ),
    )
    decision = engine.verify(workflow)
    return decision.step_satisfied and workflow.current_step_index == 1, decision.reason


def _contextual_quota_completes() -> tuple[bool, str]:
    engine, workflow = _workflow()
    workflow.current_step_index = 1
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "read",
            "get_screen_ui",
            "ok",
            "五小時用量：剩餘 37%",
            {"visible_text": ["五小時用量：剩餘 37%"]},
            observation_revision="r2",
        ),
    )
    decision = engine.verify(workflow)
    value = workflow.outputs.get("五小時剩餘用量", {}).get("value")
    return decision.goal_satisfied and value == "37%", decision.reason


def _traditional_continue_approves() -> tuple[bool, str]:
    controller = PermissionController()
    state = PermissionState()
    controller.request(state, ActionEnvelope("click_ui_element", {"element_id": "9"}, "open", "guarded"))
    decision = controller.apply_reply(state, "繼續")
    allowed = controller.permits(state, ActionEnvelope("click_ui_element", {"element_id": "9"}, "open", "guarded"))
    return decision == "turn" and allowed, decision
