import re
from dataclasses import asdict, dataclass, field
from typing import Any

from agent_hooks import emit_trace


PLANNER_VERSION = "planner_v1_structured"
UNICODE_INTENT_MARKERS = ("\u4fee", "\u6e2c\u8a66", "\u622a\u5716", "\u7e7c\u7e8c")


@dataclass
class PlanStepSpec:
    name: str
    kind: str = "act"
    observe_policy: str = "standard"
    allowed_tools: list[str] = field(default_factory=list)
    verification_policy: str = "optional"
    risk_level: str = "low"
    done_condition: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlannerResult:
    objective: str
    steps: list[PlanStepSpec]
    intent: str = "task"
    planner_version: str = PLANNER_VERSION

    def step_names(self) -> list[str]:
        return [step.name for step in self.steps]

    def step_specs(self) -> list[dict[str, Any]]:
        return [step.to_dict() for step in self.steps]


class PlannerV1:
    def plan(self, objective: str, intent: str = "task", session_id: str = "", turn_id: int = 0) -> PlannerResult:
        text = re.sub(r"\s+", " ", objective or "").strip()
        lowered = text.casefold()

        if _is_cancel(lowered):
            result = PlannerResult(text, [_step("cancel active task", "control", risk="low", done="active workflow is cancelled")], intent="cancel")
        elif intent == "screen_observe" or _looks_like_screen_observe(lowered):
            result = PlannerResult(
                text,
                [
                    _step("observe current screen once", "observe", tools=["get_screen_ui", "execute_python"], observe="observe_required", verify="observe", risk="low", done="one screen observation artifact or UI summary exists"),
                    _step("summarize visible state for owner", "reply", tools=[], observe="observe_required", verify="owner_visible", risk="low", done="owner receives one concise screen summary"),
                ],
                intent=intent,
            )
        elif _looks_like_ui_action(lowered):
            result = PlannerResult(
                text,
                [
                    _step("identify target app, screen, and requested path", "plan", tools=["get_screen_ui", "search_knowledge"], observe="observe_required", verify="observe", risk="medium", done="target app/window/action path is identified"),
                    _step("click visible target window or taskbar icon when possible", "act", tools=["click_ui_element", "press_hotkey", "type_keyboard"], observe="observe_required", verify="observe", risk="guarded", done="target window is activated directly, with hotkey only as fallback"),
                    _step("open the requested menu or control", "act", tools=["click_ui_element", "press_hotkey", "type_keyboard"], observe="observe_required", verify="observe", risk="guarded", done="requested control/menu is reached or a blocker is captured"),
                    _step("observe the visible result", "observe", tools=["get_screen_ui"], observe="observe_required", verify="observe", risk="low", done="visible result is observed once"),
                    _step("report the requested value or blocker clearly", "reply", tools=[], verify="owner_visible", done="owner receives the result without internal policy jargon"),
                ],
                intent=intent,
            )
        elif _looks_like_code_task(lowered):
            result = PlannerResult(
                text,
                [
                    _step("inspect relevant code and current state", "plan", tools=["search_knowledge", "read_file", "search_in_files", "list_files"], verify="none", risk="low", done="relevant files and current failure are understood"),
                    _step("apply minimal safe code changes", "act", tools=["write_file"], verify="none", risk="medium", done="only necessary project files are changed"),
                    _step("run deterministic regression checks", "verify", tools=["execute_command"], observe="deterministic", verify="deterministic", risk="low", done="py_compile/self_test/agent_eval evidence is captured"),
                    _step("assimilate verification evidence", "verify", tools=[], observe="deterministic", verify="deterministic", risk="low", done="verification evidence is attached to the workflow"),
                    _step("summarize changes, risks, and next step", "reply", tools=[], verify="owner_visible", done="owner receives concise summary"),
                ],
                intent=intent,
            )
        elif _looks_like_verification(lowered):
            result = PlannerResult(
                text,
                [
                    _step("identify verification target", "plan", tools=["search_knowledge", "read_file"], verify="none", done="target files or checks are known"),
                    _step("run deterministic verification", "verify", tools=["execute_command"], observe="deterministic", verify="deterministic", risk="low", done="py_compile/self_test/agent_eval result is captured"),
                    _step("assimilate verification evidence", "verify", tools=[], observe="deterministic", verify="deterministic", risk="low", done="TaskGraph and SessionBrain record pass/fail evidence"),
                    _step("report pass/fail and next action", "reply", tools=[], verify="owner_visible", done="owner sees clear outcome and next step"),
                ],
                intent=intent,
            )
        else:
            result = PlannerResult(
                text,
                [
                    _step("clarify objective from owner message", "plan", tools=["search_knowledge"], verify="none", done="objective is short and actionable"),
                    _step("perform necessary safe action", "act", tools=["search_knowledge", "read_file", "list_files"], verify="optional", risk="low", done="requested safe action has a result"),
                    _step("verify result if any tool was used", "verify", tools=["execute_command"], observe="deterministic", verify="optional", risk="low", done="tool result is checked when applicable"),
                    _step("reply with concise outcome", "reply", tools=[], verify="owner_visible", done="owner receives a clear answer"),
                ],
                intent=intent,
            )

        emit_trace(
            "planner.result",
            session_id=session_id,
            turn_id=turn_id,
            objective=text[:160],
            intent=result.intent,
            step_count=len(result.steps),
            planner_version=result.planner_version,
        )
        return result


