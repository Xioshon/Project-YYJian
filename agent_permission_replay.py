import os
import json
from dataclasses import dataclass
from typing import Any, Callable

from agent_outcome import tool_result_outcome
from agent_reply_composer import ReplyComposer, ReplyEvent
from agent_self_recovery import self_repair_instruction, should_prompt_self_repair
from agent_user_voice import approved_tool_blocked_reply, approved_tool_error_reply, approved_tool_success_reply, failure_replay_reply
from agent_tool_runtime import is_safe_workspace_media
from core_tools import ToolResult


@dataclass
class PermissionReplayHandled:
    content: str
    reasoning: str = ""

    def to_chat_result(self) -> dict[str, str]:
        return {"content": self.content, "reasoning": self.reasoning}


class PermissionReplayController:
    """Runs an approved pending action exactly once, then repairs bounded failures."""

    def __init__(
        self,
        *,
        permission_manager: Any,
        session_brain: Any,
        executor: Any,
        hooks: Any,
        after_tool_result: Callable[[str, dict, ToolResult], tuple[Any, dict[str, Any] | None]],
        append_user_context: Callable[[str], str],
        append_assistant_reply: Callable[[str], None],
        memory: list[dict[str, Any]] | None = None,
        continue_after_error: Callable[[Callable | None], Any] | None = None,
        recover_tool_result: Callable[[str, dict, ToolResult, Callable | None, Any], tuple[ToolResult, dict[str, Any] | None]] | None = None,
        response_policy: Any = None,
        reset_turn_state: Callable[[], None],
        reply_composer: ReplyComposer | None = None,
        session_id: str,
        turn_id_getter: Callable[[], int],
    ):
        self.permission_manager = permission_manager
        self.session_brain = session_brain
        self.executor = executor
        self.hooks = hooks
        self.after_tool_result = after_tool_result
        self.recover_tool_result = recover_tool_result
        self.append_user_context = append_user_context
        self.append_assistant_reply = append_assistant_reply
        self.memory = memory
        self.continue_after_error = continue_after_error
        self.response_policy = response_policy
        self.reset_turn_state = reset_turn_state
        self.reply_composer = reply_composer or ReplyComposer()
        self.session_id = session_id
        self.turn_id_getter = turn_id_getter

    def maybe_replay(self, grant: str, user_input: str, tool_callback: Callable | None) -> PermissionReplayHandled | None:
        if grant != "single":
            return None
        approved_action = self.permission_manager.pop_approved_action()
        if not approved_action:
            return None

        turn_id = self.turn_id_getter()
        self.append_user_context(user_input)
        self.hooks.emit(
            "PermissionReplay",
            session_id=self.session_id,
            turn_id=turn_id,
            tool=approved_action.tool_name,
            arguments=approved_action.arguments,
        )
        result = self.executor.execute(approved_action.tool_name, approved_action.arguments, tool_callback, None)
        if self.recover_tool_result is not None:
            if result.status == "error" and hasattr(self.permission_manager, "grant_repair_tool"):
                self.permission_manager.grant_repair_tool(approved_action.tool_name, turn_id)
            result, _recovery = self.recover_tool_result(
                approved_action.tool_name,
                approved_action.arguments,
                result,
                tool_callback,
                self.response_policy,
            )
        verification, replay_case = self.after_tool_result(approved_action.tool_name, approved_action.arguments, result)
        self.hooks.emit(
            "PermissionReplayResult",
            session_id=self.session_id,
            turn_id=turn_id,
            tool=approved_action.tool_name,
            status=result.status,
            verification_status=getattr(verification, "status", ""),
        )
        delivery_note = ""
        if result.status == "ok" and not replay_case:
            delivery_note, _delivery_result = self._deliver_safe_artifact_after_success(approved_action.tool_name, result, tool_callback)
        repaired = self._maybe_continue_after_error(approved_action, result, replay_case, tool_callback)
        if repaired is not None:
            return repaired
        final_reply = self._format_replay_reply(approved_action, result, replay_case)
        if delivery_note:
            final_reply += delivery_note
        reply_decision = self.hooks.emit("BeforeReply", session_id=self.session_id, turn_id=turn_id, content_preview=final_reply[:160])
        if reply_decision.annotate:
            final_reply += reply_decision.annotate
        self.append_assistant_reply(final_reply)
        self.reset_turn_state()
        self.hooks.emit("Stop", session_id=self.session_id, turn_id=turn_id, content_preview=final_reply[:160])
        return PermissionReplayHandled(final_reply)

    def _maybe_continue_after_error(self, approved_action: Any, result: ToolResult, replay_case: dict[str, Any] | None, tool_callback: Callable | None) -> PermissionReplayHandled | None:
        if replay_case or result.status != "error" or not self.memory or not self.continue_after_error:
            return None
        if not should_prompt_self_repair(approved_action.tool_name, result, self.response_policy):
            return None
        turn_id = self.turn_id_getter()
        tool_call_id = f"permission_replay_{turn_id}_{approved_action.tool_name}"
        self.memory.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": approved_action.tool_name,
                            "arguments": json.dumps(approved_action.arguments or {}, ensure_ascii=False),
                        },
                    }
                ],
            }
        )
        self.memory.append({"role": "tool", "tool_call_id": tool_call_id, "name": approved_action.tool_name, "content": result.to_text()[:4000]})
        self.memory.append({"role": "system", "content": self_repair_instruction(approved_action.tool_name, approved_action.arguments or {}, result)})
        if hasattr(self.permission_manager, "grant_repair_tool"):
            self.permission_manager.grant_repair_tool(approved_action.tool_name, turn_id)
        self.hooks.emit("PermissionReplaySelfRepair", session_id=self.session_id, turn_id=turn_id, tool=approved_action.tool_name)
        continued = self.continue_after_error(tool_callback)
        return PermissionReplayHandled(getattr(continued, "content", ""), getattr(continued, "reasoning", ""))

    def _format_replay_reply(self, approved_action: Any, result: ToolResult, replay_case: dict[str, Any] | None) -> str:
        tool_name = approved_action.tool_name
        if result.status == "ok":
            self.session_brain.mark_validation_needed(
                "verify tool results: " + tool_name,
                self.turn_id_getter(),
                self.session_id,
                evidence=[tool_name],
            )
            outcome_summary, artifacts = tool_result_outcome(tool_name, result)
            owner_summary = _owner_summary_for_replay_result(tool_name, approved_action.arguments, result, outcome_summary)
            fallback = owner_summary or approved_tool_success_reply(tool_name, result.message, outcome_summary, bool(artifacts))
            return self.reply_composer.compose(
                ReplyEvent(
                    "tool_success",
                    user_input="permission replay approved by owner",
                    tool_name=tool_name,
                    result=result,
                    summary=owner_summary or outcome_summary,
                    artifacts=artifacts,
                    next_action="answer the owner's actual task result",
                ),
                fallback,
            )
        if replay_case:
            return self.reply_composer.compose(
                ReplyEvent(
                    "tool_error",
                    user_input="permission replay failed repeatedly",
                    tool_name=tool_name,
                    result=result,
                    reason="failure replay generated",
                    extra={"replay_name": replay_case.get("name", "")},
                ),
                failure_replay_reply(tool_name, replay_case.get("name", "")),
            )
        if result.status == "blocked":
            if result.requires_permission:
                self.session_brain.mark_permission_needed(tool_name, self.turn_id_getter(), self.session_id)
            return self.reply_composer.compose(
                ReplyEvent("permission_request" if result.requires_permission else "tool_error", tool_name=tool_name, result=result),
                approved_tool_blocked_reply(tool_name, result),
            )
        return self.reply_composer.compose(
            ReplyEvent("tool_error", tool_name=tool_name, result=result, reason="approved tool returned error"),
            approved_tool_error_reply(tool_name, result),
        )

    def _deliver_safe_artifact_after_success(self, tool_name: str, result: ToolResult, tool_callback: Callable | None) -> tuple[str, ToolResult | None]:
        _outcome_summary, artifacts = tool_result_outcome(tool_name, result)
        artifact = _first_safe_media_artifact(artifacts)
        if not artifact:
            return "", None
        args = {"file_path": artifact, "caption": "剛剛的結果喔"}
        delivered = self.executor.execute("send_telegram_media", args, tool_callback, None)
        if self.recover_tool_result is not None:
            delivered, _recovery = self.recover_tool_result("send_telegram_media", args, delivered, tool_callback, self.response_policy)
        self.after_tool_result("send_telegram_media", args, delivered)
        if delivered.status == "ok":
            return f"\n我也順手把 `{os.path.basename(artifact)}` 發給你了。", delivered
        if delivered.requires_permission:
            self.session_brain.mark_permission_needed("send_telegram_media", self.turn_id_getter(), self.session_id)
            return f"\n結果檔案已經生成了，不過發送 `{os.path.basename(artifact)}` 還需要你確認一下。", delivered
        return f"\n結果檔案已經生成了，但我發送時卡住了：{delivered.message}", delivered


