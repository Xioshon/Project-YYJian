from __future__ import annotations

from core_tools import ToolResult
from yueyue_v3.models import ExecutionEvidence
from yueyue_v3.planning import GoalPlannerV3
from yueyue_v3.providers import ProviderResponse, ScriptedProvider
from yueyue_v3.tools import _legacy_result
from yueyue_v3.workflow import WorkflowEngine

ALL_NAMES = [
    "read_file",
    "list_files",
    "search_in_files",
    "execute_command",
    "execute_python",
    "write_file",
    "delete_file",
    "get_screen_ui",
]


def test_read_only_file_fallback_never_exposes_command_or_mutation_tools() -> None:
    planner = GoalPlannerV3(ScriptedProvider([ProviderResponse("I cannot format this", "", [])]), lambda: ALL_NAMES)
    planned = planner.plan("只讀取 C:\\Agent\\README.md 的第一個標題，不要修改檔案")
    exposed = {name for step in planned.steps for name in step.allowed_tools}

    assert exposed <= {"read_file", "list_files", "search_in_files"}
    assert all(step.kind != "act" for step in planned.steps)


def test_workspace_readme_request_stays_inside_read_only_tool_domain() -> None:
    contract = {
        "objective": "讀取 workspace 裡的 README 摘要",
        "requested_outputs": [{"name": "summary", "description": "README 摘要", "evidence_kind": "text"}],
        "success_criteria": ["取得 README 摘要"],
        "steps": [
            {
                "name": "讀取 README",
                "kind": "observe",
                "done_condition": "取得文字",
                "allowed_tools": ["read_file", "analyze_media"],
                "required_sources": ["read_file"],
                "completion_mode": "all_sources",
            },
            {
                "name": "回報",
                "kind": "reply",
                "done_condition": "完成回報",
                "allowed_tools": ["react_to_message"],
            },
        ],
    }
    provider = ScriptedProvider(
        [ProviderResponse("", "", [{"id": "plan", "name": "submit_goal_contract", "arguments": contract}])]
    )
    planner = GoalPlannerV3(provider, lambda: [*ALL_NAMES, "analyze_media", "react_to_message"])

    planned = planner.plan("幫我讀取 workspace 裡的 README 摘要，先只規劃，不要真的執行")
    exposed = {name for step in planned.steps for name in step.allowed_tools}

    assert exposed <= {"read_file", "list_files", "search_in_files"}


def test_observe_contract_derives_missing_required_sources_from_allowed_tools() -> None:
    contract = {
        "objective": "讀取 README",
        "requested_outputs": [{"name": "summary", "description": "README 摘要", "evidence_kind": "text"}],
        "success_criteria": ["取得摘要"],
        "steps": [
            {
                "name": "讀取",
                "kind": "observe",
                "done_condition": "取得文字",
                "allowed_tools": ["read_file"],
                "completion_mode": "all_sources",
            },
            {"name": "回報", "kind": "reply", "done_condition": "已回報", "allowed_tools": []},
        ],
    }
    provider = ScriptedProvider(
        [ProviderResponse("", "", [{"id": "plan", "name": "submit_goal_contract", "arguments": contract}])]
    )

    planned = GoalPlannerV3(provider, lambda: ALL_NAMES).plan("讀取 workspace 裡的 README 摘要")

    assert planned.steps[0].required_sources == ["read_file"]


def test_read_file_evidence_can_satisfy_observation_and_text_output() -> None:
    planner = GoalPlannerV3(ScriptedProvider([ProviderResponse("invalid", "", [])]), lambda: ALL_NAMES)
    planned = planner.plan("只讀取 C:\\Agent\\README.md 的第一個標題，不要修改檔案")
    engine = WorkflowEngine()
    workflow = engine.create(planned.goal, planned.steps)
    engine.approve(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence("step_1", "read_file", "ok", "# YueYue Agent", {"text": "# YueYue Agent"}),
    )

    decision = engine.verify(workflow)

    assert decision.step_satisfied is True
    assert decision.goal_satisfied is True
    assert "YueYue Agent" in str(next(iter(workflow.outputs.values())))


def test_planner_requests_a_required_structured_tool_call() -> None:
    class CapturingProvider:
        def __init__(self):
            self.tool_choice = None
            self.reasoning_effort = None

        def chat(self, messages, tools, model="", tool_choice="auto", reasoning_effort=""):
            self.tool_choice = tool_choice
            self.reasoning_effort = reasoning_effort
            return ProviderResponse("", "", [])

    provider = CapturingProvider()
    GoalPlannerV3(provider, lambda: ALL_NAMES).plan("讀取 README.md")
    assert provider.tool_choice == "required"
    # Planning is a task-path call, so it must request deep reasoning (default high).
    assert provider.reasoning_effort == "high"