def _step(
    name: str,
    kind: str,
    tools: list[str] | None = None,
    observe: str = "standard",
    verify: str = "optional",
    risk: str = "low",
    done: str = "",
    notes: list[str] | None = None,
) -> PlanStepSpec:
    return PlanStepSpec(
        name=name,
        kind=kind,
        observe_policy=observe,
        allowed_tools=tools or [],
        verification_policy=verify,
        risk_level=risk,
        done_condition=done,
        notes=notes or [],
    )


def _is_cancel(text: str) -> bool:
    return any(marker in text for marker in ["算了", "停止", "取消", "不用做了", "別做", "不要做", "stop", "cancel"])


def _looks_like_verification(text: str) -> bool:
    return any(marker in text for marker in ["測試", "验证", "驗證", "確認", "检查", "檢查", "self_test", "py_compile", "checkonly", "agent_eval", "eval"])


def _looks_like_screen_observe(text: str) -> bool:
    return any(marker in text for marker in ["截圖", "截图", "看螢幕", "看屏幕", "看看狀態", "看看状态", "畫面", "画面", "screenshot", "screen"])


def _looks_like_ui_action(text: str) -> bool:
    clean_markers = [
        "\u6253\u5f00",
        "\u6253\u958b",
        "\u70b9\u51fb",
        "\u9ede\u64ca",
        "\u5bfc\u822a",
        "\u5c0e\u822a",
        "\u8bbe\u7f6e",
        "\u8a2d\u5b9a",
        "\u83dc\u5355",
        "\u83dc\u55ae",
        "\u4e8c\u7ea7\u83dc\u5355",
        "\u4e8c\u7d1a\u83dc\u55ae",
        "\u5269\u4f59\u7528\u91cf",
        "\u5269\u9918\u7528\u91cf",
        "\u7a97\u53e3",
        "\u8996\u7a97",
        "\u6309",
        "\u95dc\u9589",
        "\u5173\u95ed",
        "bluetooth",
        "audio receiver",
        "close connection",
        "open connection",
        "connected",
        "disconnected",
    ]
    if any(marker in text for marker in clean_markers):
        return True
    unicode_markers = [
        "打开",
        "打開",
        "点击",
        "點擊",
        "导航",
        "導航",
        "设置",
        "設定",
        "菜单",
        "菜單",
        "二级菜单",
        "二級菜單",
        "剩余用量",
        "剩餘用量",
        "codex",
        "窗口",
        "視窗",
        "app",
        "alt+tab",
    ]
    if any(marker in text for marker in unicode_markers):
        return True
    return any(
        marker in text
        for marker in [
            "打開",
            "打开",
            "點擊",
            "点击",
            "電腦",
            "电脑",
            "瀏覽器",
            "浏览器",
            "控制",
            "browser",
            "chrome",
            "whatsapp",
            "youtube",
        ]
    )


def _looks_like_code_task(text: str) -> bool:
    unicode_markers = ["修", "修復", "修复", "bug", "代碼", "代码", "程式", "程序", "重構", "重构", "優化", "优化", "實作", "实现", "檔案", "文件"]
    if any(marker in text for marker in unicode_markers):
        return True
    return any(
        marker in text
        for marker in [
            "修",
            "bug",
            "寫",
            "写",
            "實作",
            "实现",
            "實現",
            "implement",
            "優化",
            "优化",
            "代碼",
            "代码",
            "程式",
            "程序",
            "重構",
            "重构",
            "code",
        ]
    )


DEFAULT_PLANNER = PlannerV1()
