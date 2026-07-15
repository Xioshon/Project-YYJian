from __future__ import annotations

from yueyue_v3.models import ExecutionEvidence, GoalContract, RequestedOutput, StepContract
from yueyue_v3.workflow import WorkflowEngine


def _usage_workflow():
    engine = WorkflowEngine()
    goal = GoalContract(
        objective="查看 Codex 五小時剩餘用量百分比",
        requested_outputs=[
            RequestedOutput(
                name="五小時剩餘用量",
                description="Codex 五小時限制的剩餘百分比",
                evidence_kind="percentage",
                required_facts=["五小時"],
            )
        ],
        success_criteria=["取得五小時剩餘百分比"],
    )
    steps = [
        StepContract("observe", "觀察目前畫面", "observe", "看見剩餘用量入口", ["get_screen_ui"], ["剩餘用量"]),
        StepContract("open", "打開剩餘用量", "act", "點擊後重新觀察", ["click_ui_element", "get_screen_ui"]),
        StepContract("read", "讀取五小時額度", "verify", "取得五小時百分比", ["get_screen_ui"], ["五小時"]),
    ]
    workflow = engine.create(goal, steps)
    engine.approve(workflow)
    return engine, workflow


def test_tail_ui_fact_advances_observe_step_but_does_not_complete_goal() -> None:
    engine, workflow = _usage_workflow()
    evidence = ExecutionEvidence(
        step_id="observe",
        source="get_screen_ui",
        status="ok",
        summary="畫面已觀察",
        facts={"visible_text": [*(f"無關控件 {index}" for index in range(300)), "剩餘用量 1%"]},
        observation_revision="rev-1",
    )
    engine.add_evidence(workflow, evidence)
    decision = engine.verify(workflow)

    assert decision.step_satisfied is True
    assert workflow.current_step_index == 1
    assert decision.goal_satisfied is False
    assert "五小時剩餘用量" in decision.missing_outputs


def test_observe_step_completes_from_required_sources_without_text_matching() -> None:
    engine = WorkflowEngine()
    workflow = engine.create(
        GoalContract("Inspect desktop", [RequestedOutput("result", "requested result")], []),
        [
            StepContract(
                "observe",
                "Inspect current desktop",
                "observe",
                "A screenshot and window inventory have been collected",
                ["capture_screen", "list_windows"],
                required_sources=["capture_screen", "list_windows"],
                completion_mode="all_sources",
            ),
            StepContract("act", "Focus target", "act", "Target focused", ["focus_window"]),
        ],
    )
    engine.approve(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence("observe", "capture_screen", "ok", "Screen captured."),
    )
    assert engine.verify(workflow).step_satisfied is False
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "observe",
            "list_windows",
            "ok",
            "Visible windows listed.",
            {"windows": [{"title": "Codex"}, {"title": "SiliconFlow - Chrome"}]},
        ),
    )

    decision = engine.verify(workflow)

    assert decision.step_satisfied is True
    assert workflow.current_step_index == 1
    assert engine.allowed_tools(workflow) == ["focus_window"]


def test_new_screenshot_revision_without_new_facts_is_not_progress() -> None:
    engine = WorkflowEngine()
    workflow = engine.create(
        GoalContract("Inspect desktop", [RequestedOutput("result", "requested result")], []),
        [StepContract("observe", "Inspect", "observe", "Find target", ["capture_screen"])],
    )
    engine.approve(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "observe",
            "capture_screen",
            "ok",
            "Screen captured.",
            {"width": 2560, "height": 1440, "window_title": "SiliconFlow - Chrome"},
            observation_revision="revision-1",
        ),
    )
    first = engine.progress_signature(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "observe",
            "capture_screen",
            "ok",
            "Screen captured.",
            {"width": 2560, "height": 1440, "window_title": "SiliconFlow - Chrome"},
            observation_revision="revision-2",
        ),
    )

    assert engine.progress_signature(workflow) == first


def test_new_structured_fact_is_progress_even_with_same_step() -> None:
    engine = WorkflowEngine()
    workflow = engine.create(
        GoalContract("Inspect desktop", [RequestedOutput("result", "requested result")], []),
        [StepContract("observe", "Inspect", "observe", "Find target", ["list_windows"])],
    )
    engine.approve(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence("observe", "list_windows", "ok", "Listed", {"windows": [{"title": "Chrome"}]}),
    )
    first = engine.progress_signature(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "observe",
            "list_windows",
            "ok",
            "Listed",
            {"windows": [{"title": "Chrome"}, {"title": "Codex"}]},
        ),
    )

    assert engine.progress_signature(workflow) != first