def test_planner_normalizes_read_only_model_contract_to_observation() -> None:
    contract = {
        "objective": "讀取 README",
        "risk_level": "low",
        "requested_outputs": [
            {
                "name": "readme_content",
                "description": "README 文字",
                "evidence_kind": "text",
                "required_facts": ["文件已被成功讀取"],
            }
        ],
        "success_criteria": ["取得內容"],
        "steps": [
            {
                "name": "讀取檔案",
                "kind": "act",
                "done_condition": "取得非空文字",
                "allowed_tools": ["read_file"],
                "required_facts": ["文件路徑存在且可讀"],
                "risk_level": "low",
            },
            {
                "name": "驗證內容",
                "kind": "verify",
                "done_condition": "內容非空",
                "allowed_tools": [],
                "required_facts": ["read_file 返回非空內容"],
                "risk_level": "low",
            },
        ],
    }
    provider = ScriptedProvider(
        [ProviderResponse("", "", [{"id": "plan", "name": "submit_goal_contract", "arguments": contract}])]
    )
    planned = GoalPlannerV3(provider, lambda: ALL_NAMES).plan("只讀取 C:\\Agent\\README.md")

    assert planned.steps[0].kind == "observe"
    assert planned.steps[0].allowed_tools == ["read_file"]
    assert planned.steps[0].required_facts == []
    assert planned.goal.requested_outputs[0].required_facts == []


def test_source_mode_without_derivable_sources_yields_a_valid_contract() -> None:
    # Live failure 2026-07-12: a "count Python files" plan set completion_mode="all_sources"
    # on an observe step that used execute_command (not a recognized source tool), so no
    # required_sources could be derived. The contract was stored invalid, replanning
    # reproduced it, and the whole task failed with "all_sources requires required_sources".
    # _parse must normalize this into a valid contract instead.
    contract = {
        "objective": "數一數 C:\\Agent 資料夾裡有多少個 Python 檔案",
        "risk_level": "low",
        "requested_outputs": [
            {"name": "python_file_count", "description": "確切的 .py 數量", "evidence_kind": "value"}
        ],
        "success_criteria": ["回報確切數量"],
        "steps": [
            {
                "name": "執行計數命令",
                "kind": "observe",
                "done_condition": "拿到 Python 檔案數量",
                "allowed_tools": ["execute_command"],
                "required_sources": [],
                "completion_mode": "all_sources",
            },
            {
                "name": "回報數字",
                "kind": "reply",
                "done_condition": "把數量告訴主人",
                "allowed_tools": [],
            },
        ],
    }
    provider = ScriptedProvider(
        [ProviderResponse("", "", [{"id": "plan", "name": "submit_goal_contract", "arguments": contract}])]
    )
    planned = GoalPlannerV3(provider, lambda: ALL_NAMES).plan(contract["objective"])

    assert planned.steps[0].completion_mode == "auto"
    assert WorkflowEngine.contract_issues(planned.steps) == []


def test_real_source_step_is_not_downgraded() -> None:
    # The normalization must only fire when sources genuinely cannot be derived - a step that
    # names a real source tool keeps its all_sources mode and required_sources.
    contract = {
        "objective": "看一下螢幕現在顯示什麼",
        "risk_level": "low",
        "requested_outputs": [
            {"name": "screen_state", "description": "畫面內容", "evidence_kind": "screen_state"}
        ],
        "success_criteria": ["描述畫面"],
        "steps": [
            {
                "name": "看螢幕",
                "kind": "observe",
                "done_condition": "看到畫面",
                "allowed_tools": ["capture_screen", "get_screen_ui"],
                "required_sources": ["capture_screen"],
                "completion_mode": "all_sources",
            },
            {
                "name": "回報",
                "kind": "reply",
                "done_condition": "描述給主人",
                "allowed_tools": [],
            },
        ],
    }
    provider = ScriptedProvider(
        [ProviderResponse("", "", [{"id": "plan", "name": "submit_goal_contract", "arguments": contract}])]
    )
    planned = GoalPlannerV3(provider, lambda: [*ALL_NAMES, "capture_screen"]).plan(contract["objective"])

    assert planned.steps[0].completion_mode == "all_sources"
    assert planned.steps[0].required_sources == ["capture_screen"]
    assert WorkflowEngine.contract_issues(planned.steps) == []


