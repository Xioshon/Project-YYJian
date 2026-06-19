import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from agent_latency import ResponsePolicy
from agent_reply_composer import ReplyComposer, ReplyEvent
from agent_self_recovery import IDEMPOTENT_RETRY_TOOLS
from agent_session import SessionBrain
from agent_tool_runtime import is_safe_verifier_command
from core_tools import PROJECT_CACHE_DIR, ROOT_DIR, ToolResult, is_workspace_path, resolve_path


_T = {
    "no_result": "\u525b\u525b\u6c92\u6709\u65b0\u7684\u5de5\u5177\u7d50\u679c\u5594\u3002",
    "previous_status": "\u4e0a\u4e00\u6b65\u72c0\u614b",
    "artifacts": "\u6211\u770b\u5230\u7684\u7d50\u679c\u6a94\u6848",
    "pending_validation": "\u76ee\u524d\u9084\u5728\u7b49\u9a57\u8b49",
    "missing_artifact": "\u525b\u525b\u6c92\u6709\u627e\u5230\u53ef\u4ee5\u767c\u7d66\u4f60\u7684\u7d50\u679c\u6a94\u6848\u3002\u4f60\u8981\u6211\u91cd\u8a66\u6216\u91cd\u65b0\u622a\u5716\uff0c\u76f4\u63a5\u8aaa\u300c\u7e7c\u7e8c\u300d\u5c31\u597d\u3002",
    "caption": "\u525b\u525b\u7684\u7d50\u679c\u5594",
    "analyze_prompt": "\u7528\u7e41\u9ad4\u4e2d\u6587\u7c21\u77ed\u5206\u6790\u9019\u5f35\u5716\u7247\u6216\u622a\u5716\uff0c\u5148\u8aaa\u91cd\u9ede\u3002",
    "worker_started": "\u6211\u5df2\u7d93\u628a {name} \u9a57\u8b49\u653e\u5230\u80cc\u666f\u8dd1\u4e86\uff0cjob={job}\u3002",
    "worker_failed": "\u9a57\u8b49\u555f\u52d5\u5931\u6557\uff1a{error}",
    "no_safe_verifier": "\u76ee\u524d\u6c92\u6709\u5b89\u5168\u53ef\u4ee5\u76f4\u63a5\u8dd1\u7684\u4e0b\u4e00\u6b65\uff0c\u6211\u5148\u505c\u4f4f\uff0c\u4e0d\u4e82\u52d5\u4f60\u96fb\u8166\u3002",
}


@dataclass
class OutcomeHandled:
    content: str
    reasoning: str = ""

    def to_chat_result(self) -> dict[str, str]:
        return {"content": self.content, "reasoning": self.reasoning}


def tool_result_outcome(tool_name: str, result: ToolResult) -> tuple[str, list[str]]:
    lines = [f"{tool_name}: {result.status} - {result.message}"]
    artifacts = collect_result_artifacts(result)
    data = result.data if isinstance(result.data, dict) else {}
    stdout = str(data.get("stdout") or "").strip()
    stderr = str(data.get("stderr") or "").strip()
    returncode = data.get("returncode")
    if returncode is not None:
        lines.append(f"returncode: {returncode}")
    if stdout:
        lines.append("stdout:\n" + stdout[:1600])
    if stderr:
        lines.append("stderr:\n" + stderr[:1200])
    if result.error:
        lines.append("error:\n" + str(result.error)[:1200])
    recovery = data.get("recovery_attempted") if isinstance(data, dict) else None
    if isinstance(recovery, dict):
        reason = str(recovery.get("reason") or recovery.get("details", {}).get("strategy") or "").strip()
        attempts = recovery.get("attempts") or 0
        retry_status = str(recovery.get("retry_status") or "").strip()
        lines.append(f"recovery_attempted: {reason or 'auto_recovery'} attempts={attempts} status={retry_status or 'not_ok'}")
    if artifacts:
        lines.append("artifacts:\n" + "\n".join(f"- {item}" for item in artifacts[:8]))
    return "\n".join(lines), artifacts


def collect_result_artifacts(result: ToolResult) -> list[str]:
    candidates: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            candidates.extend(path_candidates_from_text(value))

    visit(result.data)
    visit(result.message)
    visit(result.error)
    return dedupe_existing_paths(candidates)


