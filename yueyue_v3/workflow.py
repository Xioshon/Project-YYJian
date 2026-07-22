from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any

from .models import (
    ExecutionEvidence,
    GoalContract,
    StepContract,
    StepStatus,
    VerificationDecision,
    WorkflowState,
    WorkflowStatus,
)

OBSERVATION_SOURCES = {
    "get_screen_ui",
    "capture_screen",
    "analyze_media",
    "list_windows",
    "read_file",
    "list_files",
    "search_in_files",
    "read_webpage",
    "read_url_context",
}
# Tools whose successful execution IS the action of an act step. This was desktop-only, so an act
# step driven by execute_command/write_file could never satisfy `_verify_step` ("The planned action
# has not succeeded yet") no matter how many times it succeeded - live 2026-07-21 a Hello.txt was
# actually created on the first try, then retried for 7 minutes and finally mis-reported as a
# permission failure. File/command mutations are actions too.
ACTION_SOURCES = {
    "focus_window", "click_ui_element", "click_screen", "press_hotkey", "type_keyboard",
    "execute_command", "execute_python", "execute_async_command",
    "write_file", "delete_file", "download_file",
}
# Desktop/UI actions only - semantic re-verification of a screenshot makes sense for these, but
# demanding it after a file write would stall a perfectly finished task.
UI_ACTION_SOURCES = {"focus_window", "click_ui_element", "click_screen", "press_hotkey", "type_keyboard"}
# Tools that change persistent state the owner asked for. A pending act step using one of these
# holds the goal open (see verify()); UI-navigation acts are deliberately excluded.
MUTATION_TOOLS = {
    "write_file",
    "delete_file",
    "download_file",
    "execute_command",
    "execute_python",
    "execute_async_command",
}