def test_model_narrative_required_facts_are_retained_as_verification_constraints() -> None:
    contract = {
        "objective": "Find Codex and read the five-hour percentage",
        "requested_outputs": [
            {
                "name": "five_hour_percentage",
                "description": "remaining percentage for the five-hour limit",
                "evidence_kind": "value",
                "required_facts": ["The displayed percentage value", "The label confirming five-hour limit"],
            }
        ],
        "success_criteria": ["percentage is visible"],
        "steps": [
            {
                "name": "Find Codex",
                "kind": "observe",
                "done_condition": "Codex appears in the window list",
                "allowed_tools": ["list_windows"],
                "required_facts": ["Exact Codex window title", "Whether it is focused"],
            },
            {
                "name": "Read quota",
                "kind": "verify",
                "done_condition": "five-hour percentage is visible",
                "allowed_tools": ["get_screen_ui"],
                "required_facts": ["The exact displayed percentage"],
            },
        ],
    }
    provider = ScriptedProvider(
        [ProviderResponse("", "", [{"id": "plan", "name": "submit_goal_contract", "arguments": contract}])]
    )
    planned = GoalPlannerV3(provider, lambda: [*ALL_NAMES, "list_windows"]).plan(contract["objective"])

    assert planned.goal.requested_outputs[0].required_facts == [
        "The displayed percentage value",
        "The label confirming five-hour limit",
    ]
    assert planned.steps[0].required_facts == ["Exact Codex window title", "Whether it is focused"]
    assert planned.steps[1].required_facts == ["The exact displayed percentage"]


def test_legacy_string_data_becomes_typed_text_fact() -> None:
    result = _legacy_result(ToolResult("ok", "File read.", data="# YueYue Agent"))
    assert result.facts["text"] == "# YueYue Agent"


def test_model_cannot_broaden_owner_objective_and_first_heading_is_extracted() -> None:
    original = "只讀取 README.md 的第一個標題並告訴我"
    contract = {
        "objective": "讀取 README.md 的完整內容",
        "requested_outputs": [{"name": "readme_content", "description": "README.md 完整內容", "evidence_kind": "text"}],
        "success_criteria": ["取得完整內容"],
        "steps": [
            {
                "name": "讀取",
                "kind": "observe",
                "done_condition": "內容非空",
                "allowed_tools": ["read_file"],
                "required_facts": [],
            },
            {"name": "回報", "kind": "reply", "done_condition": "已回報", "allowed_tools": []},
        ],
    }
    provider = ScriptedProvider(
        [ProviderResponse("", "", [{"id": "plan", "name": "submit_goal_contract", "arguments": contract}])]
    )
    planned = GoalPlannerV3(provider, lambda: ALL_NAMES).plan(original)
    engine = WorkflowEngine()
    workflow = engine.create(planned.goal, planned.steps)
    engine.approve(workflow)
    engine.add_evidence(
        workflow,
        ExecutionEvidence(
            "step_1",
            "read_file",
            "ok",
            "File read.",
            {"text": "# YueYue Agent\n\nLong introduction\n## Setup\nMore text"},
        ),
    )
    decision = engine.verify(workflow)

    assert planned.goal.objective == original
    assert decision.goal_satisfied is True
    assert workflow.outputs["readme_content"] == "# YueYue Agent"


def test_planner_coerces_percentage_output_to_value_evidence() -> None:
    contract = {
        "objective": "Read quota",
        "requested_outputs": [
            {
                "name": "remaining_percentage",
                "description": "remaining percentage for the five-hour limit",
                "evidence_kind": "text",
            }
        ],
        "success_criteria": ["percentage read"],
        "steps": [
            {
                "name": "Observe",
                "kind": "observe",
                "done_condition": "quota is visible",
                "allowed_tools": ["get_screen_ui"],
            },
            {"name": "Report", "kind": "reply", "done_condition": "reported", "allowed_tools": []},
        ],
    }
    provider = ScriptedProvider(
        [ProviderResponse("", "", [{"id": "plan", "name": "submit_goal_contract", "arguments": contract}])]
    )

    planned = GoalPlannerV3(provider, lambda: ALL_NAMES).plan("Read remaining five-hour percentage")

    assert planned.goal.requested_outputs[0].evidence_kind == "value"
