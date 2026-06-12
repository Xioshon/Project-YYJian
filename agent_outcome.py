import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from agent_latency import ResponsePolicy
from agent_session import SessionBrain
from core_tools import PROJECT_CACHE_DIR, ROOT_DIR, ToolResult, is_workspace_path, resolve_path


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
        lines.append("stdout:\n" + stdout[:1200])
    if stderr:
        lines.append("stderr:\n" + stderr[:1200])
    if result.error:
        lines.append("error:\n" + str(result.error)[:1200])
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
    found = re.findall(r"[A-Za-z]:\\[^\s\"'<>|]+|(?:[\w .\-\u4e00-\u9fff]+)\.(?:png|jpg|jpeg|webp|gif|txt|json|md|log|py)", text)
    return [item.strip("`'\"，。；;:,") for item in found if item.strip()]


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
    markers = ["結果", "有結果", "怎麼樣", "截圖呢", "圖呢", "result", "status"]
    return bool(normalized) and len(normalized) <= 120 and any(marker.casefold() in normalized for marker in markers)


def detect_outcome_action(text: str) -> str:
    normalized = (text or "").strip().casefold()
    if not normalized or len(normalized) > 140:
        return ""
    if any(marker in normalized for marker in ["發給我", "傳給我", "上傳給我", "發圖", "傳圖", "send it", "send file"]):
        return "send_artifact"
    if any(marker in normalized for marker in ["分析一下", "分析下", "這是什麼", "这是什么", "analyze it"]):
        return "analyze_artifact"
    if any(marker in normalized for marker in ["繼續", "继续", "下一步", "跑吧", "continue", "next step"]):
        return "continue_task"
    return ""


def format_last_outcome_reply(brain: SessionBrain) -> str:
    state = brain.state
    if not state.last_tool:
        return "剛剛沒有可回報的工具結果喵。你要我繼續哪個任務，可以直接說一下。"
    lines = [f"剛剛 `{state.last_tool}` 的結果是：{state.last_tool_status or 'unknown'}"]
    if state.last_tool_summary:
        lines.append(state.last_tool_summary[-1400:])
    if state.last_artifacts:
        lines.append("我看到的產物：")
        lines.extend(f"- {item}" for item in state.last_artifacts[-5:])
    if state.pending_validation:
        lines.append("目前還在等待後續確認：" + " | ".join(state.pending_validation[-3:]))
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
        executor: Any,
        worker_queue: Any,
        hooks: Any,
        after_tool_result: Callable[[str, dict, ToolResult], tuple[Any, dict[str, Any] | None]],
        append_reply: Callable[[str, str], None],
        session_id: str,
        turn_id_getter: Callable[[], int],
    ):
        self.session_brain = session_brain
        self.executor = executor
        self.worker_queue = worker_queue
        self.hooks = hooks
        self.after_tool_result = after_tool_result
        self.append_reply = append_reply
        self.session_id = session_id
        self.turn_id_getter = turn_id_getter

    def maybe_handle(self, action: str, user_input: str, tool_callback: Callable | None) -> OutcomeHandled | None:
        artifact = artifact_for_action(self.session_brain)
        if action in {"send_artifact", "analyze_artifact"} and not artifact:
            return self._finish(user_input, "剛剛沒有找到可用的產物檔案喵。你要我重新截圖或重跑剛剛的步驟，可以直接說「重試」。")
        if action == "send_artifact":
            return self._send_artifact(user_input, artifact, tool_callback)
        if action == "analyze_artifact":
            return self._analyze_artifact(user_input, artifact, tool_callback)
        if action == "continue_task":
            return self._continue_task(user_input)
        return None

    def result_followup(self, user_input: str) -> OutcomeHandled:
        return self._finish(user_input, format_last_outcome_reply(self.session_brain))

    def _send_artifact(self, user_input: str, artifact: str, tool_callback: Callable | None) -> OutcomeHandled:
        args = {"file_path": artifact, "caption": "剛剛的結果喵"}
        result = self.executor.execute("send_telegram_media", args, tool_callback, None)
        verification, replay_case = self.after_tool_result("send_telegram_media", args, result)
        if result.status == "ok":
            final_reply = f"發給你啦喵：{os.path.basename(artifact)}"
        elif replay_case:
            final_reply = f"`send_telegram_media` 重複卡住，我先停下來了。Replay case: {replay_case.get('name')}"
        elif result.requires_permission:
            self.session_brain.mark_permission_needed("send_telegram_media", self.turn_id_getter(), self.session_id)
            final_reply = f"發送這個檔案需要你確認一下：{os.path.basename(artifact)}"
        else:
            final_reply = f"我想發給你，但發送失敗了：{result.message}"
            if result.error:
                final_reply += f"\n{result.error[:800]}"
        return self._finish(user_input, final_reply, verification_status=getattr(verification, "status", ""))

    def _analyze_artifact(self, user_input: str, artifact: str, tool_callback: Callable | None) -> OutcomeHandled:
        args = {"file_path": artifact, "prompt": "用繁體中文簡短分析這張圖片或截圖，先說重點。"}
        result = self.executor.execute("analyze_media", args, tool_callback, ResponsePolicy(max_tool_iterations=1, allow_vision=True, route="artifact_analysis"))
        verification, replay_case = self.after_tool_result("analyze_media", args, result)
        if result.status == "ok":
            summary = result.data.get("summary") if isinstance(result.data, dict) else ""
            final_reply = f"我看完啦喵。\n{summary or result.message}"
        elif replay_case:
            final_reply = f"`analyze_media` 重複卡住，我先停下來了。Replay case: {replay_case.get('name')}"
        else:
            final_reply = f"我想分析剛剛的產物，但失敗了：{result.message}"
            if result.error:
                final_reply += f"\n{result.error[:800]}"
        return self._finish(user_input, final_reply, verification_status=getattr(verification, "status", ""))

    def _continue_task(self, user_input: str) -> OutcomeHandled:
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
                final_reply = (
                    f"好，我接著跑 `{verifier_name}` 驗證喵。\n"
                    f"job: {job.job_id}\n"
                    "我先讓背景 verifier 跑，下一輪我會吸收結果再告訴你。"
                )
            except Exception as exc:
                final_reply = f"我想接著跑驗證，但 verifier 啟動失敗了：{exc}"
        else:
            final_reply = format_last_outcome_reply(self.session_brain)
            final_reply += "\n目前沒有明確下一步。你可以說「發給我」「分析一下」或直接補一句新目標。"
        return self._finish(user_input, final_reply)

    def _finish(self, user_input: str, final_reply: str, **trace_fields: Any) -> OutcomeHandled:
        self.append_reply(user_input, final_reply)
        self.hooks.emit("Stop", session_id=self.session_id, turn_id=self.turn_id_getter(), content_preview=final_reply[:160], **trace_fields)
        return OutcomeHandled(final_reply)
