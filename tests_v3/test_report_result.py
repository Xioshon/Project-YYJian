from __future__ import annotations

import tempfile

from yueyue_v3.models import ExecutionEvidence, GoalContract, RequestedOutput, StepContract
from yueyue_v3.observations import ObservationService
from yueyue_v3.tools import ToolCatalogV3
from yueyue_v3.workflow import WorkflowEngine


def _catalog() -> ToolCatalogV3:
    tmp = tempfile.mkdtemp()
    return ToolCatalogV3(ObservationService(state_dir=tmp))


def test_report_result_excluded_from_public_count_but_executable() -> None:
    cat = _catalog()
    # Public/registered tool count invariant must not change - report_result is internal.
    assert ToolCatalogV3.REPORT_RESULT not in cat.names()
    assert len(cat.names()) == 30
    # ...but it is registered, executable, and listable when a step explicitly allows it.
    assert cat.get(ToolCatalogV3.REPORT_RESULT) is not None
    listed = [tool.name for tool in cat.list(["list_files", ToolCatalogV3.REPORT_RESULT])]
    assert ToolCatalogV3.REPORT_RESULT in listed


def test_report_result_records_named_facts() -> None:
    cat = _catalog()
    res = cat.execute(
        ToolCatalogV3.REPORT_RESULT,
        {"results": [{"name": "python_file_count", "value": "22"}]},
    )
    assert res.status == "ok"
    assert res.facts == {"python_file_count": "22"}


def test_report_result_rejects_empty_results() -> None:
    cat = _catalog()
    res = cat.execute(ToolCatalogV3.REPORT_RESULT, {"results": []})
    assert res.status == "error"


def test_derived_value_from_report_result_satisfies_the_goal() -> None:
    # The end-to-end point of the tool: a count task whose answer is not returned by any
    # single tool (list_files gives names, not a count) completes when the model submits the
    # derived value, which the output extractor then finds as a named fact.
    goal = GoalContract(
        objective="數一數 C:\\Agent 裡有多少個 Python 檔案",
        requested_outputs=[
            RequestedOutput("python_file_count", "確切的 .py 數量", True, "value", [])
        ],
        success_criteria=["回報確切數量"],
    )
    steps = [
        StepContract(
            step_id="step_1",
            name="數檔案",
            kind="observe",
            done_condition="拿到數量",
            allowed_tools=["list_files"],
        ),
        StepContract(step_id="step_2", name="回報", kind="reply", done_condition="告訴主人", allowed_tools=[]),
    ]
    engine = WorkflowEngine()
    workflow = engine.create(goal, steps)
    engine.approve(workflow)

    # The model observed the folder (list_files), then derived and submitted the count.
    engine.add_evidence(
        workflow,
        ExecutionEvidence("step_1", "list_files", "ok", "列出檔案", {"files": "a.py b.py ..."}),
    )
    engine.add_evidence(
        workflow,
        ExecutionEvidence("step_1", "report_result", "ok", "Recorded", {"python_file_count": "22"}),
    )

    decision = engine.verify(workflow)
    assert decision.goal_satisfied is True
    assert str(workflow.outputs.get("python_file_count")) == "22"
