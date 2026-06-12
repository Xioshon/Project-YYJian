import re
from dataclasses import dataclass, field

from agent_hooks import emit_trace


@dataclass
class PlanStepSpec:
    name: str
    kind: str = "act"
    observe_policy: str = "standard"
    notes: list[str] = field(default_factory=list)


@dataclass
class PlannerResult:
    objective: str
    steps: list[PlanStepSpec]
    intent: str = "task"
    planner_version: str = "planner_v1"

    def step_names(self) -> list[str]:
        return [step.name for step in self.steps]


class PlannerV1:
    def plan(self, objective: str, intent: str = "task", session_id: str = "", turn_id: int = 0) -> PlannerResult:
        text = re.sub(r"\s+", " ", objective or "").strip()
        lowered = text.casefold()
        steps: list[PlanStepSpec] = []

        if _is_cancel(lowered):
            steps.append(PlanStepSpec("cancel active task", kind="control"))
            result = PlannerResult(text, steps, intent="cancel")
        elif any(marker in lowered for marker in ["測試", "测试", "self_test", "py_compile", "驗證", "验证", "checkonly", "eval"]):
            steps = [
                PlanStepSpec("understand requested verification target", kind="plan"),
                PlanStepSpec("run deterministic verification worker", kind="verify", observe_policy="deterministic"),
                PlanStepSpec("assimilate verification evidence into task graph", kind="verify", observe_policy="deterministic"),
                PlanStepSpec("report pass/fail and next action", kind="reply"),
            ]
            result = PlannerResult(text, steps, intent=intent)
        elif any(marker in lowered for marker in ["打開", "打开", "點", "点击", "browser", "chrome", "whatsapp", "youtube", "電腦", "电脑", "screenshot", "截圖", "截图"]):
            steps = [
                PlanStepSpec("plan safe UI/computer action", kind="plan"),
                PlanStepSpec("perform requested UI action with permission", kind="act", observe_policy="observe_required"),
                PlanStepSpec("observe screen or tool evidence", kind="observe", observe_policy="observe_required"),
                PlanStepSpec("verify the visible result before continuing", kind="verify", observe_policy="observe_required"),
                PlanStepSpec("report outcome clearly", kind="reply"),
            ]
            result = PlannerResult(text, steps, intent=intent)
        elif any(marker in lowered for marker in ["修", "bug", "實作", "实现", "implement", "優化", "优化", "代码", "程式碼", "code"]):
            steps = [
                PlanStepSpec("inspect relevant code and current state", kind="plan"),
                PlanStepSpec("apply minimal safe code changes", kind="act"),
                PlanStepSpec("run deterministic regression checks", kind="verify", observe_policy="deterministic"),
                PlanStepSpec("assimilate verification evidence", kind="verify", observe_policy="deterministic"),
                PlanStepSpec("summarize changes, risks, and next step", kind="reply"),
            ]
            result = PlannerResult(text, steps, intent=intent)
        else:
            steps = [
                PlanStepSpec("clarify objective from owner message", kind="plan"),
                PlanStepSpec("perform necessary safe action", kind="act"),
                PlanStepSpec("verify result if any tool was used", kind="verify"),
                PlanStepSpec("reply with concise outcome", kind="reply"),
            ]
            result = PlannerResult(text, steps, intent=intent)

        emit_trace("planner.result", session_id=session_id, turn_id=turn_id, objective=text[:160], intent=result.intent, step_count=len(result.steps), planner_version=result.planner_version)
        return result


def _is_cancel(text: str) -> bool:
    return any(marker in text for marker in ["算了", "停止", "取消", "stop", "cancel", "別做", "不要做"])


DEFAULT_PLANNER = PlannerV1()