def test_generic_menu_percentage_never_satisfies_five_hour_output() -> None:
    engine, workflow = _usage_workflow()
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            step_id="observe",
            source="get_screen_ui",
            status="ok",
            summary="剩餘用量 1%",
            facts={"visible_text": ["剩餘用量 1%"]},
            observation_revision="rev-1",
        ),
    )
    decision = engine.verify(workflow)
    assert decision.goal_satisfied is False


def test_contextual_five_hour_percentage_completes_goal() -> None:
    engine, workflow = _usage_workflow()
    workflow.current_step_index = 2
    workflow.steps[2].status = workflow.steps[1].status
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            step_id="read",
            source="get_screen_ui",
            status="ok",
            summary="五小時用量：剩餘 37%\n每週用量：剩餘 82%",
            facts={"visible_text": ["五小時用量：剩餘 37%", "每週用量：剩餘 82%"]},
            observation_revision="rev-3",
        ),
    )
    decision = engine.verify(workflow)
    assert decision.goal_satisfied is True
    assert workflow.outputs["五小時剩餘用量"]["value"] == "37%"


def test_successful_action_forces_fresh_observation_before_another_action() -> None:
    engine, workflow = _usage_workflow()
    workflow.current_step_index = 1
    workflow.steps[1].status = workflow.steps[0].status
    engine.add_evidence(
        workflow,
        ExecutionEvidence("open", "click_ui_element", "ok", "clicked", {"observe_needed": True}),
    )

    allowed = engine.allowed_tools(workflow)

    assert allowed == ["get_screen_ui"]


def test_generic_output_description_can_never_prove_its_own_goal() -> None:
    engine = WorkflowEngine()
    workflow = engine.create(
        GoalContract(
            "查看指定介面的實際數值",
            [RequestedOutput("requested_result", "The concrete value or state requested by the owner")],
            ["The requested result is directly evidenced"],
            "guarded",
        ),
        [StepContract("observe", "觀察", "observe", "看見目標", ["capture_screen"])],
    )
    engine.approve(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence("observe", "capture_screen", "ok", "Screen captured for internal observation."),
    )
    engine.add_evidence(
        workflow,
        ExecutionEvidence("observe", "click_screen", "error", "The screenshot expired."),
    )

    decision = engine.verify(workflow)

    assert decision.goal_satisfied is False
    assert decision.extracted_outputs == {}


def test_english_five_hour_output_ignores_unrelated_percentage() -> None:
    engine = WorkflowEngine()
    workflow = engine.create(
        GoalContract(
            "Read the remaining percentage for the five-hour limit",
            [RequestedOutput("five_hour_percentage", "remaining percentage for the five-hour limit")],
            ["five-hour percentage is read"],
        ),
        [StepContract("read", "Read quota", "verify", "five-hour value visible", ["get_screen_ui"])],
    )
    engine.approve(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "read",
            "get_screen_ui",
            "ok",
            "Weekly remaining: 82%\nFive-hour remaining: 37%",
            {"visible_text": ["Weekly remaining: 82%", "Five-hour remaining: 37%"]},
        ),
    )
    decision = engine.verify(workflow)
    assert decision.goal_satisfied is True
    assert workflow.outputs["five_hour_percentage"]["value"] == "37%"


def test_percentage_extractor_associates_value_with_adjacent_label() -> None:
    engine = WorkflowEngine()
    workflow = engine.create(
        GoalContract(
            "Read the remaining percentage for the five-hour limit",
            [
                RequestedOutput(
                    "five_hour_percentage",
                    "remaining percentage for the five-hour limit",
                    required_facts=["The percentage value displayed for the five-hour limit"],
                )
            ],
            [],
        ),
        [StepContract("read", "Read quota", "observe", "five-hour usage is visible", ["get_screen_ui"])],
    )
    engine.approve(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "read",
            "get_screen_ui",
            "ok",
            "5 \u5c0f\u6642\u4f7f\u7528\u60c5\u6cc1\u9650\u5236\n\u5269\u9918 0%\n\u6bcf\u9031\u4f7f\u7528\u4e0a\u9650\n\u5269\u9918 48%",
            observation_revision="usage",
        ),
    )

    decision = engine.verify(workflow)

    assert decision.goal_satisfied is True
    assert workflow.outputs["five_hour_percentage"]["value"] == "0%"