def path_candidates_from_text(text: str) -> list[str]:
    if not text:
        return []
    found = re.findall(
        r"[A-Za-z]:\\[^\s\"'<>|]+|(?:[\w .\-\u4e00-\u9fff]+)\.(?:png|jpg|jpeg|webp|gif|txt|json|md|log|py)",
        text,
    )
    return [item.strip("`'\"\uff0c\u3002\uff1b;:,") for item in found if item.strip()]


def dedupe_existing_paths(candidates: list[str]) -> list[str]:
    resolved: list[str] = []
    for candidate in candidates:
        paths = []
        if os.path.isabs(candidate):
            paths.append(candidate)
        else:
            paths.extend(
                [
                    resolve_path(candidate),
                    os.path.join(ROOT_DIR, "workspace", candidate),
                    os.path.join(PROJECT_CACHE_DIR, candidate),
                ]
            )
        for path in paths:
            try:
                absolute = os.path.abspath(path)
                if os.path.exists(absolute) and is_workspace_path(absolute) and absolute not in resolved:
                    resolved.append(absolute)
                    break
            except Exception:
                continue
    return resolved


def is_result_followup(text: str) -> bool:
    normalized = (text or "").strip().casefold()
    markers = [
        "\u7d50\u679c",
        "\u6709\u7d50\u679c",
        "\u600e\u9ebc\u6a23",
        "\u600e\u4e48\u6837",
        "\u622a\u5716\u5462",
        "\u622a\u56fe\u5462",
        "\u5716\u5462",
        "\u56fe\u5462",
        "\u8dd1\u5b8c\u4e86",
        "\u597d\u4e86\u55ce",
        "\u597d\u4e86\u5417",
        "\u6210\u529f\u4e86\u55ce",
        "\u6210\u529f\u4e86\u5417",
        "\u6240\u4ee5\u5462",
        "\u5982\u4f55",
        "\u6709\u7b54\u6848\u55ce",
        "\u6709\u7b54\u6848\u5417",
        "status",
        "result",
    ]
    return bool(normalized) and len(normalized) <= 120 and any(marker in normalized for marker in markers)


def detect_outcome_action(text: str) -> str:
    normalized = (text or "").strip().casefold()
    if not normalized or len(normalized) > 140:
        return ""
    if any(
        marker in normalized
        for marker in [
            "\u767c\u7d66\u6211",
            "\u53d1\u7ed9\u6211",
            "\u50b3\u7d66\u6211",
            "\u4f20\u7ed9\u6211",
            "\u767c\u5716",
            "\u53d1\u56fe",
            "\u767c\u5716\u7247",
            "\u53d1\u56fe\u7247",
            "send it",
            "send file",
        ]
    ):
        return "send_artifact"
    if any(
        marker in normalized
        for marker in [
            "\u5206\u6790\u4e00\u4e0b",
            "\u5206\u6790\u4e0b",
            "\u9019\u662f\u4ec0\u9ebc",
            "\u8fd9\u662f\u4ec0\u4e48",
            "\u770b\u770b\u5167\u5bb9",
            "\u770b\u770b\u5185\u5bb9",
            "analyze it",
        ]
    ):
        return "analyze_artifact"
    if any(
        marker in normalized
        for marker in [
            "\u7e7c\u7e8c",
            "\u7ee7\u7eed",
            "\u4e0b\u4e00\u6b65",
            "\u8dd1\u5427",
            "\u63a5\u8457",
            "\u63a5\u7740",
            "\u518d\u8a66",
            "\u518d\u8bd5",
            "\u91cd\u8a66",
            "\u91cd\u8bd5",
            "continue",
            "next step",
        ]
    ):
        return "continue_task"
    return ""


def format_last_outcome_reply(brain: SessionBrain) -> str:
    state = brain.state
    if not state.last_tool:
        return _T["no_result"]
    lines = [f"{_T['previous_status']}: {state.last_tool_status or 'unknown'}"]
    if state.last_tool_summary:
        lines.append(state.last_tool_summary[-1600:])
    if state.last_artifacts:
        lines.append(_T["artifacts"] + ":")
        lines.extend(f"- {item}" for item in state.last_artifacts[-5:])
    if state.pending_validation:
        lines.append(_T["pending_validation"] + ": " + " | ".join(state.pending_validation[-3:]))
    return "\n".join(lines)


def artifact_for_action(brain: SessionBrain) -> str:
    for artifact in brain.state.last_artifacts:
        try:
            if os.path.exists(artifact) and is_workspace_path(artifact):
                return artifact
        except Exception:
            continue
    return ""


