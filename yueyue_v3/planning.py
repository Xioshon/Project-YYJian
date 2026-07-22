from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core_tools import AgentTool, ToolResult, env_value

from .models import GoalContract, RequestedOutput, StepContract


def _task_reasoning_effort() -> str:
    """Planning is a task-path call, so it uses the same reasoning depth as execution /
    verification (see runtime._task_reasoning_effort). Default "high"."""
    raw = env_value("YUEYUE_TASK_REASONING_EFFORT")
    return raw.strip() if raw.strip() else "high"

DESKTOP_TOOLS = {
    "list_windows",
    "focus_window",
    "get_screen_ui",
    "capture_screen",
    "analyze_media",
    "click_ui_element",
    "click_screen",
    "press_hotkey",
    "type_keyboard",
}
FILE_READ_TOOLS = {"read_file", "list_files", "search_in_files"}
CANONICAL_FACT_KEYS = {
    "window_id",
    "window_title",
    "snapshot_id",
    "revision",
    "visible_text",
    "text",
    "status",
    "percentage",
    "ui_elements",
    "windows",
    "returncode",
    "stdout",
    "stderr",
}


# Social-layer tools that never belong in a task workflow's step plan - stickers and message
# reactions are the social flow's job. Live gap-battery flake 2026-07-15: a count-files task
# wandered into react_to_message (no real message existed) and blocked on the error.
_SOCIAL_SIDE_TOOLS = {"react_to_message", "search_sticker"}


def _environment_facts() -> str:
    """Known local paths + tool boundaries for the planner. Plan the owner's actual mutation -
    never a step that merely discovers a path we already know."""
    import os as _os
    from pathlib import Path as _Path

    home = _Path(_os.path.expanduser("~"))
    root = _os.path.abspath(_os.getenv("YUEYUE_ROOT_DIR") or _os.path.dirname(_os.path.dirname(__file__)))
    return (
        "\nKnown environment (do NOT plan a step just to discover these - they are given): "
        f"home={home}; downloads={home / 'Downloads'}; desktop={home / 'Desktop'}; "
        f"documents={home / 'Documents'}; project_root={root}; workspace={_Path(root) / 'workspace'}. "
        "To create or overwrite ANY file (inside the workspace or at an absolute path like the "
        "downloads folder or desktop) plan write_file with the absolute path - it is direct and "
        "cannot be broken by shell quoting. Do NOT plan echo/redirect shell commands to write "
        "files. execute_command is for running programs, not for authoring text files. "
        "requested_outputs must be the owner's actual deliverable (the created file's content), "
        "never an intermediate like a path you were told."
    )

# ROADMAP P3: golden plan shapes for the most common task families. Gap-battery evidence: with a
# bare prompt the SAME task got a good plan one run and a shallow/one-step plan the next
# (write-append-verify planned as a single step; a count task planned around the answer instead of
# deriving it). Few-shot shapes anchor the stochasticity at near-zero token cost.
_GOLDEN_PLAN_EXAMPLES = (
    "\n\nGolden plan shapes (adapt names/tools, keep the SHAPE):\n"
    "1. Count/derive from observation ('數一下X有幾個'): step1 observe (execute_command or "
    "list_files) -> the answer appears in stdout/results; requested_output = the number (value). "
    "Do NOT plan extra verification commands - report_result submits the observed number.\n"
    "2. Create/modify then confirm ('建立檔案寫step1，再加一行step2，讀回確認'): one act step PER "
    "mutation using the user's EXACT literal content and filename (write the user's 'step1'; append "
    "the user's 'step2' - never substitute invented placeholder content), then one observe step "
    "reading the final state; requested_output = the final content read back (text).\n"
    "3. Find which file contains X: step1 observe with search_in_files(keyword=X); "
    "requested_output = the matching filename(s) (text).\n"
    "4. Look up an environment value ('查Python版本'): step1 observe with execute_command running "
    "the exact command; requested_output = the value from stdout (text).\n"
    "5. Screen question ('屏幕上是什麼'): step1 observe capture_screen + analyze_media; "
    "requested_output = screen_state with required_facts."
)