def test_action_step_does_not_pass_when_fresh_observation_shows_wrong_page() -> None:
    engine = WorkflowEngine()
    workflow = engine.create(
        GoalContract(
            "Open Usage",
            [
                RequestedOutput(
                    "usage_page",
                    "Usage page",
                    evidence_kind="screen_state",
                    required_facts=["Usage section within Settings"],
                )
            ],
            [],
        ),
        [
            StepContract(
                "open_usage",
                "Open Usage",
                "act",
                "The Usage section within Settings is displayed",
                ["click_ui_element", "get_screen_ui"],
                ["Usage section within Settings"],
            )
        ],
    )
    engine.approve(workflow)
    engine.add_evidence(workflow, ExecutionEvidence("open_usage", "click_ui_element", "ok", "clicked"))
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "open_usage",
            "get_screen_ui",
            "ok",
            "General settings are visible",
            {"visible_text": ["General", "Work mode", "Permissions"]},
            observation_revision="general-revision",
        ),
    )

    decision = engine.verify(workflow)

    assert decision.step_satisfied is False
    assert decision.goal_satisfied is False
    assert workflow.current_step_index == 0
    assert "click_ui_element" in engine.allowed_tools(workflow)


def test_action_step_passes_when_fresh_observation_matches_semantic_constraint() -> None:
    engine = WorkflowEngine()
    workflow = engine.create(
        GoalContract(
            "Open Usage",
            [
                RequestedOutput(
                    "usage_page",
                    "Usage page",
                    evidence_kind="screen_state",
                    required_facts=["Usage section within Settings"],
                )
            ],
            [],
        ),
        [
            StepContract(
                "open_usage",
                "Open Usage",
                "act",
                "The Usage section within Settings is displayed",
                ["click_ui_element", "get_screen_ui"],
                ["Usage section within Settings"],
            )
        ],
    )
    engine.approve(workflow)
    engine.add_evidence(workflow, ExecutionEvidence("open_usage", "click_ui_element", "ok", "clicked"))
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "open_usage",
            "get_screen_ui",
            "ok",
            "Usage and billing settings are visible",
            {"visible_text": ["Settings", "Usage", "Five-hour limit"]},
            observation_revision="usage-revision",
        ),
    )

    decision = engine.verify(workflow)

    assert decision.step_satisfied is True


def test_observation_matches_entity_fact_without_requiring_assertion_verbs() -> None:
    engine = WorkflowEngine()
    workflow = engine.create(
        GoalContract("Find Codex", [RequestedOutput("window", "Codex window", evidence_kind="status")], []),
        [
            StepContract(
                "locate",
                "Locate Codex",
                "observe",
                "A Codex window is identified",
                ["list_windows"],
                ["Codex window exists and its title is known"],
            )
        ],
    )
    engine.approve(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "locate",
            "list_windows",
            "ok",
            "Visible windows listed.",
            {"windows": [{"window_id": "42", "title": "Codex"}]},
        ),
    )

    decision = engine.verify(workflow)

    assert decision.step_satisfied is True


def test_action_step_can_use_verified_fact_from_previous_step() -> None:
    engine = WorkflowEngine()
    workflow = engine.create(
        GoalContract("Focus Codex", [RequestedOutput("focused", "Codex focused", evidence_kind="screen_state")], []),
        [
            StepContract("locate", "Locate", "observe", "Codex exists", ["list_windows"], ["Codex"]),
            StepContract(
                "focus",
                "Focus",
                "act",
                "Codex is foreground",
                ["focus_window", "get_screen_ui"],
                ["Codex window title from locate_codex_window"],
            ),
        ],
    )
    engine.approve(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence("locate", "list_windows", "ok", "listed", {"windows": [{"title": "Codex"}]}),
    )
    engine.verify(workflow)
    engine.add_evidence(workflow, ExecutionEvidence("focus", "focus_window", "ok", "focused"))
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "focus",
            "get_screen_ui",
            "ok",
            "Active window: Codex",
            {"window_title": "Codex"},
            observation_revision="codex-active",
        ),
    )

    decision = engine.verify(workflow)

    assert decision.step_satisfied is True


def test_disjunctive_visibility_fact_accepts_visible_window_evidence() -> None:
    engine = WorkflowEngine()
    workflow = engine.create(
        GoalContract("Find Codex", [RequestedOutput("window", "Codex", evidence_kind="status")], []),
        [
            StepContract(
                "locate",
                "Locate",
                "observe",
                "Codex is listed",
                ["list_windows"],
                ["The exact window title matching Codex", "Whether the window is already visible or minimized"],
            )
        ],
    )
    engine.approve(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "locate",
            "list_windows",
            "ok",
            "Visible windows listed.",
            {"windows": [{"title": "Codex"}]},
        ),
    )

    assert engine.verify(workflow).step_satisfied is True