class WorkflowEngine:
    """Durable goal reducer with deterministic verification before LLM review."""

    def __init__(self, require_semantic_actions: bool = False):
        self.require_semantic_actions = bool(require_semantic_actions)

    def create(self, goal: GoalContract, steps: list[StepContract]) -> WorkflowState:
        return WorkflowState(workflow_id=f"wf_{int(time.time())}_{uuid.uuid4().hex[:8]}", goal=goal, steps=steps)

    @staticmethod
    def contract_issues(steps: list[StepContract]) -> list[str]:
        issues: list[str] = []
        for step in steps:
            tools = set(step.allowed_tools)
            required_sources = set(step.required_sources)
            mode = _completion_mode(step)
            if step.kind != "reply" and not tools:
                issues.append(f"{step.step_id}: no allowed tools")
            if required_sources - tools:
                missing = ",".join(sorted(required_sources - tools))
                issues.append(f"{step.step_id}: required sources are not allowed: {missing}")
            if step.kind == "observe" and mode == "all_sources" and not required_sources:
                issues.append(f"{step.step_id}: all_sources requires required_sources")
            if step.kind == "observe" and mode == "facts" and not step.required_facts:
                issues.append(f"{step.step_id}: facts completion requires required_facts")
            if step.kind in {"act", "verify"} and not step.done_condition.strip():
                issues.append(f"{step.step_id}: {step.kind} steps require a non-empty done_condition")
            if (
                step.kind == "observe"
                and mode not in {"all_sources", "any_source", "facts"}
                and not step.done_condition.strip()
            ):
                issues.append(f"{step.step_id}: observe steps need required_sources/required_facts or a non-empty done_condition")
        return issues

    def approve(self, workflow: WorkflowState) -> WorkflowState:
        if workflow.status in {WorkflowStatus.PLANNED, WorkflowStatus.AWAITING_PERMISSION}:
            workflow.status = WorkflowStatus.RUNNING
            step = workflow.current_step()
            if step and step.status in {StepStatus.PLANNED, StepStatus.AWAITING_PERMISSION}:
                step.status = StepStatus.RUNNING
        workflow.updated_at = time.time()
        return workflow

    def add_evidence(self, workflow: WorkflowState, evidence: ExecutionEvidence) -> WorkflowState:
        if any(item.evidence_id == evidence.evidence_id for item in workflow.evidence):
            return workflow
        workflow.evidence.append(evidence)
        workflow.evidence = workflow.evidence[-200:]
        step = workflow.current_step()
        if step:
            step.evidence_ids.append(evidence.evidence_id)
            step.evidence_ids = step.evidence_ids[-40:]
            if evidence.source in ACTION_SOURCES and evidence.status == "ok":
                step.last_action_event_id = evidence.evidence_id
                step.status = StepStatus.AWAITING_OBSERVATION
            elif evidence.status == "error":
                step.attempts += 1
        workflow.updated_at = time.time()
        return workflow

    def verify(self, workflow: WorkflowState) -> VerificationDecision:
        step = workflow.current_step()
        step_evidence = self._step_evidence(workflow, step) if step else []
        step_satisfied, step_reason = self._verify_step(step, step_evidence)
        if step and step_satisfied:
            step.status = StepStatus.VERIFIED
            if workflow.current_step_index < len(workflow.steps) - 1:
                workflow.current_step_index += 1
                next_step = workflow.current_step()
                if next_step and next_step.status == StepStatus.PLANNED:
                    next_step.status = StepStatus.RUNNING
        elif step and step.kind == "act" and step.status == StepStatus.AWAITING_OBSERVATION:
            successful = [item for item in step_evidence if item.status == "ok"]
            action_indexes = [index for index, item in enumerate(successful) if item.source in ACTION_SOURCES]
            if action_indexes and any(
                item.source in OBSERVATION_SOURCES for item in successful[action_indexes[-1] + 1 :]
            ):
                step.status = StepStatus.RUNNING

        outputs, missing = self._extract_outputs(workflow.goal, workflow.evidence)
        workflow.outputs.update(outputs)
        # An output binding alone must not complete the goal while MUTATION steps the owner asked
        # for are still pending - gap-battery evidence 2026-07-15: a write-append-verify task bound
        # the read-back of the FIRST write ("step1") as the final content and declared success
        # without ever appending step2. Only mutation acts block completion: observation-enabling
        # acts (focus_window before reading a percentage) may legitimately become unnecessary once
        # the requested value is already in evidence, and forcing them would replay UI side effects.
        mutations_pending = any(
            item.kind == "act"
            and set(item.allowed_tools) & MUTATION_TOOLS
            and item.status
            in {
                StepStatus.PLANNED,
                StepStatus.RUNNING,
                StepStatus.AWAITING_PERMISSION,
                StepStatus.AWAITING_OBSERVATION,
            }
            for item in workflow.steps
        )
        goal_satisfied = bool(workflow.goal.requested_outputs) and not missing and not mutations_pending
        decision = VerificationDecision(
            action_succeeded=any(item.status == "ok" for item in workflow.evidence),
            step_satisfied=step_satisfied,
            goal_satisfied=goal_satisfied,
            reason="All required outputs are supported by structured evidence." if goal_satisfied else step_reason,
            missing_outputs=missing,
            extracted_outputs=outputs,
            confidence=0.98 if goal_satisfied else (0.9 if step_satisfied else 0.35),
        )
        workflow.verification = decision
        if goal_satisfied:
            workflow.status = WorkflowStatus.COMPLETED
            for item in workflow.steps:
                if item.status not in {StepStatus.FAILED, StepStatus.BLOCKED}:
                    item.status = StepStatus.VERIFIED
        elif workflow.status not in {
            WorkflowStatus.AWAITING_PERMISSION,
            WorkflowStatus.BLOCKED,
            WorkflowStatus.CANCELLED,
            WorkflowStatus.COMPLETED,
        }:
            workflow.status = WorkflowStatus.RUNNING
        workflow.updated_at = time.time()
        return decision

    def allowed_tools(self, workflow: WorkflowState) -> list[str]:
        if workflow.status != WorkflowStatus.RUNNING:
            return []
        step = workflow.current_step()
        if not step:
            return []
        allowed = list(dict.fromkeys(step.allowed_tools))
        if step.status == StepStatus.AWAITING_OBSERVATION:
            return [name for name in allowed if name in OBSERVATION_SOURCES]
        return allowed

    def block(self, workflow: WorkflowState, reason: str) -> None:
        workflow.status = WorkflowStatus.BLOCKED
        step = workflow.current_step()
        if step:
            step.status = StepStatus.BLOCKED
        workflow.verification = VerificationDecision(
            action_succeeded=any(item.status == "ok" for item in workflow.evidence),
            step_satisfied=False,
            goal_satisfied=False,
            reason=reason,
            missing_outputs=[item.name for item in workflow.goal.requested_outputs if item.required],
        )
        workflow.updated_at = time.time()

    def progress_signature(self, workflow: WorkflowState) -> str:
        step = workflow.current_step()
        evidence = self._step_evidence(workflow, step) if step else []
        stable_facts = sorted(
            {
                _stable_evidence_identity(item)
                for item in evidence
                if item.status == "ok" and (item.facts or item.source in ACTION_SOURCES)
            }
        )
        action_count = sum(item.status == "ok" and item.source in ACTION_SOURCES for item in evidence)
        payload = {
            "step_id": step.step_id if step else "none",
            "step_index": workflow.current_step_index,
            "step_status": step.status.value if step else "none",
            "facts": stable_facts,
            "action_count": action_count,
            "outputs": workflow.outputs,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def _step_evidence(self, workflow: WorkflowState, step: StepContract | None) -> list[ExecutionEvidence]:
        if not step:
            return []
        ids = set(step.evidence_ids)
        return [item for item in workflow.evidence if item.evidence_id in ids]

    def _verify_step(
        self,
        step: StepContract | None,
        evidence: list[ExecutionEvidence],
    ) -> tuple[bool, str]:
        if not step:
            return False, "No executable step is available."
        successful = [item for item in evidence if item.status == "ok"]
        if not successful:
            return False, f"Step '{step.name}' has no successful evidence yet."
        searchable = _searchable_evidence(successful)
        if step.kind == "observe":
            observed = any(item.source in OBSERVATION_SOURCES for item in successful)
            mode = _completion_mode(step)
            sources = {item.source for item in successful if item.source in OBSERVATION_SOURCES}
            required_sources = set(step.required_sources)
            if mode == "all_sources":
                condition_satisfied = bool(required_sources) and required_sources <= sources
            elif mode == "any_source":
                condition_satisfied = bool(sources & required_sources) if required_sources else observed
            elif mode == "facts":
                condition_satisfied = observed and bool(step.required_facts) and _facts_satisfied(
                    searchable, step.required_facts
                )
            else:
                condition_satisfied = observed and _fact_matches(searchable, step.done_condition)
            return (
                condition_satisfied,
                "Observation satisfies the step condition."
                if condition_satisfied
                else (
                    "The observation does not yet satisfy the step condition."
                    if observed
                    else "A structured observation is still required."
                ),
            )
        if step.kind == "act":
            action_indexes = [index for index, item in enumerate(successful) if item.source in ACTION_SOURCES]
            if not action_indexes:
                return False, "The planned action has not succeeded yet."
            last_action = action_indexes[-1]
            post_action_observations = [
                item for item in successful[last_action + 1 :] if item.source in OBSERVATION_SOURCES
            ]
            observed_after = bool(post_action_observations)
            # A successful file/command mutation IS a completed act - its own result is the
            # evidence. Only UI actions need a fresh look at the screen, because a click can
            # "succeed" while the interface does something else entirely.
            #
            # This deliberately does NOT depend on whether an observation followed. It used to
            # (`not observed_after and ...`), which meant reading the file back made the step
            # HARDER to verify than not looking at all: with an observation present the step fell
            # through to fuzzy-matching the plan's descriptive done_condition ("檔案已建立且內容
            # 正確") against the file's actual text, which never matches, so the workflow spun and
            # blocked on a file that had been written correctly (live 2026-07-22).
            if successful[last_action].source not in UI_ACTION_SOURCES:
                return True, "The action completed successfully."
            if self.require_semantic_actions and observed_after and successful[last_action].source in UI_ACTION_SOURCES:
                semantic = next(
                    (item for item in reversed(successful[last_action + 1 :]) if item.source == "semantic_verifier"),
                    None,
                )
                latest_revision = post_action_observations[-1].observation_revision
                if not semantic or (
                    latest_revision
                    and semantic.observation_revision
                    and semantic.observation_revision != latest_revision
                ):
                    return False, "The resulting state needs semantic verification."
                satisfied = bool(semantic.facts.get("condition_satisfied"))
                return satisfied, (
                    "Semantic verification confirms the step condition."
                    if satisfied
                    else "Semantic verification rejected the resulting state."
                )
            condition_satisfied = observed_after and _fact_matches(
                _searchable_evidence(post_action_observations), step.done_condition
            )
            return (
                condition_satisfied,
                "Action succeeded and the resulting state satisfies the step condition."
                if condition_satisfied
                else (
                    "The resulting state does not yet satisfy the step condition."
                    if observed_after
                    else "The action succeeded but needs a fresh observation."
                ),
            )
        if step.kind == "verify":
            condition_satisfied = _fact_matches(searchable, step.done_condition) and _facts_satisfied(
                searchable, step.required_facts
            )
            return condition_satisfied, (
                "Verification evidence satisfies the step condition."
                if condition_satisfied
                else "Verification evidence does not yet satisfy the step condition."
            )
        if step.kind == "reply":
            return False, "Reply is emitted only after the goal is satisfied."
        return True, "Step evidence accepted."

    def _extract_outputs(
        self, goal: GoalContract, evidence: list[ExecutionEvidence]
    ) -> tuple[dict[str, Any], list[str]]:
        searchable = _searchable_evidence(evidence)
        outputs: dict[str, Any] = {}
        missing: list[str] = []
        percentages = _percentage_facts(searchable)
        for output in goal.requested_outputs:
            if not output.required:
                continue
            raw_hint = goal.objective + " " + output.name + " " + output.description
            hint = _normalize(raw_hint)
            is_percentage_output = any(token in hint for token in ("percentage", "percent", "%", "quota", "remaining"))
            required_facts = [_normalize(item) for item in output.required_facts if item.strip()]
            if required_facts and not is_percentage_output and not _facts_satisfied(searchable, output.required_facts):
                missing.append(output.name)
                continue
            if output.evidence_kind == "screen_state":
                if not required_facts:
                    missing.append(output.name)
                    continue
                action_times = [
                    item.created_at for item in evidence if item.source in ACTION_SOURCES and item.status == "ok"
                ]
                latest_action = max(action_times) if action_times else 0.0
                observed_after = [
                    item
                    for item in evidence
                    if item.source in OBSERVATION_SOURCES and item.status == "ok" and item.created_at > latest_action
                ]
                observed_after_text = _searchable_evidence(observed_after)
                facts_confirmed = bool(observed_after) and _facts_satisfied(observed_after_text, output.required_facts)
                if latest_action and facts_confirmed:
                    outputs[output.name] = {"verified": True, "facts": output.required_facts}
                else:
                    missing.append(output.name)
                continue
            if any(token in hint for token in ("percentage", "percent", "%", "quota", "remaining")):
                contextual = _select_percentage(percentages, hint)
                if contextual:
                    outputs[output.name] = contextual
                else:
                    missing.append(output.name)
                continue
            if output.evidence_kind == "text":
                # Walk newest-first and take the first evidence that yields REAL text. stdout
                # outranks the tool's generic completion message (a version task once completed
                # with the literal value "Command completed."), generic status phrases never bind,
                # and there is deliberately NO facts-json fallback (it once bound a file-listing
                # blob as the "file content"). An empty value is NOT a result - if nothing real
                # exists yet the output stays missing and the workflow continues.
                selected = ""
                for item in reversed(evidence):
                    if item.status != "ok":
                        continue
                    text_value = (
                        # The exactly-named fact wins: report_result stores {output_name: value},
                        # and taking its summary instead leaked the internal
                        # "Recorded derived result: ..." wording into the owner's reply.
                        str(item.facts.get(output.name) or "").strip()
                        or str(item.facts.get("text") or "").strip()
                        or str(item.facts.get("stdout") or "").strip()
                        or _non_generic(item.summary.strip())
                    )
                    candidate = _select_text_output(text_value, raw_hint) if text_value else ""
                    if str(candidate).strip():
                        selected = candidate
                        break
                if str(selected).strip():
                    outputs[output.name] = selected
                else:
                    missing.append(output.name)
                continue
            if output.evidence_kind == "artifact":
                artifacts = [artifact for item in evidence if item.status == "ok" for artifact in item.artifacts]
                if artifacts:
                    outputs[output.name] = artifacts[-1]
                else:
                    missing.append(output.name)
                continue
            if output.evidence_kind == "status":
                exact_status = _find_named_fact(evidence, output.name)
                if exact_status in (None, ""):
                    exact_status = _find_named_fact(evidence, "status")
                if exact_status not in (None, ""):
                    outputs[output.name] = exact_status
                else:
                    missing.append(output.name)
                continue
            if any(
                token in hint for token in ("百分", "percentage", "%", "用量", "quota", "remaining", "限额", "限額")
            ):
                contextual = _select_percentage(percentages, hint)
                if contextual:
                    outputs[output.name] = contextual
                else:
                    missing.append(output.name)
                continue
            exact = _find_named_fact(evidence, output.name)
            if exact not in (None, ""):
                outputs[output.name] = exact
            else:
                missing.append(output.name)
        return outputs, missing


# Tool-status phrases describe the EXECUTION, not the requested result. Binding one as an output
# value produced owner-facing replies like 「找到了，結果是 Command completed.」/「File list
# completed.」. Pattern, not enumeration - every tool _ok() message follows this English shape,
# and enumerating them is a losing game (the battery found a new one within hours).
_GENERIC_STATUS_RE = re.compile(
    r"^[a-z][a-z /_-]{0,50}(completed|written|created|finished|done|succeeded|recorded|analyzed|ok)\.?$",
    re.IGNORECASE,
)
_GENERIC_STATUS_WORDS = {"done", "done.", "ok", "ok.", "success", "success."}
# Internal tool phrasings that must never surface as the owner-facing answer.
_INTERNAL_RESULT_PREFIXES = ("recorded derived result",)


def _non_generic(text: str) -> str:
    stripped = text.strip()
    if stripped.casefold() in _GENERIC_STATUS_WORDS or _GENERIC_STATUS_RE.match(stripped):
        return ""
    if stripped.casefold().startswith(_INTERNAL_RESULT_PREFIXES):
        return ""
    return text


def _searchable_evidence(evidence: list[ExecutionEvidence]) -> str:
    chunks: list[str] = []
    for item in evidence:
        chunks.append(item.summary)
        chunks.append(json_like_text(item.facts))
    return "\n".join(chunks)


def json_like_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key} {json_like_text(item)}" for key, item in value.items())
    if isinstance(value, list):
        return " ".join(json_like_text(item) for item in value)
    return str(value or "")