@dataclass
class PlannedWorkflow:
    goal: GoalContract
    steps: list[StepContract]
    # Additional INDEPENDENT tasks found in the same message, to be queued and run one by one.
    # Folded into planning (2026-07-22) because a separate splitter model call cost ~15s on EVERY
    # task turn - the planner already reads the whole message, so this field is free.
    deferred_objectives: list[str] = field(default_factory=list)


class GoalPlannerV3:
    def __init__(self, provider: Any, tool_names: Callable[[], list[str]]):
        self.provider = provider
        self.tool_names = tool_names

    def plan(self, objective: str) -> PlannedWorkflow:
        return self._plan(objective, [])

    def replan(self, objective: str, issues: list[str]) -> PlannedWorkflow:
        return self._plan(objective, issues)

    def _plan(self, objective: str, contract_issues: list[str]) -> PlannedWorkflow:
        tool_names = self.tool_names()
        if _desktop_task(objective):
            allowed_domain = sorted(DESKTOP_TOOLS & set(tool_names))
        elif _file_read_task(objective):
            allowed_domain = sorted(FILE_READ_TOOLS & set(tool_names))
        else:
            allowed_domain = [name for name in tool_names if name not in _SOCIAL_SIDE_TOOLS]
        submit = AgentTool(
            "submit_goal_contract",
            "Submit a structured goal and executable steps without performing any action.",
            lambda **_: ToolResult("ok", "accepted"),
            {
                "type": "object",
                "properties": {
                    "objective": {"type": "string"},
                    "risk_level": {"type": "string", "enum": ["low", "guarded", "high"]},
                    "requested_outputs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "required": {"type": "boolean"},
                                "evidence_kind": {
                                    "type": "string",
                                    "enum": ["value", "screen_state", "artifact", "status", "text"],
                                },
                                "required_facts": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["name", "description"],
                        },
                    },
                    "success_criteria": {"type": "array", "items": {"type": "string"}},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "kind": {"type": "string", "enum": ["observe", "act", "verify", "reply"]},
                                "done_condition": {"type": "string"},
                                "allowed_tools": {"type": "array", "items": {"type": "string"}},
                                "required_facts": {"type": "array", "items": {"type": "string"}},
                                "required_sources": {"type": "array", "items": {"type": "string"}},
                                "completion_mode": {
                                    "type": "string",
                                    "enum": ["all_sources", "any_source", "facts", "semantic"],
                                },
                                "risk_level": {"type": "string", "enum": ["low", "guarded", "high"]},
                            },
                            "required": ["name", "kind", "done_condition", "allowed_tools"],
                        },
                    },
                    "deferred_objectives": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Additional INDEPENDENT tasks the user packed into the same message, "
                            "each a complete standalone instruction. Sequential steps of ONE task "
                            "do not belong here. Empty for a single-task message."
                        ),
                    },
                },
                "required": ["objective", "requested_outputs", "success_criteria", "steps"],
            },
            False,
        )
        system = (
            "Create one submit_goal_contract call. Plan from the concrete outcome, not keywords. "
            "If the message packs several INDEPENDENT tasks (建 A 檔，再建 B 檔 / 數檔案，順便查天氣), "
            "plan ONLY the first and list the rest verbatim in deferred_objectives so they run one by one. "
            "Sequential steps of a single task (建檔然後寫入) are NOT independent - keep them as steps. "
            "Separate observation and action steps. Every observation step must declare required_sources and a "
            "completion_mode. Use all_sources when each listed source is required, any_source when one sufficient "
            "observation is enough, facts when required_facts prove completion, and semantic only for genuinely "
            "ambiguous visual meaning. done_condition is descriptive and is not the primary machine predicate. "
            "An action step must include the action tool and observation tools for post-action verification. "
            "A screenshot, click, or command success is evidence, not the goal. "
            "requested_outputs must capture the user's FINAL deliverable, never an intermediate "
            "acknowledgment: if the user asks to create/modify something and confirm its final state, "
            "the output is that final state (e.g. the file's final content read back), not 'file "
            "written'. When the request has multiple numbered parts, every part needs its own step "
            "and the goal is only complete once the last part is verified. "
            "To locate which file contains a text, prefer search_in_files over reading files one "
            "by one. Use only these tools: "
            + ", ".join(allowed_domain)
            + _GOLDEN_PLAN_EXAMPLES
        )
        # The planner must know the machine's real paths and tool boundaries, or it plans a
        # path-DISCOVERY goal (live 2026-07-21: requested_output became "downloads_path" and the
        # file was never created) instead of planning the mutation the owner actually asked for.
        system += _environment_facts()
        if contract_issues:
            system += (
                " The previous contract was rejected for these structural reasons: "
                + "; ".join(str(item) for item in contract_issues[:8])
                + ". Return a corrected contract, not the same invalid structure."
            )
        try:
            response = self.provider.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": objective}],
                [submit],
                tool_choice="required",
                reasoning_effort=_task_reasoning_effort(),
            )
            call = next((item for item in response.tool_calls if item.get("name") == submit.name), None)
            planned = self._parse(call.get("arguments") if call else None, objective, allowed_domain)
            if planned:
                return planned
        except Exception:
            pass
        return self._fallback(objective, allowed_domain)

    def _parse(self, raw: Any, objective: str, allowed_domain: list[str]) -> PlannedWorkflow | None:
        if not isinstance(raw, dict):
            return None
        outputs = []
        for item in raw.get("requested_outputs") or []:
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                continue
            evidence_kind = str(item.get("evidence_kind") or "value")[:40]
            output_hint = f"{item.get('name', '')} {item.get('description', '')}".casefold()
            if any(marker in output_hint for marker in ("percentage", "percent", "%", "quota", "remaining")):
                evidence_kind = "value"
            required_facts = _verification_facts(item.get("required_facts") or [])
            if evidence_kind in {"text", "artifact"}:
                required_facts = []
            outputs.append(
                RequestedOutput(
                    str(item.get("name") or "")[:100],
                    str(item.get("description") or "")[:400],
                    bool(item.get("required", True)),
                    evidence_kind,
                    required_facts,
                )
            )
        allowed = set(allowed_domain)
        steps = []
        for index, item in enumerate(raw.get("steps") or [], start=1):
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                continue
            tools = [str(name) for name in item.get("allowed_tools") or [] if str(name) in allowed]
            kind = str(item.get("kind") or "act")
            required_facts = _verification_facts(item.get("required_facts") or [])
            required_sources = [
                str(name)
                for name in item.get("required_sources") or []
                if str(name) in tools and str(name) in DESKTOP_TOOLS | FILE_READ_TOOLS
            ]
            completion_mode = str(item.get("completion_mode") or "auto")
            if completion_mode not in {"all_sources", "any_source", "facts", "semantic"}:
                completion_mode = "auto"
            if kind == "act" and tools and set(tools) <= FILE_READ_TOOLS:
                kind = "observe"
                required_facts = []
            if kind == "observe" and completion_mode in {"all_sources", "any_source"} and not required_sources:
                required_sources = [name for name in tools if name in DESKTOP_TOOLS | FILE_READ_TOOLS]
            # Normalize so the stored contract is always valid: a source/facts-based completion
            # mode whose prerequisites can't be met would be rejected by
            # WorkflowEngine.contract_issues, silently failing the whole task (observed
            # 2026-07-12 on a "count Python files" task where the model set all_sources but used
            # execute_command, which is not a recognized source tool, so no required_sources
            # could be derived and replanning kept reproducing the same invalid structure).
            # _parse is the boundary between untrusted model output and the validated internal
            # contract, so it - not the model, and not a retry that may loop - guarantees validity.
            if completion_mode in {"all_sources", "any_source"} and not required_sources:
                completion_mode = "auto"
            if completion_mode == "facts" and not required_facts:
                completion_mode = "auto"
            if not tools and kind in {"act", "observe"}:
                # The model listed only tools outside the validated domain (hallucinated names or
                # social-layer tools), which all got filtered above. An act/observe step with zero
                # tools is a guaranteed dead-end ("No safe capability is available", observed live
                # 2026-07-15 on a search task). Backfill the safe read-only set and demote to
                # observe - read tools cannot mutate anything, and the execution loop can still
                # finish via report_result once evidence exists. "reply" steps legitimately carry
                # no tools and are left alone.
                backfill = sorted(FILE_READ_TOOLS & set(allowed)) or sorted(
                    {"get_screen_ui", "capture_screen"} & set(allowed)
                )
                if backfill:
                    tools = backfill
                    kind = "observe"
                    required_facts = []
            # Post-action screenshot verification belongs ONLY to genuine desktop/UI actions
            # (click/type/hotkey). Appending capture_screen to EVERY act step made a plain
            # file-write task screenshot the desktop and then stall on "No state change observed"
            # (live 2026-07-20: 「幫我新增 Hello.txt」 took a screenshot instead of writing).
            # File/command acts verify by reading the file back or the command's own output.
            if kind == "act" and set(tools) & DESKTOP_TOOLS:
                tools = list(
                    dict.fromkeys(tools + [name for name in ("get_screen_ui", "capture_screen") if name in allowed])
                )
            steps.append(
                StepContract(
                    step_id=f"step_{index}",
                    name=str(item.get("name"))[:180],
                    kind=kind,
                    done_condition=str(item.get("done_condition") or "")[:400],
                    allowed_tools=tools[:14],
                    required_facts=required_facts,
                    risk_level=str(item.get("risk_level") or "low"),
                    required_sources=required_sources[:8],
                    completion_mode=completion_mode,
                )
            )
        criteria = [str(item)[:400] for item in raw.get("success_criteria") or [] if str(item).strip()]
        if not outputs or not steps or not criteria:
            return None
        # A single-step plan is valid for a pure LOOKUP (observe/reply): the observation IS the
        # deliverable and report_result submits it - exactly golden example 1 ("count/list from one
        # observation, do NOT plan extra verification"). The old `len(steps) < 2` guard rejected
        # every such plan, so a plain 「列出下載夾今天編輯過的檔案」 fell back to the generic 4-step
        # scaffold and then blocked despite producing the right answer (live 2026-07-23). A lone
        # MUTATION (act) still needs its own verification step, so that case still falls back.
        if len(steps) == 1 and steps[0].kind == "act":
            return None
        deferred = [
            str(item).strip()[:500]
            for item in (raw.get("deferred_objectives") or [])
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]
        # Normally the goal keeps the owner's VERBATIM words (a model-rewritten objective drifts).
        # The one exception is a split message: the plan covers only the first task, so the goal
        # must say so too - otherwise verification would demand evidence for the siblings that
        # were queued for later and never complete.
        stated = str(raw.get("objective") or "").strip()
        goal = GoalContract(
            (stated or str(objective))[:700] if deferred else str(objective)[:700],
            outputs[:8],
            criteria[:10],
            str(raw.get("risk_level") or "low"),
        )
        return PlannedWorkflow(goal, steps[:10], deferred[:4])

    def _fallback(self, objective: str, allowed_domain: list[str]) -> PlannedWorkflow:
        desktop = _desktop_task(objective)
        file_read = _file_read_task(objective)
        if file_read:
            output = RequestedOutput(
                "requested_result",
                "The concrete text requested by the owner",
                evidence_kind="text",
            )
            steps = [
                StepContract(
                    "step_1",
                    "Read the requested local information",
                    "observe",
                    "The requested text is present in read-only evidence",
                    [name for name in ("read_file", "list_files", "search_in_files") if name in allowed_domain],
                ),
                StepContract(
                    "step_2",
                    "Report the verified text",
                    "reply",
                    "The owner receives the evidence-backed text",
                    [],
                ),
            ]
        elif desktop:
            output = RequestedOutput("requested_result", "The concrete value or state requested by the owner")
            steps = [
                StepContract(
                    "step_1",
                    "Observe the target window",
                    "observe",
                    "The relevant window and actionable controls are visible",
                    [
                        name
                        for name in ("list_windows", "focus_window", "get_screen_ui", "capture_screen", "analyze_media")
                        if name in allowed_domain
                    ],
                    [],
                ),
                StepContract(
                    "step_2",
                    "Perform the minimum required UI action",
                    "act",
                    "The requested screen or control is visible after the action",
                    [
                        name
                        for name in (
                            "click_ui_element",
                            "click_screen",
                            "press_hotkey",
                            "type_keyboard",
                            "get_screen_ui",
                            "capture_screen",
                            "analyze_media",
                        )
                        if name in allowed_domain
                    ],
                    [],
                    "guarded",
                ),
                StepContract(
                    "step_3",
                    "Verify the requested result",
                    "verify",
                    "The concrete requested output is present in structured evidence",
                    [name for name in ("get_screen_ui", "capture_screen", "analyze_media") if name in allowed_domain],
                    [],
                ),
                StepContract(
                    "step_4", "Report the verified result", "reply", "The owner receives the evidence-backed result", []
                ),
            ]
        else:
            output = RequestedOutput("requested_result", "The concrete result requested by the owner")
            steps = [
                StepContract(
                    "step_1",
                    "Gather evidence",
                    "observe",
                    "Enough evidence exists to choose the next safe action",
                    allowed_domain[:20],
                ),
                StepContract(
                    "step_2",
                    "Perform the task",
                    "act",
                    "The requested change or result is directly observed",
                    allowed_domain[:20],
                    risk_level="guarded",
                ),
                StepContract(
                    "step_3", "Verify the result", "verify", "The requested output is present", allowed_domain[:20]
                ),
                StepContract("step_4", "Report the result", "reply", "The owner receives the verified result", []),
            ]
        return PlannedWorkflow(
            GoalContract(
                objective[:700],
                [output],
                ["The requested result is directly evidenced", "No intermediate action is treated as completion"],
                "guarded",
            ),
            steps,
        )