def next_verifier_from_plan(lines: list[str]) -> str:
    allowed = ("py_compile", "self_test", "agent_eval", "trace_summary")
    for line in lines or []:
        lowered = str(line or "").casefold()
        for name in allowed:
            if lowered.startswith(name.casefold() + " ") or lowered.startswith(name.casefold() + "(") or name.casefold() in lowered:
                return name
    return ""


class OutcomeController:
    def __init__(
        self,
        *,
        session_brain: SessionBrain,
        task_graphs: Any,
        executor: Any,
        worker_queue: Any,
        hooks: Any,
        after_tool_result: Callable[..., tuple[Any, dict[str, Any] | None]],
        append_reply: Callable[[str, str], None],
        session_id: str,
        turn_id_getter: Callable[[], int],
        recover_tool_result: Callable[[str, dict, ToolResult, Callable | None, Any], tuple[ToolResult, dict[str, Any] | None]] | None = None,
        reply_composer: ReplyComposer | None = None,
    ):
        self.session_brain = session_brain
        self.task_graphs = task_graphs
        self.executor = executor
        self.worker_queue = worker_queue
        self.hooks = hooks
        self.after_tool_result = after_tool_result
        self.recover_tool_result = recover_tool_result
        self.append_reply = append_reply
        self.reply_composer = reply_composer or ReplyComposer()
        self.session_id = session_id
        self.turn_id_getter = turn_id_getter

    def maybe_handle(self, action: str, user_input: str, tool_callback: Callable | None) -> OutcomeHandled | None:
        artifact = artifact_for_action(self.session_brain)
        if action in {"send_artifact", "analyze_artifact"} and not artifact:
            return self._finish(user_input, _T["missing_artifact"], event_type="outcome_reply")
        if action == "send_artifact":
            return self._send_artifact(user_input, artifact, tool_callback)
        if action == "analyze_artifact":
            return self._analyze_artifact(user_input, artifact, tool_callback)
        if action == "continue_task":
            return self._continue_task(user_input, tool_callback)
        return None

    def result_followup(self, user_input: str) -> OutcomeHandled:
        return self._finish(user_input, format_last_outcome_reply(self.session_brain), event_type="outcome_reply")

    def _send_artifact(self, user_input: str, artifact: str, tool_callback: Callable | None) -> OutcomeHandled:
        args = {"file_path": artifact, "caption": _T["caption"]}
        policy = ResponsePolicy(max_tool_iterations=1, route="artifact_send")
        result = self.executor.execute("send_telegram_media", args, tool_callback, policy)
        if self.recover_tool_result is not None:
            result, _recovery = self.recover_tool_result("send_telegram_media", args, result, tool_callback, policy)
        verification, replay_case = self.after_tool_result("send_telegram_media", args, result)
        summary = f"artifact={os.path.basename(artifact)} status={result.status} message={result.message}"
        event_type = "tool_success" if result.status == "ok" else "permission_request" if result.requires_permission else "tool_error"
        if replay_case:
            event_type = "repeated_tool_stop"
        return self._finish(
            user_input,
            summary,
            event_type=event_type,
            tool_name="send_telegram_media",
            result=result,
            artifacts=[artifact],
            verification_status=getattr(verification, "status", ""),
        )

    def _analyze_artifact(self, user_input: str, artifact: str, tool_callback: Callable | None) -> OutcomeHandled:
        args = {"file_path": artifact, "prompt": _T["analyze_prompt"]}
        policy = ResponsePolicy(max_tool_iterations=1, allow_vision=True, route="artifact_analysis")
        result = self.executor.execute("analyze_media", args, tool_callback, policy)
        if self.recover_tool_result is not None:
            result, _recovery = self.recover_tool_result("analyze_media", args, result, tool_callback, policy)
        verification, replay_case = self.after_tool_result("analyze_media", args, result)
        summary = result.data.get("summary") if isinstance(result.data, dict) else result.message
        event_type = "tool_success" if result.status == "ok" else "tool_error"
        if replay_case:
            event_type = "repeated_tool_stop"
        return self._finish(
            user_input,
            str(summary),
            event_type=event_type,
            tool_name="analyze_media",
            result=result,
            artifacts=[artifact],
            verification_status=getattr(verification, "status", ""),
        )

    def _continue_task(self, user_input: str, tool_callback: Callable | None = None) -> OutcomeHandled:
        verifier_name = next_verifier_from_plan(self.session_brain.state.verification_plan)
        if verifier_name:
            try:
                job = self.worker_queue.start_verifier(
                    verifier_name,
                    timeout=180 if verifier_name == "self_test" else 90,
                    metadata={
                        "session_id": self.session_id,
                        "turn_id": self.turn_id_getter(),
                        "source": "outcome_continue",
                        "last_tool": self.session_brain.state.last_tool,
                    },
                )
                return self._finish(
                    user_input,
                    _T["worker_started"].format(name=verifier_name, job=job.job_id),
                    event_type="planner_summary",
                )
            except Exception as exc:
                return self._finish(user_input, _T["worker_failed"].format(error=exc), event_type="tool_error")
        if self.session_brain.state.verification_plan:
            return self._finish(user_input, _T["no_safe_verifier"], event_type="outcome_reply")
        retried = self._retry_last_safe_failed_step(user_input, tool_callback)
        if retried is not None:
            return retried
        return self._finish(user_input, format_last_outcome_reply(self.session_brain), event_type="outcome_reply")

    def _retry_last_safe_failed_step(self, user_input: str, tool_callback: Callable | None = None) -> OutcomeHandled | None:
        graph, step = _latest_retryable_graph_and_step(self.task_graphs)
        if not graph or not step:
            return None
        tool_name = step.tool_name
        arguments = dict(step.arguments or {})
        if not _is_safe_retry_step(tool_name, arguments):
            return None
        policy = ResponsePolicy(max_tool_iterations=1, route="outcome_retry")
        result = self.executor.execute(tool_name, arguments, tool_callback, policy)
        if self.recover_tool_result is not None:
            result, _recovery = self.recover_tool_result(tool_name, arguments, result, tool_callback, policy)
        verification, replay_case = self.after_tool_result(tool_name, arguments, result, getattr(graph, "task_id", ""))
        summary, artifacts = tool_result_outcome(tool_name, result)
        event_type = "tool_success" if result.status == "ok" else "permission_request" if result.requires_permission else "tool_error"
        if replay_case:
            event_type = "repeated_tool_stop"
        return self._finish(
            user_input,
            summary,
            event_type=event_type,
            tool_name=tool_name,
            result=result,
            artifacts=artifacts,
            verification_status=getattr(verification, "status", ""),
            retried_tool=tool_name,
        )

    def _finish(self, user_input: str, final_reply: str, **trace_fields: Any) -> OutcomeHandled:
        event_type = str(trace_fields.pop("event_type", "outcome_reply"))
        tool_name = str(trace_fields.pop("tool_name", ""))
        result = trace_fields.pop("result", None)
        artifacts = trace_fields.pop("artifacts", [])
        rendered = self.reply_composer.compose(
            ReplyEvent(event_type, user_input=user_input, tool_name=tool_name, result=result, summary=final_reply, artifacts=artifacts),
            final_reply,
        )
        self.append_reply(user_input, rendered)
        self.hooks.emit("Stop", session_id=self.session_id, turn_id=self.turn_id_getter(), content_preview=rendered[:160], **trace_fields)
        return OutcomeHandled(rendered)


def _latest_retryable_step(graph: Any) -> Any:
    for step in reversed(getattr(graph, "steps", []) or []):
        if getattr(step, "status", "") in {"fail", "blocked"} or getattr(step, "result_status", "") == "error":
            return step
    return None


def _latest_retryable_graph_and_step(task_graphs: Any) -> tuple[Any, Any]:
    if task_graphs is None:
        return None, None
    graphs = list(getattr(task_graphs, "graphs", []) or [])
    active = task_graphs.active() if hasattr(task_graphs, "active") else None
    if active is not None and active not in graphs:
        graphs.append(active)
    for graph in reversed(graphs):
        if getattr(graph, "status", "") not in {"active", "awaiting_permission", "awaiting_validation", "blocked"}:
            continue
        step = _latest_retryable_step(graph)
        if step is not None:
            return graph, step
    return None, None


def _is_safe_retry_step(tool_name: str, arguments: dict[str, Any]) -> bool:
    if tool_name in IDEMPOTENT_RETRY_TOOLS:
        return True
    if tool_name == "execute_command":
        return is_safe_verifier_command(str((arguments or {}).get("command") or ""))
    return False