def _first_safe_media_artifact(artifacts: list[str]) -> str:
    media_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tgs", ".webm", ".mp4"}
    for artifact in artifacts or []:
        ext = os.path.splitext(artifact)[1].casefold()
        if ext in media_exts and is_safe_workspace_media(artifact):
            return artifact
    return ""


def _owner_summary_for_replay_result(tool_name: str, arguments: dict[str, Any], result: ToolResult, outcome_summary: str) -> str:
    if tool_name != "execute_python" or not isinstance(result.data, dict):
        return ""
    code = str((arguments or {}).get("code") or "").casefold()
    stdout = str(result.data.get("stdout") or "")
    combined = (code + "\n" + stdout).casefold()
    if not any(marker in combined for marker in ["cloudmusic", "spotify", "vlc", "potplayer", "media", "music", "網易", "网易", "音樂", "音乐"]):
        return ""
    if not any(marker in combined for marker in ["mainwindowtitle", "processname", "pid", "tasklist", "get-process", "workingsetmb"]):
        return ""

    title = _extract_media_window_title(stdout)
    process = _extract_media_process_name(stdout, combined)
    lines = ["我查到比較像答案的部分了："]
    if process:
        lines.append(f"- 目前有 `{process}` 相關進程在跑。")
    if title:
        lines.append(f"- 可見播放器窗口標題像是：`{title}`。")
    else:
        lines.append("- 有媒體相關進程，但沒有抓到清楚的播放窗口標題。")
    lines.append("- 所以我的判斷是：你的電腦上確實有媒體/音樂播放器在活動或待命。")
    lines.append("- 但只靠進程和窗口標題，還不能 100% 判斷它此刻是在播放還是暫停；要精準確認，需要再看播放器畫面或音量混音器。")
    lines.append("")
    lines.append("我猜你真正想知道的是：聲音是不是從某個播放器來、要不要幫你切過去或暫停。")
    lines.append("下一步你可以直接說「切到播放器看看」或「幫我暫停音樂」，我就接著做。")
    return "\n".join(lines)


def _extract_media_window_title(stdout: str) -> str:
    candidates: list[str] = []
    for raw_line in (stdout or "").splitlines():
        line = " ".join(raw_line.strip().split())
        if not line or line.startswith("---") or line.startswith("===") or "mainwindowtitle" in line.casefold():
            continue
        if any(marker in line.casefold() for marker in ["mygo", "spotify", "vlc", "potplayer", "music", "cloudmusic"]):
            candidates.append(line)
    if not candidates:
        return ""
    best = candidates[0]
    best = best.replace("True ", "").strip()
    parts = best.split()
    if len(parts) >= 2 and parts[0].isdigit():
        best = " ".join(parts[1:])
    if len(parts) >= 6 and parts[0].isdigit():
        best = " ".join(parts[5:])
    return best[:120]


def _extract_media_process_name(stdout: str, combined: str) -> str:
    for name in ["cloudmusic", "spotify", "vlc", "potplayer", "foobar", "wmplayer"]:
        if name in combined:
            return name
    return ""