def _desktop_task(text: str) -> bool:
    value = str(text or "").casefold()
    return any(
        marker in value
        for marker in (
            "电脑",
            "電腦",
            "屏幕",
            "螢幕",
            "窗口",
            "視窗",
            "设置",
            "設定",
            "菜单",
            "菜單",
            "codex",
            "点击",
            "點擊",
        )
    )


def _file_read_task(text: str) -> bool:
    value = str(text or "").casefold()
    read_markers = ("讀取", "读取", "只讀", "只读", "查看檔案", "查看文件", "read file", "list files")
    path_markers = (".md", ".txt", ".json", ".py", "c:\\", "/")
    file_markers = ("readme", "workspace", "檔案", "文件", "file")
    mutation_markers = ("修改", "寫入", "写入", "刪除", "删除", "覆蓋", "覆盖", "write", "delete")
    positive_intent = value
    for negative in ("不要", "不可", "不准", "別", "别", "禁止", "without"):
        for mutation in mutation_markers:
            positive_intent = positive_intent.replace(negative + mutation, "")
    return (
        any(marker in value for marker in read_markers)
        and (any(marker in value for marker in path_markers) or any(marker in value for marker in file_markers))
        and not any(marker in positive_intent for marker in mutation_markers)
    )


def _verification_facts(values: list[Any]) -> list[str]:
    facts: list[str] = []
    for value in values:
        fact = str(value or "").strip()[:160]
        if fact:
            facts.append(fact)
    return facts[:8]