def _completion_mode(step: StepContract) -> str:
    mode = str(step.completion_mode or "auto")
    if mode != "auto":
        return mode
    if step.required_sources:
        return "all_sources"
    if step.required_facts:
        return "facts"
    return "any_source" if step.kind == "observe" else "semantic"


_VOLATILE_PROGRESS_KEYS = {
    "created_at",
    "revision",
    "screenshot_id",
    "snapshot_id",
    "artifact",
    "artifacts",
    "path",
}


def _stable_evidence_identity(evidence: ExecutionEvidence) -> str:
    stable = _stable_progress_value(evidence.facts)
    payload = {"source": evidence.source, "facts": stable}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _stable_progress_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _stable_progress_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key).casefold() not in _VOLATILE_PROGRESS_KEYS
        }
    if isinstance(value, list):
        normalized = [_stable_progress_value(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
    return value


def _normalize(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(value or "").casefold(), flags=re.UNICODE)


_FACT_STOPWORDS = {
    "a",
    "about",
    "action",
    "active",
    "after",
    "again",
    "against",
    "all",
    "already",
    "am",
    "an",
    "and",
    "app",
    "application",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "being",
    "below",
    "between",
    "both",
    "brought",
    "but",
    "by",
    "clicked",
    "confirmed",
    "confirming",
    "containing",
    "could",
    "current",
    "displayed",
    "button",
    "control",
    "element",
    "entry",
    "exact",
    "exists",
    "focus",
    "focused",
    "foreground",
    "found",
    "from",
    "gear",
    "had",
    "has",
    "have",
    "having",
    "icon",
    "item",
    "here",
    "how",
    "identified",
    "in",
    "is",
    "it",
    "its",
    "known",
    "label",
    "limit",
    "locate",
    "labeled",
    "located",
    "matching",
    "may",
    "menu",
    "more",
    "most",
    "must",
    "no",
    "not",
    "now",
    "observation",
    "observed",
    "of",
    "on",
    "once",
    "only",
    "other",
    "our",
    "out",
    "over",
    "page",
    "panel",
    "point",
    "present",
    "previous",
    "evidence",
    "read",
    "requested",
    "result",
    "resulting",
    "same",
    "screen",
    "section",
    "should",
    "shown",
    "shows",
    "some",
    "state",
    "step",
    "such",
    "target",
    "than",
    "the",
    "their",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "title",
    "to",
    "under",
    "until",
    "up",
    "value",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "whether",
    "window",
    "with",
    "within",
    "would",
}

_CHINESE_FACT_STOPPHRASES = (
    "\u5df2\u7d93",
    "\u76ee\u524d",
    "\u756b\u9762",
    "\u9801\u9762",
    "\u5340\u6bb5",
    "\u5340\u57df",
    "\u5165\u53e3",
    "\u770b\u898b",
    "\u770b\u5230",
    "\u986f\u793a",
    "\u78ba\u8a8d",
    "\u53d6\u5f97",
    "\u8b80\u53d6",
    "\u627e\u5230",
)


def _facts_satisfied(searchable: str, facts: list[str]) -> bool:
    return all(_fact_matches(searchable, fact) for fact in facts if str(fact).strip())


def _fact_matches(searchable: str, fact: str) -> bool:
    haystack = str(searchable or "").casefold()
    needle = str(fact or "").casefold().strip()
    if not needle:
        return True
    needle = re.sub(r"\([^)]*(?:\bnot\b|\bwithout\b|\u4e0d\u662f|\u4e0d\u8981)[^)]*\)", "", needle)
    alternatives = [item.strip() for item in re.split(r"\bor\b|\u6216\u8005|\u6216", needle) if item.strip()]
    if len(alternatives) > 1:
        return any(_fact_matches(haystack, item) for item in alternatives)
    quoted = [item.strip() for item in re.findall(r"['\"]([^'\"]+)['\"]", needle) if item.strip()]
    if quoted:
        return all(_normalize(item) in _normalize(haystack) for item in quoted)
    if _normalize(needle) in _normalize(haystack):
        return True
    semantic_needle = needle
    for phrase in _CHINESE_FACT_STOPPHRASES:
        semantic_needle = semantic_needle.replace(phrase, "")
    english = [
        token for token in re.findall(r"[a-z0-9]+", semantic_needle) if len(token) >= 3 and token not in _FACT_STOPWORDS
    ]
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]{2,}", semantic_needle)
    chinese = []
    for chunk in chinese_chunks:
        if chunk in haystack:
            chinese.append(chunk)
        else:
            chinese.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
    tokens = list(dict.fromkeys(english + chinese))
    if not tokens:
        return True
    matched = sum(1 for token in tokens if token in haystack)
    return matched / len(tokens) >= 0.75


def _percentage_facts(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    for index, line in enumerate(lines):
        for match in re.finditer(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*%", line):
            value = match.group(1) + "%"
            start = max(0, index - 2)
            end = index + 1
            rows.append({"value": value, "context": " | ".join(lines[start:end])[:800]})
    return rows


def _select_percentage(rows: list[dict[str, str]], hint: str) -> dict[str, str] | None:
    if not rows:
        return None
    preferred = []
    requires_five_hour_context = any(
        token in hint for token in ("5小时", "五小时", "5小時", "五小時", "5hour", "fivehour")
    )
    requires_weekly_context = any(token in hint for token in ("每周", "每週", "weekly"))
    if requires_five_hour_context:
        preferred = [
            row
            for row in rows
            if any(
                token in _normalize(row["context"])
                for token in ("5小时", "五小时", "5小時", "五小時", "5hour", "fivehour")
            )
        ]
        return preferred[0] if preferred else None
    if requires_weekly_context:
        preferred = [
            row for row in rows if any(token in _normalize(row["context"]) for token in ("每周", "每週", "weekly"))
        ]
        return preferred[0] if preferred else None
    return (preferred or rows)[0]


def _find_named_fact(evidence: list[ExecutionEvidence], name: str) -> Any:
    normalized = _normalize(name)
    for item in reversed(evidence):
        for key, value in item.facts.items():
            if _normalize(key) == normalized:
                return value
    return None



def _select_text_output(text: str, hint: str, limit: int = 2000) -> str:
    value = str(text or "").strip()
    normalized_hint = _normalize(hint)
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    first_heading_markers = ("第一個標題", "第一个标题", "首個標題", "首个标题", "firstheading")
    if any(_normalize(marker) in normalized_hint for marker in first_heading_markers):
        heading = next((line for line in lines if re.match(r"^#{1,6}\s+\S", line)), None)
        return heading or (lines[0] if lines else "")
    if any(_normalize(marker) in normalized_hint for marker in ("第一行", "首行", "firstline")):
        return lines[0] if lines else ""
    if len(value) <= limit:
        return value
    return value[: limit - 80].rstrip() + "\n...[content truncated]"