def test_action_done_condition_must_match_post_action_observation_even_without_required_facts() -> None:
    engine = WorkflowEngine()
    workflow = engine.create(
        GoalContract("Open Usage", [RequestedOutput("percentage", "five-hour percentage")], []),
        [
            StepContract(
                "open_usage",
                "Open Usage",
                "act",
                "The Usage and billing panel is visible",
                ["click_ui_element", "get_screen_ui"],
            )
        ],
    )
    engine.approve(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "open_usage",
            "get_screen_ui",
            "ok",
            "Settings entry is visible",
            {"visible_text": ["Settings", "Usage and billing"]},
            observation_revision="before",
        ),
    )
    engine.add_evidence(workflow, ExecutionEvidence("open_usage", "click_ui_element", "ok", "clicked"))
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "open_usage",
            "get_screen_ui",
            "ok",
            "General settings are visible",
            {"visible_text": ["General", "Work mode"]},
            observation_revision="after-wrong-page",
        ),
    )

    decision = engine.verify(workflow)

    assert decision.step_satisfied is False
    assert "click_ui_element" in engine.allowed_tools(workflow)


def test_percentage_described_as_text_still_requires_a_numeric_percentage() -> None:
    engine = WorkflowEngine()
    workflow = engine.create(
        GoalContract(
            "Read the remaining five-hour percentage",
            [
                RequestedOutput(
                    "remaining_percentage", "remaining percentage for the five-hour limit", evidence_kind="text"
                )
            ],
            [],
        ),
        [StepContract("observe", "Observe", "observe", "Codex is visible", ["list_windows"])],
    )
    engine.approve(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "observe", "list_windows", "ok", "Visible windows listed.", {"windows": [{"title": "Codex"}]}
        ),
    )

    decision = engine.verify(workflow)

    assert decision.goal_satisfied is False
    assert decision.extracted_outputs == {}


def test_quoted_entity_fact_matches_literal_without_requiring_instruction_words() -> None:
    engine = WorkflowEngine()
    workflow = engine.create(
        GoalContract("Find Codex", [RequestedOutput("window", "window", evidence_kind="status")], []),
        [
            StepContract(
                "locate",
                "Locate",
                "observe",
                "Codex is present",
                ["list_windows"],
                ["Window title contains 'Codex'"],
            )
        ],
    )
    engine.approve(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence("locate", "list_windows", "ok", "listed", {"windows": [{"title": "Codex"}]}),
    )

    assert engine.verify(workflow).step_satisfied is True


def test_negative_parenthetical_does_not_become_required_visible_text() -> None:
    engine = WorkflowEngine()
    workflow = engine.create(
        GoalContract("Verify Codex", [RequestedOutput("window", "Codex", evidence_kind="status")], []),
        [
            StepContract(
                "verify",
                "Verify",
                "observe",
                "The screen shows the Codex application UI (not a different app)",
                ["get_screen_ui"],
            )
        ],
    )
    engine.approve(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence("verify", "get_screen_ui", "ok", "Active window: Codex", {"window_title": "Codex"}),
    )

    assert engine.verify(workflow).step_satisfied is True


def test_runtime_semantic_gate_prevents_action_advancement_until_verified() -> None:
    engine = WorkflowEngine(require_semantic_actions=True)
    workflow = engine.create(
        GoalContract("Open Settings", [RequestedOutput("settings", "Settings page", evidence_kind="screen_state")], []),
        [
            StepContract(
                "open", "Open Settings", "act", "The Settings page is open", ["click_ui_element", "get_screen_ui"]
            )
        ],
    )
    engine.approve(workflow)
    engine.add_evidence(workflow, ExecutionEvidence("open", "click_ui_element", "ok", "clicked"))
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "open",
            "get_screen_ui",
            "ok",
            "A menu contains Settings",
            {"visible_text": ["File", "Settings"]},
            observation_revision="menu",
        ),
    )

    assert engine.verify(workflow).step_satisfied is False

    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "open",
            "semantic_verifier",
            "ok",
            "Settings is only a menu item, not an open page.",
            {"condition_satisfied": False},
            observation_revision="menu",
        ),
    )
    assert engine.verify(workflow).step_satisfied is False

    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "open",
            "semantic_verifier",
            "ok",
            "The Settings page is now open.",
            {"condition_satisfied": True},
            observation_revision="menu",
        ),
    )
    assert engine.verify(workflow).step_satisfied is True
