import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

import core_tools
import agent_eval
import main as main_module
from agent_benchmark import TASK_BENCHMARK_FILE, build_default_benchmark
from agent_latency import (
    DEFAULT_MEDIA_CACHE,
    InteractionMode,
    classify_interaction,
    media_type_for,
    policy_for_semantic_intent,
    quick_ack_for,
    response_policy_for,
)
from agent_context import CONTEXT_BUDGET_REPORT_FILE, DEFAULT_CONTEXT_BUILDER
from agent_hooks import DEFAULT_HOOK_MANAGER, HookDecision
from agent_memory import (
    MEMORY_COMPILED_FILE,
    MEMORY_HEALTH_FILE,
    PERSONA_HEALTH_FILE,
    ROLLING_SUMMARY_FILE,
    compile_memory,
    looks_mojibake,
    memory_health_check,
    persona_health_check,
    search_engineering_knowledge,
    update_chat_summary,
)
from agent_outcome import detect_outcome_action, format_last_outcome_reply, is_result_followup
from agent_presence import (
    PRESENCE_CANDIDATES_FILE,
    PRESENCE_DEBUG_FILE,
    PRESENCE_HEALTH_FILE,
    PRESENCE_STATE_FILE,
    PresenceConfig,
    PresenceEngine,
    PresenceMessageCandidate,
    PresenceQualityDecision,
)
from agent_knowledge import (
    KNOWLEDGE_CHUNKS_FILE,
    KNOWLEDGE_INDEX_FILE,
    KNOWLEDGE_MANIFEST_FILE,
    read_knowledge,
    reindex_workspace,
    search_knowledge,
)
from agent_eval import EVAL_REPORT_FILE, PERMISSION_HEALTH_FILE, build_live_eval_report, check_repo_hygiene, check_user_facing_source_health, write_eval_report
from agent_observability import summarize_trace
from agent_protocol import STICKER_MARKER_LABEL, classify_approval, extract_primary_message, screenshot_marker, sticker_marker, sticker_pattern
from agent_runtime_context import build_runtime_context, should_include_task_context
from agent_action_verification import verify_action
from agent_replay import FAILURE_REPLAY_FILE, ReplayCase, ReplayHarness, record_failure_replay
from agent_self_recovery import RepairPlanner, SelfRecoveryController, _extract_command_file_names, _looks_like_screenshot_code, _missing_module_name, diagnose_tool_error, plan_recovery, plan_recovery_candidates
from agent_session import SESSION_BRAIN_FILE, SessionBrain
from agent_skills import DEFAULT_SKILL_REGISTRY
from agent_social import SocialCurationReminder, SocialSessionManager, SocialStickerIndex, infer_intent_tags, infer_metadata_tags, infer_social_mode, infer_sticker_tags, is_safe_sticker, social_reply_policy_for
from agent_planner import DEFAULT_PLANNER
from agent_subagents import BUILTIN_SUBAGENTS, SUBAGENT_RUNS_FILE, get_subagent
from agent_turns import (
    DEFAULT_TURN_DEBOUNCE_SECONDS,
    TURN_DEBOUNCE_ENV,
    InboundMessagePart,
    MessageCoalescer,
    build_aggregated_turn,
    build_turn_prompt,
    configured_turn_debounce_seconds,
)
from agent_short_context import ShortContextBuffer, build_context_for_turn
from agent_url_context import URLContextCache, classify_url_platform, inspect_url, parse_douyin_metadata, parse_html_metadata, should_preview_url, url_cache_key
from agent_verification import DEFAULT_VERIFICATION_PLANNER
from agent_transactions import TASK_TRANSACTIONS_FILE, TaskTransactionManager
from agent_task_graph import TASK_GRAPHS_FILE, WORKFLOW_REPLAY_FILE, TaskGraphManager
from agent_worker import ALLOWED_VERIFIER_COMMANDS, WORKER_JOBS_FILE, WORKER_RESULTS_FILE, WorkerJob, WorkerQueue, VerifierWorker
from core_agent import CompanionAgent, SiliconFlowAdapter, TRACE_LOG_FILE, clean_assistant_output
import agent_llm
from agent_llm import RoutedLLMAdapter, infer_route_from_messages
from main import TelegramGateway, _dedupe_preserve_order, _prompt_mode_for_seed, _split_sticker_command_payload, build_system_prompt, find_sticker_file


SELF_TEST_LOCK_FILE = os.path.join(core_tools.PROJECT_CACHE_DIR, "self_test.lock")
SELF_TEST_LOCK_STALE_SECONDS = 6 * 60 * 60
_task_plan_backup = None
_memory_backup = None
_session_brain_backup = None
_transactions_backup = None
_failure_replay_backup = None
_rolling_summary_backup = None
_memory_compiled_backup = None
_memory_health_backup = None
_knowledge_manifest_backup = None
_knowledge_chunks_backup = None
_knowledge_index_backup = None
_eval_report_backup = None
_self_test_lock_fd = None
_self_test_lock_path = ""
_task_benchmark_backup = None
_task_graphs_backup = None
_workflow_replay_backup = None
_worker_jobs_backup = None
_worker_results_backup = None
_context_budget_report_backup = None
_subagent_runs_backup = None
_trace_log_backup = None
_presence_state_backup = None
_presence_candidates_backup = None
_presence_health_backup = None
_presence_debug_backup = None


def acquire_self_test_lock(path: str = SELF_TEST_LOCK_FILE, stale_seconds: int = SELF_TEST_LOCK_STALE_SECONDS) -> bool:
    global _self_test_lock_fd, _self_test_lock_path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags)
    except FileExistsError:
        try:
            age = time.time() - os.path.getmtime(path)
        except OSError:
            age = 0
        if age > stale_seconds:
            try:
                os.remove(path)
            except OSError:
                return False
            return acquire_self_test_lock(path, stale_seconds)
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(json.dumps({"pid": os.getpid(), "created_at": time.time(), "path": path}, ensure_ascii=False))
        file.write("\n")
    if os.path.abspath(path) == os.path.abspath(SELF_TEST_LOCK_FILE):
        _self_test_lock_fd = fd
        _self_test_lock_path = path
    return True


def release_self_test_lock(path: str | None = None) -> None:
    global _self_test_lock_fd, _self_test_lock_path
    target = path or _self_test_lock_path
    if target:
        try:
            os.remove(target)
        except FileNotFoundError:
            pass
        except OSError:
            pass
    if not path or os.path.abspath(path) == os.path.abspath(_self_test_lock_path or ""):
        _self_test_lock_fd = None
        _self_test_lock_path = ""


def result_text(value):
    if hasattr(value, "to_text"):
        return value.to_text()
    return str(value)


def check(name, fn):
    try:
        result = fn()
        print(f"[OK] {name}: {str(result).replace(chr(10), ' ')[:240]}")
        return True
    except Exception as exc:
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        return False


def validate_tool_schemas():
    names = set()
    for tool in core_tools.ALL_TOOLS:
        if tool.name in names:
            raise AssertionError(f"duplicate tool: {tool.name}")
        names.add(tool.name)
        json.dumps(tool.parameters)
        signature = inspect.signature(tool.func)
        required = set(tool.parameters.get("required", []))
        missing = [
            name
            for name, parameter in signature.parameters.items()
            if parameter.default is inspect._empty and name not in required
        ]
        if missing:
            raise AssertionError(f"{tool.name} missing required schema fields: {missing}")
    if "react_to_message" not in names:
        raise AssertionError("react_to_message was not registered")
    return f"{len(core_tools.ALL_TOOLS)} tools validated"


def protocol_constants_are_unicode_safe():
    if STICKER_MARKER_LABEL != "表情包":
        raise AssertionError(repr(STICKER_MARKER_LABEL))
    if classify_approval("可以", True) != "single":
        raise AssertionError("Chinese single approval failed")
    if classify_approval("本輪允許", True) != "turn":
        raise AssertionError("Chinese turn approval failed")
    if classify_approval("本轮允许", True) != "turn":
        raise AssertionError("Simplified Chinese turn approval failed")
    if classify_approval("嗯，继续吧", True) != "single":
        raise AssertionError("natural-language approval failed")
    if classify_approval("嗯，继续吧", False) != "none":
        raise AssertionError("approval must not trigger without pending permission")
    wrapped = "主人主要訊息：本轮允许\n\n短期聊天上下文：\n- topic=可以 | last_reply=被攔住了"
    if classify_approval(wrapped, True) != "turn":
        raise AssertionError("wrapped Telegram turn approval failed")
    wrapped_single = "主人主要訊息：嗯，继续吧\n\n短期聊天上下文：\n- topic=普通聊天"
    if classify_approval(wrapped_single, True) != "single":
        raise AssertionError("wrapped natural-language approval failed")
    if classify_approval("allow all", True) != "turn":
        raise AssertionError("ASCII turn approval failed")
    marker = sticker_marker("x.png")
    if not sticker_pattern().findall(marker):
        raise AssertionError(marker)
    if "琛" in marker or "鍙" in "".join(["可以", "本輪允許"]):
        raise AssertionError("mojibake leaked into protocol marker")
    return marker


def init_agent():
    agent = CompanionAgent(SiliconFlowAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "self_test_history.json"))
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    if len(agent.tools) != len(core_tools.ALL_TOOLS):
        raise AssertionError("not all tools were registered")
    return f"{len(agent.tools)} tools registered"


class UnknownToolAdapter:
    def __init__(self):
        self.calls = 0

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_unknown", "name": "old_missing_tool", "arguments": {}, "raw_arguments": "{}"}],
            }
        return {"role": "assistant", "content": "unknown handled"}


def unknown_tool_fallback():
    agent = CompanionAgent(UnknownToolAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "unknown_tool_test.json"))
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    result = agent.chat("trigger old tool")
    if result["content"] != "unknown handled":
        raise AssertionError(result)
    tool_messages = [m for m in agent.memory if m.get("role") == "tool"]
    if not tool_messages or "Unknown tool" not in tool_messages[-1].get("content", ""):
        raise AssertionError("unknown tool result was not recorded")
    return "unknown tool call produced a valid tool response"


class PermissionAdapter:
    def __init__(self):
        self.calls = 0
        self.args = {"filename": "project_cache/permission_test.txt", "content": "allowed"}

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls in (1, 3):
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"call_write_{self.calls}", "name": "write_file", "arguments": self.args, "raw_arguments": json.dumps(self.args)}],
            }
        if self.calls == 2:
            return {"role": "assistant", "content": "需要權限，可以嗎？"}
        return {"role": "assistant", "content": "permission handled"}


def permission_followup_allows_exact_tool():
    target = os.path.join(core_tools.PROJECT_CACHE_DIR, "permission_test.txt")
    try:
        os.remove(target)
    except FileNotFoundError:
        pass
    agent = CompanionAgent(PermissionAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "permission_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    first = agent.chat("write a file")
    if "可以" not in first["content"] and "確認" not in first["content"]:
        raise AssertionError(first)
    if os.path.exists(target):
        raise AssertionError("file was written before permission")
    second = agent.chat("可以")
    if _contains_internal_policy_leak(second["content"]) or "replay case" in second["content"].casefold():
        raise AssertionError(second)
    if not os.path.exists(target):
        raise AssertionError("file was not written after permission")
    if agent.llm.calls != 1:
        raise AssertionError(f"approval should replay pending action without another LLM call; calls={agent.llm.calls}")
    return "single approval replayed the pending exact tool"


class PermissionReplayPythonAdapter:
    def __init__(self):
        self.calls = 0
        self.args = {"code": "print('approved python replay')"}

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_py", "name": "execute_python", "arguments": self.args, "raw_arguments": json.dumps(self.args)}],
            }
        if self.calls == 2:
            return {"role": "assistant", "content": "需要權限，可以嗎？"}
        return {"role": "assistant", "content": "unexpected replanning"}


class PermissionReplayMediaStatusAdapter:
    def __init__(self):
        self.calls = 0
        self.args = {
            "code": (
                "import subprocess\n"
                "subprocess.check_output(['powershell','-NoProfile','-Command',"
                "'Get-Process cloudmusic | Select-Object Id,MainWindowTitle'])\n"
            ),
            "timeout": 15,
        }

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_media_status", "name": "execute_python", "arguments": self.args, "raw_arguments": json.dumps(self.args)}],
            }
        return {"role": "assistant", "content": "unexpected replanning"}


class PermissionReplayRepairAdapter:
    def __init__(self):
        self.calls = 0
        self.saw_repair_prompt = False

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            args = {"code": "raise RuntimeError('boom before fallback')"}
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_py_runtime_error", "name": "execute_python", "arguments": args, "raw_arguments": json.dumps(args)}],
            }
        tool_text = "\n".join(message.get("content", "") for message in messages if message.get("role") == "tool")
        if "fallback screenshot ok" in tool_text or '"status": "ok"' in tool_text:
            return {"role": "assistant", "content": "我自己換了 fallback 截圖方式，已經跑通了"}
        system_text = "\n".join(message.get("content", "") for message in messages if message.get("role") == "system")
        if "[SelfRepair]" in system_text:
            self.saw_repair_prompt = True
            args = {"code": "print('fallback screenshot ok')"}
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_py_fallback", "name": "execute_python", "arguments": args, "raw_arguments": json.dumps(args)}],
            }
        if "requires_permission" in tool_text or '"status": "blocked"' in tool_text:
            return {"role": "assistant", "content": "需要你確認一下權限喔，可以嗎？"}
        return {"role": "assistant", "content": "沒有進入 replay repair"}


class PermissionReplayTransientAdapter:
    def __init__(self):
        self.calls = 0
        self.args = {"query": "telegram send"}

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_flaky", "name": "fake_flaky_search", "arguments": self.args, "raw_arguments": json.dumps(self.args)}],
            }
        tool_text = "\n".join(message.get("content", "") for message in messages if message.get("role") == "tool")
        if "requires_permission" in tool_text or '"status": "blocked"' in tool_text:
            return {"role": "assistant", "content": "這一步需要你確認一下，可以嗎？"}
        return {"role": "assistant", "content": "unexpected replanning"}


def permission_replay_bypasses_chat_route_policy():
    agent = CompanionAgent(PermissionReplayPythonAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "permission_python_route_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    first = agent.chat("run python", response_policy=response_policy_for(InteractionMode.TOOL_TASK))
    if "可以" not in first["content"] and "確認" not in first["content"] and "requires approval" not in first["content"]:
        raise AssertionError(first)
    second = agent.chat("好", response_policy=response_policy_for(InteractionMode.CHAT))
    if "approved python replay" not in second["content"]:
        raise AssertionError("permission replay did not surface stdout")
    if _contains_internal_policy_leak(second["content"]):
        raise AssertionError(second)
    if agent.llm.calls != 1:
        raise AssertionError(f"approval should replay without replanning; calls={agent.llm.calls}")
    return "approved pending python replay bypassed chat route policy"


def permission_replay_delivers_safe_artifact_after_success():
    artifact = os.path.join(core_tools.PROJECT_CACHE_DIR, "permission_replay_auto_artifact.png")
    with open(artifact, "wb") as file:
        file.write(b"\x89PNG\r\n\x1a\n")
    sent: list[str] = []

    def fake_execute_python(code: str, timeout: int = 30):
        return core_tools.ToolResult("ok", "Python completed.", data={"returncode": 0, "stdout": artifact, "stderr": ""})

    def fake_send_telegram_media(file_path: str, caption: str = ""):
        sent.append(os.path.abspath(file_path))
        return core_tools.ToolResult("ok", "fake media sent", data={"file_path": file_path, "caption": caption})

    agent = CompanionAgent(PermissionReplayPythonAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "permission_replay_artifact_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.add_tool(core_tools.AgentTool("execute_python", "fake python", fake_execute_python, {"type": "object", "properties": {}}, True))
    agent.add_tool(core_tools.AgentTool("send_telegram_media", "fake send", fake_send_telegram_media, {"type": "object", "properties": {}}))

    first = agent.chat("幫我截圖", response_policy=response_policy_for(InteractionMode.TOOL_TASK))
    if "可以" not in first["content"] and "確認" not in first["content"]:
        raise AssertionError(first)
    second = agent.chat("可以", response_policy=response_policy_for(InteractionMode.CHAT))
    if not sent or sent[-1] != os.path.abspath(artifact):
        raise AssertionError({"reply": second, "sent": sent})
    if "順手" not in second["content"] or "permission_replay_auto_artifact.png" not in second["content"]:
        raise AssertionError(second)
    if agent.llm.calls != 1:
        raise AssertionError(f"approval should replay and deliver without replanning; calls={agent.llm.calls}")
    return "approved replay delivered generated screenshot artifact"


def permission_replay_recovers_transient_artifact_delivery():
    artifact = os.path.join(core_tools.PROJECT_CACHE_DIR, "permission_replay_transient_artifact.png")
    with open(artifact, "wb") as file:
        file.write(b"\x89PNG\r\n\x1a\n")
    attempts = {"send": 0}

    def fake_execute_python(code: str, timeout: int = 30):
        return core_tools.ToolResult("ok", "Python completed.", data={"returncode": 0, "stdout": artifact, "stderr": ""})

    def fake_send_telegram_media(file_path: str, caption: str = ""):
        attempts["send"] += 1
        if attempts["send"] == 1:
            return core_tools.ToolResult("error", "Connection aborted.", error="ConnectionResetError(10054)")
        return core_tools.ToolResult("ok", "fake media sent after retry", data={"file_path": file_path, "caption": caption})

    agent = CompanionAgent(PermissionReplayPythonAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "permission_replay_delivery_recovery_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.add_tool(core_tools.AgentTool("execute_python", "fake python", fake_execute_python, {"type": "object", "properties": {}}, True))
    agent.add_tool(core_tools.AgentTool("send_telegram_media", "fake send", fake_send_telegram_media, {"type": "object", "properties": {}}))

    first = agent.chat("幫我截圖", response_policy=response_policy_for(InteractionMode.TOOL_TASK))
    if "可以" not in first["content"] and "確認" not in first["content"]:
        raise AssertionError(first)
    second = agent.chat("可以", response_policy=response_policy_for(InteractionMode.CHAT))
    if attempts["send"] != 2:
        raise AssertionError({"attempts": attempts, "reply": second})
    if "順手" not in second["content"] or "permission_replay_transient_artifact.png" not in second["content"]:
        raise AssertionError(second)
    if agent.llm.calls != 1:
        raise AssertionError(f"approval should replay, recover delivery, and avoid replanning; calls={agent.llm.calls}")
    return "approved replay recovered transient artifact delivery"


def permission_replay_recovers_transient_error_before_reply():
    attempts = {"count": 0}

    def flaky_search(query: str):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return core_tools.ToolResult("error", "Connection aborted.", error="ConnectionResetError(10054)")
        return core_tools.ToolResult("ok", "Search completed.", data=["recovered result"])

    agent = CompanionAgent(PermissionReplayTransientAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "permission_replay_transient_test.json"))
    agent.interactive_mode = False
    agent.add_tool(core_tools.AgentTool("fake_flaky_search", "fake flaky guarded search", flaky_search, {"type": "object", "properties": {"query": {"type": "string"}}}, True))
    agent.self_recovery = SelfRecoveryController(executor=agent.executor, hooks=agent.hooks, session_id=agent.session_id)
    agent.self_recovery._can_retry_exactly = lambda tool_name, arguments: tool_name == "fake_flaky_search"
    first = agent.chat("幫我跑一個會暫時斷線的任務", response_policy=response_policy_for(InteractionMode.TOOL_TASK))
    if "可以" not in first["content"] and "requires approval" not in first["content"]:
        raise AssertionError(first)
    calls_before_approval = agent.llm.calls
    second = agent.chat("可以", response_policy=response_policy_for(InteractionMode.CHAT))
    if attempts["count"] != 2:
        raise AssertionError(f"expected replay plus automatic retry, got {attempts['count']}")
    if "Search completed" not in second["content"]:
        raise AssertionError(second)
    if agent.llm.calls != calls_before_approval:
        raise AssertionError(f"approval replay should not ask the model to replan; calls={agent.llm.calls}")
    return "permission replay recovered transient error before replying"


def permission_replay_summarizes_media_status_instead_of_raw_stdout():
    def fake_execute_python(code: str, timeout: int = 30):
        stdout = (
            "=== cloudmusic window title ===\n"
            "Id MainWindowTitle\n"
            "-- ---------------\n"
            "22256 靜降想? - MyGO!!!!!\n"
            "\n"
            "=== cloudmusic process info ===\n"
            "PID CPU WorkingSetMB Responding MainWindowTitle\n"
            "22256 5117.4 105 True 靜降想? - MyGO!!!!!\n"
        )
        return core_tools.ToolResult("ok", "Python completed.", data={"returncode": 0, "stdout": stdout, "stderr": ""})

    agent = CompanionAgent(PermissionReplayMediaStatusAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "permission_replay_media_status_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.add_tool(core_tools.AgentTool("execute_python", "fake python", fake_execute_python, {"type": "object", "properties": {}}, True))
    first = agent.chat("幫我用 Python 看一下現在我的電腦有沒有在播放媒體或者音樂", response_policy=response_policy_for(InteractionMode.TOOL_TASK))
    if "可以" not in first["content"] and "確認" not in first["content"]:
        raise AssertionError(first)
    second = agent.chat("可以", response_policy=response_policy_for(InteractionMode.CHAT))
    content = second["content"]
    required = ["我查到", "cloudmusic", "MyGO", "不能 100% 判斷", "我猜你真正想知道"]
    missing = [item for item in required if item not in content]
    if missing:
        raise AssertionError({"missing": missing, "content": content})
    if "stdout:" in content or "returncode:" in content:
        raise AssertionError(content)
    if agent.llm.calls != 1:
        raise AssertionError(f"approval should replay without replanning; calls={agent.llm.calls}")
    return "permission replay summarized media status"


def permission_replay_failed_python_enters_self_repair_loop():
    attempts = {"count": 0}

    def fake_python(code: str, timeout: int = 30):
        attempts["count"] += 1
        if "boom before fallback" in code:
            return core_tools.ToolResult(
                "error",
                "Python failed.",
                data={"returncode": 1, "stdout": "", "stderr": "RuntimeError: boom before fallback"},
                error="RuntimeError: boom before fallback",
            )
        return core_tools.ToolResult("ok", "Python completed.", data={"returncode": 0, "stdout": "fallback screenshot ok", "stderr": ""})

    agent = CompanionAgent(PermissionReplayRepairAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "permission_replay_repair_test.json"))
    agent.interactive_mode = False
    agent.add_tool(core_tools.AgentTool("execute_python", "fake python", fake_python, {"type": "object", "properties": {"code": {"type": "string"}, "timeout": {"type": "integer"}}}, True))
    first = agent.chat("請截圖看看", response_policy=response_policy_for(InteractionMode.TOOL_TASK))
    if "可以" not in first["content"] and "requires approval" not in first["content"]:
        raise AssertionError(first)
    second = agent.chat("可以", response_policy=response_policy_for(InteractionMode.CHAT))
    if "fallback" not in second["content"] and "跑通" not in second["content"]:
        raise AssertionError(second)
    if attempts["count"] != 2:
        raise AssertionError(f"expected failed replay plus one repair attempt, got {attempts['count']}")
    if not agent.llm.saw_repair_prompt:
        raise AssertionError("replay failure did not present self-repair prompt")
    if any("[SelfRepair]" in message.get("content", "") for message in agent.memory if message.get("role") == "system"):
        raise AssertionError("transient self-repair leaked into memory")
    return "failed permission replay entered bounded self-repair loop"


def permission_replay_lives_in_controller_not_core_loop():
    import inspect
    import core_agent as core_agent_module
    from agent_permission_replay import PermissionReplayController

    chat_source = inspect.getsource(core_agent_module.CompanionAgent.chat)
    controller_source = inspect.getsource(PermissionReplayController)
    if "self.hooks.emit(\n                    \"PermissionReplay\"" in chat_source or "pop_approved_action()" in chat_source:
        raise AssertionError("permission replay flow leaked back into CompanionAgent.chat")
    if "PermissionReplay" not in controller_source or "pop_approved_action" not in controller_source:
        raise AssertionError("permission replay controller is missing replay responsibilities")
    return "permission replay flow isolated in controller"


def tool_loop_lives_in_controller_not_core_loop():
    import inspect
    import core_agent as core_agent_module
    from agent_tool_loop import ToolLoopController

    chat_source = inspect.getsource(core_agent_module.CompanionAgent.chat)
    controller_source = inspect.getsource(ToolLoopController)
    forbidden = ["llm.chat_with_tools", "repeat_state", "Repeated tool call stopped", "failsafe"]
    leaked = [marker for marker in forbidden if marker in chat_source]
    if leaked:
        raise AssertionError(f"tool loop leaked back into CompanionAgent.chat: {leaked}")
    required = ["llm.chat_with_tools", "repeat_state", "Repeated tool call stopped", "failsafe"]
    missing = [marker for marker in required if marker not in controller_source]
    if missing:
        raise AssertionError(f"tool loop controller missing responsibilities: {missing}")
    return "tool loop flow isolated in controller"


def tool_runtime_services_are_outside_core_agent():
    import inspect
    import core_agent as core_agent_module
    import agent_tool_runtime

    core_source = inspect.getsource(core_agent_module)
    runtime_source = inspect.getsource(agent_tool_runtime)
    forbidden = ["class PermissionManager", "class ToolExecutor", "class ToolRegistry", "LOW_RISK_TOOLS", "PERMISSION_BUNDLES"]
    leaked = [marker for marker in forbidden if marker in core_source]
    if leaked:
        raise AssertionError(f"tool runtime leaked back into core_agent.py: {leaked}")
    missing = [marker for marker in forbidden if marker not in runtime_source]
    if missing:
        raise AssertionError(f"agent_tool_runtime.py missing service responsibilities: {missing}")
    return "tool runtime services isolated"


def llm_adapter_lives_outside_core_agent():
    import inspect
    import core_agent as core_agent_module

    core_source = inspect.getsource(core_agent_module)
    llm_source = inspect.getsource(agent_llm)
    forbidden = ["from openai import OpenAI", "import httpx", "chat.completions.create", "base_url=\"https://api.siliconflow.cn/v1\"", "class SiliconFlowAdapter"]
    leaked = [marker for marker in forbidden if marker in core_source]
    if leaked:
        raise AssertionError(f"LLM provider leaked back into core_agent.py: {leaked}")
    required = ["class SiliconFlowAdapter", "chat.completions.create", "format_tools_for_openai", "add_runtime_guardrail"]
    missing = [marker for marker in required if marker not in llm_source]
    if missing:
        raise AssertionError(f"agent_llm.py missing adapter responsibilities: {missing}")
    if SiliconFlowAdapter is not agent_llm.SiliconFlowAdapter:
        raise AssertionError("core_agent compatibility export should point to agent_llm.SiliconFlowAdapter")
    return "LLM adapter isolated"


def routed_llm_adapter_selects_fast_chat_and_strong_task_models():
    adapter = RoutedLLMAdapter(chat_model="fast-chat", task_model="strong-task", vision_model="vision-task", api_key="test-key")
    if adapter.model_for_route("chat") != "fast-chat":
        raise AssertionError("chat route should use fast chat model")
    if adapter.model_for_route("social_sticker") != "fast-chat":
        raise AssertionError("social route should use fast chat model")
    if adapter.model_for_route("tool_task") != "strong-task":
        raise AssertionError("tool route should use strong task model")
    if adapter.model_for_route("screen_observe") != "vision-task":
        raise AssertionError("screen route should use vision model")
    messages = [{"role": "user", "content": "hi\n\n[目前任務筆記]\nstate\nintent: task_continuation"}]
    if infer_route_from_messages(messages) != "task_continuation":
        raise AssertionError("task intent route not inferred")
    return "routed model policy selected expected models"


def main_build_agent_uses_routed_llm_adapter():
    source = inspect.getsource(main_module.build_agent)
    if "RoutedLLMAdapter" not in source or "SiliconFlowAdapter" in source:
        raise AssertionError(source)
    return "main build_agent uses routed adapter"


def task_result_followup_uses_last_outcome_without_replanning():
    agent = CompanionAgent(PermissionReplayPythonAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "task_result_followup_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.chat("run python", response_policy=response_policy_for(InteractionMode.TOOL_TASK))
    replay = agent.chat("好", response_policy=response_policy_for(InteractionMode.CHAT))
    if "approved python replay" not in replay["content"]:
        raise AssertionError(replay)
    before_calls = agent.llm.calls
    status = agent.chat("有結果嗎", response_policy=response_policy_for(InteractionMode.CHAT))
    if "approved python replay" not in status["content"] or "execute_python" not in status["content"]:
        raise AssertionError(status)
    if agent.llm.calls != before_calls:
        raise AssertionError("result follow-up should use stored outcome without LLM replanning")
    return "result follow-up used stored tool outcome"


def outcome_context_handles_result_followup_even_when_classified_chat():
    agent = CompanionAgent(NoReplanAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "outcome_context_chat_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.session_brain.state.state = "idle"
    agent.session_brain.state.last_tool = "execute_python"
    agent.session_brain.state.last_tool_status = "ok"
    agent.session_brain.state.last_tool_summary = "execute_python: ok - Python completed.\nstdout:\nmade a screenshot"
    result = agent.chat("有結果嗎", response_policy=response_policy_for(InteractionMode.CHAT))
    if "execute_python" not in result["content"] or "made a screenshot" not in result["content"]:
        raise AssertionError(result)
    if agent.llm.calls:
        raise AssertionError("outcome context follow-up should not call LLM even if classified as chat")
    return "outcome context bypassed chat route for result follow-up"


class NoReplanAdapter:
    def __init__(self):
        self.calls = 0

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        raise AssertionError("outcome continuation should not call LLM")


def _agent_with_last_artifact(filename: str = "outcome_artifact.png"):
    artifact = os.path.join(core_tools.PROJECT_CACHE_DIR, filename)
    with open(artifact, "wb") as file:
        file.write(b"\x89PNG\r\n\x1a\n")
    agent = CompanionAgent(NoReplanAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, f"artifact_outcome_test_{filename}.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.session_brain.state.state = "awaiting_validation"
    agent.session_brain.state.pending_validation = ["verify tool results: execute_python"]
    agent.session_brain.state.last_tool = "execute_python"
    agent.session_brain.state.last_tool_status = "ok"
    agent.session_brain.state.last_tool_summary = "execute_python: ok - Python completed.\nstdout:\nmade a screenshot"
    agent.session_brain.state.last_artifacts = [artifact]
    return agent, artifact


def outcome_send_artifact_uses_stored_artifact_without_replanning():
    agent, artifact = _agent_with_last_artifact("send_me_artifact.png")

    def fake_send_telegram_media(file_path, caption=""):
        if os.path.abspath(file_path) != os.path.abspath(artifact):
            raise AssertionError(file_path)
        return core_tools.ToolResult("ok", "fake media sent", data={"file": file_path})

    agent.add_tool(core_tools.AgentTool("send_telegram_media", "fake send", fake_send_telegram_media, {"type": "object", "properties": {}}))
    result = agent.chat("發給我", response_policy=response_policy_for(InteractionMode.CHAT))
    if "send_me_artifact.png" not in result["content"]:
        raise AssertionError(result)
    if agent.llm.calls:
        raise AssertionError("send artifact should not replan")
    return "stored artifact sent without replanning"


def outcome_send_artifact_recovers_transient_error_without_replanning():
    agent, artifact = _agent_with_last_artifact("send_me_after_retry.png")
    attempts = {"send": 0}

    def fake_send_telegram_media(file_path, caption=""):
        if os.path.abspath(file_path) != os.path.abspath(artifact):
            raise AssertionError(file_path)
        attempts["send"] += 1
        if attempts["send"] == 1:
            return core_tools.ToolResult("error", "Connection aborted.", error="ConnectionResetError(10054)")
        return core_tools.ToolResult("ok", "fake media sent after retry", data={"file": file_path})

    agent.add_tool(core_tools.AgentTool("send_telegram_media", "fake send", fake_send_telegram_media, {"type": "object", "properties": {}}))
    result = agent.chat("發給我", response_policy=response_policy_for(InteractionMode.CHAT))
    if attempts["send"] != 2:
        raise AssertionError({"attempts": attempts, "reply": result})
    if "send_me_after_retry.png" not in result["content"]:
        raise AssertionError(result)
    if agent.llm.calls:
        raise AssertionError("send artifact recovery should not replan")
    return "stored artifact send recovered transient error without replanning"


def outcome_continue_retries_last_safe_failed_step_without_replanning():
    artifact = os.path.join(core_tools.PROJECT_CACHE_DIR, "retry_last_safe_step.png")
    with open(artifact, "wb") as file:
        file.write(b"\x89PNG\r\n\x1a\n")
    attempts = {"send": 0}

    def fake_send_telegram_media(file_path, caption=""):
        if os.path.abspath(file_path) != os.path.abspath(artifact):
            raise AssertionError(file_path)
        attempts["send"] += 1
        return core_tools.ToolResult("ok", "fake media sent after owner retry", data={"file": file_path})

    agent = CompanionAgent(NoReplanAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "outcome_safe_retry_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.add_tool(core_tools.AgentTool("send_telegram_media", "fake send", fake_send_telegram_media, {"type": "object", "properties": {}}))
    graph_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "outcome_safe_retry_graph_test.json")
    try:
        os.remove(graph_path)
    except FileNotFoundError:
        pass
    agent.task_graphs = TaskGraphManager(graph_path)
    failed = core_tools.ToolResult("error", "Connection aborted.", error="ConnectionResetError(10054)")
    verification = verify_action("send_telegram_media", {"file_path": artifact, "caption": "剛剛的結果喔"}, failed, "self_test", 1)
    agent.task_graphs.record_tool_result("send_telegram_media", {"file_path": artifact, "caption": "剛剛的結果喔"}, failed, verification, "self_test", 1, objective="send stored artifact")
    agent.session_brain.state.state = "awaiting_validation"
    agent.session_brain.state.last_tool = "send_telegram_media"
    agent.session_brain.state.last_tool_status = "error"
    agent.session_brain.state.verification_plan = []

    result = agent.chat("再試一次", response_policy=response_policy_for(InteractionMode.CHAT))
    if attempts["send"] != 1:
        raise AssertionError({"attempts": attempts, "reply": result})
    if "retry_last_safe_step.png" not in result["content"] and "fake media sent" not in result["content"]:
        raise AssertionError(result)
    if agent.llm.calls:
        raise AssertionError("safe outcome retry should not call LLM")
    return "continue retried last safe failed step without replanning"


def outcome_retry_records_result_on_original_failed_graph():
    artifact = os.path.join(core_tools.PROJECT_CACHE_DIR, "retry_last_safe_step_original_graph.png")
    with open(artifact, "wb") as file:
        file.write(b"\x89PNG\r\n\x1a\n")
    attempts = {"send": 0}

    def fake_send_telegram_media(file_path, caption=""):
        attempts["send"] += 1
        return core_tools.ToolResult("ok", "fake media sent on retry", data={"file": file_path})

    agent = CompanionAgent(NoReplanAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "outcome_original_graph_retry_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.add_tool(core_tools.AgentTool("send_telegram_media", "fake send", fake_send_telegram_media, {"type": "object", "properties": {}}))
    graph_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "outcome_original_graph_retry_test.json")
    try:
        os.remove(graph_path)
    except FileNotFoundError:
        pass
    agent.task_graphs = TaskGraphManager(graph_path)
    failed = core_tools.ToolResult("error", "Connection aborted.", error="ConnectionResetError(10054)")
    failed_args = {"file_path": artifact, "caption": "剛剛的結果喔"}
    verification = verify_action("send_telegram_media", failed_args, failed, "self_test", 1)
    failed_graph = agent.task_graphs.record_tool_result("send_telegram_media", failed_args, failed, verification, "self_test", 1, objective="send stored artifact")
    newer_graph = agent.task_graphs.start_or_resume("new unrelated task", "self_test", 2)
    if newer_graph.task_id == failed_graph.task_id:
        raise AssertionError("test setup did not create a separate active graph")
    agent.session_brain.state.state = "awaiting_validation"
    agent.session_brain.state.last_tool = "send_telegram_media"
    agent.session_brain.state.last_tool_status = "error"
    agent.session_brain.state.verification_plan = []

    result = agent.chat("再試一次", response_policy=response_policy_for(InteractionMode.CHAT))
    if attempts["send"] != 1 or ("retry_last_safe_step_original_graph.png" not in result["content"] and "fake media sent" not in result["content"]):
        raise AssertionError({"attempts": attempts, "reply": result})
    updated_failed_graph = next(graph for graph in agent.task_graphs.graphs if graph.task_id == failed_graph.task_id)
    still_new_graph = next(graph for graph in agent.task_graphs.graphs if graph.task_id == newer_graph.task_id)
    failed_step = updated_failed_graph.current_step()
    if not failed_step or failed_step.result_status != "ok" or failed_step.status not in {"done", "verified"}:
        raise AssertionError({"failed_graph": updated_failed_graph, "reply": result})
    if still_new_graph.steps:
        raise AssertionError("retry result was recorded on the newer graph")
    if agent.llm.calls:
        raise AssertionError("safe outcome retry should not call LLM")
    return "outcome retry result stayed on original failed graph"


def outcome_analyze_artifact_uses_stored_artifact_without_replanning():
    agent, artifact = _agent_with_last_artifact("analyze_me_artifact.png")

    def fake_analyze_media(file_path, prompt=""):
        if os.path.abspath(file_path) != os.path.abspath(artifact):
            raise AssertionError(file_path)
        return core_tools.ToolResult("ok", "fake analysis complete", data={"summary": "這是一張測試截圖。"})

    agent.add_tool(core_tools.AgentTool("analyze_media", "fake analyze", fake_analyze_media, {"type": "object", "properties": {}}))
    result = agent.chat("分析一下", response_policy=response_policy_for(InteractionMode.CHAT))
    if "測試截圖" not in result["content"]:
        raise AssertionError(result)
    if agent.llm.calls:
        raise AssertionError("analyze artifact should not replan")
    return "stored artifact analyzed without replanning"


def outcome_action_without_artifact_is_clear():
    agent = CompanionAgent(NoReplanAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "no_artifact.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.session_brain.state.state = "awaiting_validation"
    agent.session_brain.state.last_tool = "execute_python"
    agent.session_brain.state.last_tool_status = "ok"
    agent.session_brain.state.last_artifacts = []
    agent.session_brain.state.pending_validation = ["verify tool results: execute_python"]
    result = agent.chat("發給我", response_policy=response_policy_for(InteractionMode.CHAT))
    if "沒有找到" not in result["content"] or "結果" not in result["content"]:
        raise AssertionError(result)
    if agent.llm.calls:
        raise AssertionError("missing artifact should not replan")
    return "missing artifact continuation was clear"


def outcome_intent_helpers_are_deterministic_and_bounded():
    if detect_outcome_action("發給我") != "send_artifact":
        raise AssertionError("send artifact intent not detected")
    if detect_outcome_action("分析一下") != "analyze_artifact":
        raise AssertionError("analyze artifact intent not detected")
    if detect_outcome_action("繼續") != "continue_task":
        raise AssertionError("continue intent not detected")
    if detect_outcome_action("發給我" + "，但是" * 80):
        raise AssertionError("long mixed text should not be hijacked by outcome controller")
    if not is_result_followup("有結果嗎"):
        raise AssertionError("result followup not detected")
    agent, _artifact = _agent_with_last_artifact("outcome_format_artifact.png")
    reply = format_last_outcome_reply(agent.session_brain)
    if "execute_python" not in reply or "outcome_format_artifact.png" not in reply:
        raise AssertionError(reply)
    return "outcome helper boundary held"


def unicode_intent_routing_uses_real_chinese_not_mojibake():
    samples = {
        "result_followup": "\u6709\u7d50\u679c\u55ce",
        "continue": "\u7e7c\u7e8c",
        "send_artifact": "\u767c\u7d66\u6211",
        "analyze": "\u5206\u6790\u4e00\u4e0b",
        "screen": "\u5e6b\u6211\u622a\u53d6\u96fb\u8166\u5c4f\u5e55\u7684\u756b\u9762",
        "tool": "\u5e6b\u6211\u4fee bug",
        "vision": "\u5206\u6790\u9019\u5f35\u5716",
    }
    if not is_result_followup(samples["result_followup"]):
        raise AssertionError("Chinese result follow-up was not detected")
    if detect_outcome_action(samples["continue"]) != "continue_task":
        raise AssertionError("Chinese continue intent was not detected")
    if detect_outcome_action(samples["send_artifact"]) != "send_artifact":
        raise AssertionError("Chinese send-artifact intent was not detected")
    if detect_outcome_action(samples["analyze"]) != "analyze_artifact":
        raise AssertionError("Chinese analyze intent was not detected")
    if classify_interaction(samples["screen"], False) != InteractionMode.SCREEN_OBSERVE:
        raise AssertionError("Chinese screen intent should route to screen_observe")
    if classify_interaction(samples["tool"], False) != InteractionMode.TOOL_TASK:
        raise AssertionError("Chinese tool intent should route to tool_task")
    if classify_interaction(samples["vision"], True, "photo") != InteractionMode.VISION_TASK:
        raise AssertionError("Chinese vision intent with media should route to vision_task")
    return "unicode intent routing is stable"


def outcome_continue_starts_allowlisted_verifier_worker():
    jobs_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "continue_worker_jobs_test.jsonl")
    results_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "continue_worker_results_test.jsonl")
    for path in (jobs_path, results_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    queue = WorkerQueue(jobs_path=jobs_path, results_path=results_path, allowed_commands={"py_compile": ["python", "-c", "print('compiled')"]})
    agent = CompanionAgent(NoReplanAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "continue_verifier.json"))
    agent.worker_queue = queue
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.session_brain.state.state = "awaiting_validation"
    agent.session_brain.state.pending_validation = ["verify tool results: execute_python"]
    agent.session_brain.state.verification_plan = ["py_compile (required): python -m py_compile core_tools.py -- runtime changed"]
    agent.session_brain.state.last_tool = "execute_python"
    agent.session_brain.state.last_tool_status = "ok"
    result = agent.chat("繼續", response_policy=response_policy_for(InteractionMode.CHAT))
    if "py_compile" not in result["content"] or "job=" not in result["content"]:
        raise AssertionError(result)
    if agent.llm.calls:
        raise AssertionError("continue verifier should not replan")
    jobs = queue.list_jobs(limit=10)
    if not any(job.get("kind") == "py_compile" for job in jobs):
        raise AssertionError(jobs)
    return "continue started allowlisted verifier worker"


def outcome_continue_rejects_non_allowlisted_verifier_plan():
    queue = WorkerQueue(jobs_path=os.path.join(core_tools.PROJECT_CACHE_DIR, "continue_reject_jobs_test.jsonl"), results_path=os.path.join(core_tools.PROJECT_CACHE_DIR, "continue_reject_results_test.jsonl"), allowed_commands={"py_compile": ["python", "-c", "print('ok')"]})
    agent = CompanionAgent(NoReplanAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "continue_reject.json"))
    agent.worker_queue = queue
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.session_brain.state.state = "awaiting_validation"
    agent.session_brain.state.verification_plan = ["danger (required): powershell remove everything"]
    result = agent.chat("繼續", response_policy=response_policy_for(InteractionMode.CHAT))
    if "沒有" not in result["content"] or "安全" not in result["content"]:
        raise AssertionError(result)
    if queue.list_jobs(limit=10):
        raise AssertionError("non-allowlisted verifier should not create job")
    return "non-allowlisted continue plan was not executed"


class WrongToolAfterApprovalAdapter:
    def __init__(self):
        self.calls = 0
        self.write_args = {"filename": "project_cache/wrong_tool.txt", "content": "x"}

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_write", "name": "write_file", "arguments": self.write_args, "raw_arguments": json.dumps(self.write_args)}],
            }
        if self.calls == 2:
            return {"role": "assistant", "content": "需要權限，可以嗎？"}
        if self.calls == 3:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_cmd", "name": "execute_command", "arguments": {"command": "echo should_not_run"}, "raw_arguments": '{"command":"echo should_not_run"}'}],
            }
        return {"role": "assistant", "content": "wrong tool blocked"}


def single_approval_does_not_allow_unrelated_tool():
    target = os.path.join(core_tools.PROJECT_CACHE_DIR, "wrong_tool.txt")
    try:
        os.remove(target)
    except FileNotFoundError:
        pass
    agent = CompanionAgent(WrongToolAfterApprovalAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "wrong_tool_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.chat("write something")
    result = agent.chat("可以")
    if not os.path.exists(target):
        raise AssertionError("pending write_file was not replayed")
    if agent.llm.calls != 1:
        raise AssertionError(f"approval should not replan into unrelated execute_command; calls={agent.llm.calls}")
    if "write_file" not in result["content"]:
        raise AssertionError(result)
    return result["content"]


class TurnApprovalAdapter:
    def __init__(self):
        self.calls = 0

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_write", "name": "write_file", "arguments": {"filename": "project_cache/turn.txt", "content": "turn"}, "raw_arguments": '{"filename":"project_cache/turn.txt","content":"turn"}'}],
            }
        if self.calls == 2:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_write2", "name": "write_file", "arguments": {"filename": "project_cache/turn.txt", "content": "turn"}, "raw_arguments": '{"filename":"project_cache/turn.txt","content":"turn"}'},
                    {"id": "call_py", "name": "execute_python", "arguments": {"code": "print('turn ok')"}, "raw_arguments": json.dumps({"code": "print('turn ok')"})},
                ],
            }
        return {"role": "assistant", "content": "turn approval handled"}


class ComputerControlTurnApprovalAdapter:
    def __init__(self):
        self.calls = 0

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            args = {"keys": "space"}
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_hotkey", "name": "press_hotkey", "arguments": args, "raw_arguments": json.dumps(args)}],
            }
        if self.calls == 2:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_hotkey2", "name": "press_hotkey", "arguments": {"keys": "space"}, "raw_arguments": json.dumps({"keys": "space"})},
                    {"id": "call_click", "name": "click_ui_element", "arguments": {"element_id": "button_1"}, "raw_arguments": json.dumps({"element_id": "button_1"})},
                    {"id": "call_type", "name": "type_keyboard", "arguments": {"text": "hello"}, "raw_arguments": json.dumps({"text": "hello"})},
                    {"id": "call_cmd", "name": "execute_command", "arguments": {"command": "echo should_not_run"}, "raw_arguments": json.dumps({"command": "echo should_not_run"})},
                ],
            }
        return {"role": "assistant", "content": "computer control turn approval handled"}


class PlanFirstAdapter:
    def __init__(self):
        self.calls = 0

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        return {"role": "assistant", "content": "should not run before plan approval"}


class PlanApprovalAdapter:
    def __init__(self):
        self.calls = 0

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            args = {"keys": "alt+tab"}
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_hotkey", "name": "press_hotkey", "arguments": args, "raw_arguments": json.dumps(args)}],
            }
        return {"role": "assistant", "content": "plan approved and first UI step ran"}


class CwdRecoveryAdapter:
    def __init__(self):
        self.calls = 0

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            args = {"command": "python -m py_compile core_tools.py", "timeout": 60, "cwd": "workspace"}
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_compile", "name": "execute_command", "arguments": args, "raw_arguments": json.dumps(args)}],
            }
        tool_content = "\n".join(message.get("content", "") for message in messages if message.get("role") == "tool")
        if "recovered_from" not in tool_content or '"status": "ok"' not in tool_content:
            return {"role": "assistant", "content": "recovery missing"}
        return {"role": "assistant", "content": "recovery ok"}


class TransientToolAdapter:
    def __init__(self):
        self.calls = 0

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_flaky", "name": "fake_flaky_search", "arguments": {"query": "x"}, "raw_arguments": '{"query":"x"}'}],
            }
        tool_content = "\n".join(message.get("content", "") for message in messages if message.get("role") == "tool")
        if "recovered_from" in tool_content and '"status": "ok"' in tool_content:
            return {"role": "assistant", "content": "自己重試後好了"}
        return {"role": "assistant", "content": "沒有自救成功"}


class SelfRepairHintAdapter:
    def __init__(self):
        self.calls = 0
        self.saw_repair_prompt = False

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            args = {"command": "python -m py_compile definitely_missing_file.py", "timeout": 20, "cwd": "project"}
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_bad_compile", "name": "execute_command", "arguments": args, "raw_arguments": json.dumps(args)}],
            }
        tool_text = "\n".join(message.get("content", "") for message in messages if message.get("role") == "tool")
        if "Command completed" in tool_text or '"status": "ok"' in tool_text:
            return {"role": "assistant", "content": "我自己換了安全驗證方式，已經跑通了"}
        system_text = "\n".join(message.get("content", "") for message in messages if message.get("role") == "system")
        if "[SelfRepair]" in system_text:
            self.saw_repair_prompt = True
            args = {"command": "python -m py_compile core_tools.py", "timeout": 60, "cwd": "project"}
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_good_compile", "name": "execute_command", "arguments": args, "raw_arguments": json.dumps(args)}],
            }
        return {"role": "assistant", "content": "只看到錯誤，沒有自救"}


def turn_approval_allows_tool_chain():
    target = os.path.join(core_tools.PROJECT_CACHE_DIR, "turn.txt")
    try:
        os.remove(target)
    except FileNotFoundError:
        pass
    agent = CompanionAgent(TurnApprovalAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "turn_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.chat("do task")
    before = len([m for m in agent.memory if m.get("role") == "tool"])
    result = agent.chat("本輪允許")
    if "可以" not in result["content"] and "點頭" not in result["content"] and "確認" not in result["content"]:
        raise AssertionError(result)
    if not os.path.exists(target):
        raise AssertionError("turn write_file did not execute")
    new_tool_messages = [m for m in agent.memory if m.get("role") == "tool"][before:]
    if not any('"status": "blocked"' in m.get("content", "") and "execute_python" in m.get("content", "") for m in new_tool_messages):
        raise AssertionError("turn bundle should not allow unrelated high-risk execute_python")
    return "turn approval allowed file bundle then stopped at high-risk python"


def turn_approval_allows_computer_control_bundle():
    calls: list[str] = []

    def fake_hotkey(keys: str):
        calls.append(f"press_hotkey:{keys}")
        return core_tools.ToolResult("ok", "fake hotkey")

    def fake_click(element_id: str, double_click: bool = False):
        calls.append(f"click_ui_element:{element_id}:{double_click}")
        return core_tools.ToolResult("ok", "fake click")

    def fake_type(text: str, press_enter: bool = False):
        calls.append(f"type_keyboard:{text}:{press_enter}")
        return core_tools.ToolResult("ok", "fake type")

    agent = CompanionAgent(ComputerControlTurnApprovalAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "computer_turn_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.add_tool(core_tools.AgentTool("press_hotkey", "fake hotkey", fake_hotkey, {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]}, True))
    agent.add_tool(core_tools.AgentTool("click_ui_element", "fake click", fake_click, {"type": "object", "properties": {"element_id": {"type": "string"}, "double_click": {"type": "boolean"}}, "required": ["element_id"]}, True))
    agent.add_tool(core_tools.AgentTool("type_keyboard", "fake type", fake_type, {"type": "object", "properties": {"text": {"type": "string"}, "press_enter": {"type": "boolean"}}, "required": ["text"]}, True))
    first = agent.chat("幫我暫停播放")
    if not agent.permission_manager.pending or agent.permission_manager.pending.tool_name != "press_hotkey":
        raise AssertionError({"first": first, "pending": agent.permission_manager.pending})
    result = agent.chat("本輪允許")
    if "可以" not in result["content"] and "點頭" not in result["content"] and "確認" not in result["content"]:
        raise AssertionError(result)
    expected = {"press_hotkey:space", "click_ui_element:button_1:False", "type_keyboard:hello:False"}
    if not expected.issubset(set(calls)):
        raise AssertionError(calls)
    tool_messages = [m.get("content", "") for m in agent.memory if m.get("role") == "tool"]
    if not any('"status": "blocked"' in content and "execute_command" in content for content in tool_messages):
        raise AssertionError("computer bundle should not allow unrelated execute_command")
    return calls


def plain_approval_allows_computer_control_operation():
    calls: list[str] = []

    def fake_hotkey(keys: str):
        calls.append(f"press_hotkey:{keys}")
        return core_tools.ToolResult("ok", "fake hotkey")

    def fake_click(element_id: str, double_click: bool = False):
        calls.append(f"click_ui_element:{element_id}:{double_click}")
        return core_tools.ToolResult("ok", "fake click")

    def fake_type(text: str, press_enter: bool = False):
        calls.append(f"type_keyboard:{text}:{press_enter}")
        return core_tools.ToolResult("ok", "fake type")

    agent = CompanionAgent(ComputerControlTurnApprovalAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "computer_plain_approval_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.add_tool(core_tools.AgentTool("press_hotkey", "fake hotkey", fake_hotkey, {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]}, True))
    agent.add_tool(core_tools.AgentTool("click_ui_element", "fake click", fake_click, {"type": "object", "properties": {"element_id": {"type": "string"}, "double_click": {"type": "boolean"}}, "required": ["element_id"]}, True))
    agent.add_tool(core_tools.AgentTool("type_keyboard", "fake type", fake_type, {"type": "object", "properties": {"text": {"type": "string"}, "press_enter": {"type": "boolean"}}, "required": ["text"]}, True))
    agent.chat("幫我暫停播放")
    result = agent.chat("可以")
    if "可以" not in result["content"] and "點頭" not in result["content"] and "確認" not in result["content"]:
        raise AssertionError(result)
    expected = {"press_hotkey:space", "click_ui_element:button_1:False", "type_keyboard:hello:False"}
    if not expected.issubset(set(calls)):
        raise AssertionError(calls)
    tool_messages = [m.get("content", "") for m in agent.memory if m.get("role") == "tool"]
    if not any('"status": "blocked"' in content and "execute_command" in content for content in tool_messages):
        raise AssertionError("plain approval must not unlock unrelated execute_command")
    return "plain approval promoted to computer-control operation approval"


def approval_restores_pending_from_task_graph_after_restart():
    calls: list[str] = []

    def fake_hotkey(keys: str):
        calls.append(f"press_hotkey:{keys}")
        return core_tools.ToolResult("ok", "fake hotkey")

    def fake_click(element_id: str, double_click: bool = False):
        calls.append(f"click_ui_element:{element_id}:{double_click}")
        return core_tools.ToolResult("ok", "fake click")

    def fake_type(text: str, press_enter: bool = False):
        calls.append(f"type_keyboard:{text}:{press_enter}")
        return core_tools.ToolResult("ok", "fake type")

    agent = CompanionAgent(ComputerControlTurnApprovalAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "computer_restore_pending_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.add_tool(core_tools.AgentTool("press_hotkey", "fake hotkey", fake_hotkey, {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]}, True))
    agent.add_tool(core_tools.AgentTool("click_ui_element", "fake click", fake_click, {"type": "object", "properties": {"element_id": {"type": "string"}, "double_click": {"type": "boolean"}}, "required": ["element_id"]}, True))
    agent.add_tool(core_tools.AgentTool("type_keyboard", "fake type", fake_type, {"type": "object", "properties": {"text": {"type": "string"}, "press_enter": {"type": "boolean"}}, "required": ["text"]}, True))
    agent.chat("幫我暫停播放")
    if not agent.permission_manager.pending:
        raise AssertionError("first blocked tool did not create pending permission")
    agent.permission_manager.pending = None
    result = agent.chat("本轮允许")
    if "可以" not in result["content"] and "點頭" not in result["content"] and "確認" not in result["content"]:
        raise AssertionError(result)
    expected = {"press_hotkey:space", "click_ui_element:button_1:False", "type_keyboard:hello:False"}
    if not expected.issubset(set(calls)):
        raise AssertionError(calls)
    return "pending permission restored from task graph and approved"


def command_cwd_failure_recovers_inside_agent_loop():
    agent = CompanionAgent(CwdRecoveryAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "cwd_recovery_test.json"))
    agent.interactive_mode = False
    agent.always_allow_tools = True
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    result = agent.chat("run compile from the wrong cwd")
    if result["content"] != "recovery ok":
        raise AssertionError(result)
    tool_messages = [message.get("content", "") for message in agent.memory if message.get("role") == "tool"]
    if not any("recovered_from" in content and '"cwd": "project"' in content for content in tool_messages):
        raise AssertionError(tool_messages[-2:])
    return "cwd failure recovered and continued"


def transient_tool_error_recovers_before_user_followup():
    attempts = {"count": 0}

    def flaky_search(query: str):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return core_tools.ToolResult("error", "Connection aborted.", error="ConnectionResetError(10054)")
        return core_tools.ToolResult("ok", "search recovered", data={"items": ["ok"]})

    agent = CompanionAgent(TransientToolAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "transient_recovery_test.json"))
    agent.add_tool(core_tools.AgentTool("fake_flaky_search", "fake flaky idempotent search", flaky_search, {"type": "object", "properties": {"query": {"type": "string"}}}))
    agent.self_recovery = SelfRecoveryController(executor=agent.executor, hooks=agent.hooks, session_id=agent.session_id)
    agent.self_recovery._can_retry_exactly = lambda tool_name, arguments: tool_name == "fake_flaky_search"
    result = agent.chat("search with transient failure")
    if result["content"] != "自己重試後好了":
        raise AssertionError(result)
    if attempts["count"] != 2:
        raise AssertionError(f"expected one automatic retry, got {attempts['count']}")
    tool_messages = [message.get("content", "") for message in agent.memory if message.get("role") == "tool"]
    if not any("transient_retry" in content and "recovered_from" in content for content in tool_messages):
        raise AssertionError(tool_messages)
    return result["content"]


def self_recovery_does_not_retry_unsafe_python():
    attempts = {"count": 0}

    def unsafe_python(code: str):
        attempts["count"] += 1
        return core_tools.ToolResult("error", "Connection aborted.", error="ConnectionResetError(10054)")

    agent = CompanionAgent(PlainReplyAdapter("unused"), "system self test", os.path.join(core_tools.HISTORY_DIR, "unsafe_recovery_test.json"))
    agent.add_tool(core_tools.AgentTool("execute_python", "fake python", unsafe_python, {"type": "object", "properties": {"code": {"type": "string"}}}, True))
    original = core_tools.ToolResult("error", "Connection aborted.", error="ConnectionResetError(10054)")
    recovered, evidence = agent.self_recovery.recover("execute_python", {"code": "print('x')"}, original, None, response_policy_for(InteractionMode.TOOL_TASK), agent.turn_id)
    if recovered is not original or evidence is not None:
        raise AssertionError((recovered.to_text(), evidence))
    if attempts["count"] != 0:
        raise AssertionError("unsafe python should not be retried")
    return "unsafe python was not auto-retried"


def self_recovery_failed_retry_leaves_evidence_for_reply():
    class FailingExecutor:
        def __init__(self):
            self.calls = 0

        def execute(self, tool_name, arguments, callback=None, response_policy=None):
            self.calls += 1
            return core_tools.ToolResult("error", "Connection aborted.", error="ConnectionResetError(10054)", data={"attempt": self.calls})

    executor = FailingExecutor()
    recovery = SelfRecoveryController(executor=executor, hooks=DEFAULT_HOOK_MANAGER, session_id="self_test", max_transient_retries=2)
    original = core_tools.ToolResult("error", "Connection aborted.", error="ConnectionResetError(10054)")
    recovered, evidence = recovery.recover("search_knowledge", {"query": "permission replay"}, original, None, response_policy_for(InteractionMode.TOOL_TASK), 1)
    if recovered.status != "error" or not evidence:
        raise AssertionError({"recovered": recovered, "evidence": evidence})
    recovery_data = recovered.data.get("recovery_attempted") if isinstance(recovered.data, dict) else None
    if not recovery_data or recovery_data.get("reason") != "transient_retry" or recovery_data.get("attempts") != 2:
        raise AssertionError({"data": recovered.data, "evidence": evidence})
    from agent_user_voice import approved_tool_error_reply
    import agent_outcome

    reply = approved_tool_error_reply("search_knowledge", recovered)
    if "自己試過" not in reply or "transient_retry" not in reply:
        raise AssertionError(reply)
    summary, _artifacts = agent_outcome.tool_result_outcome("search_knowledge", recovered)
    if "recovery_attempted: transient_retry attempts=2" not in summary:
        raise AssertionError(summary)
    return "failed self-recovery leaves owner-visible evidence"


def missing_mss_screenshot_recovery_uses_safe_fallback():
    attempts = {"count": 0, "fallback_code": ""}

    class Executor:
        def execute(self, tool_name, arguments, callback=None, response_policy=None):
            attempts["count"] += 1
            attempts["fallback_code"] = arguments.get("code", "")
            if "System.Windows.Forms" not in attempts["fallback_code"] or "CopyFromScreen" not in attempts["fallback_code"]:
                return core_tools.ToolResult("error", "bad fallback", error="bad fallback")
            return core_tools.ToolResult("ok", "Python completed.", data={"stdout": "fullscreen_screenshot.png", "stderr": ""})

    recovery = SelfRecoveryController(executor=Executor(), hooks=DEFAULT_HOOK_MANAGER, session_id="self_test")
    original = core_tools.ToolResult(
        "error",
        "Python failed.",
        data={"stderr": "ModuleNotFoundError: No module named 'mss'"},
        error="ModuleNotFoundError: No module named 'mss'",
    )
    args = {"code": "import mss\n# take screenshot of monitor and save .png", "timeout": 30}
    recovered, evidence = recovery.recover("execute_python", args, original, None, response_policy_for(InteractionMode.TOOL_TASK), 1)
    if recovered.status != "ok" or not evidence or evidence.get("reason") != "screenshot_capture_fallback":
        raise AssertionError((recovered.to_text(), evidence))
    if attempts["count"] != 1:
        raise AssertionError(attempts)
    if "fullscreen_screenshot.png" not in attempts["fallback_code"]:
        raise AssertionError(attempts["fallback_code"])
    return "missing mss screenshot recovered through safe fallback"


def screenshot_runtime_error_recovery_uses_safe_fallback():
    attempts = {"count": 0, "fallback_code": ""}

    class Executor:
        def execute(self, tool_name, arguments, callback=None, response_policy=None):
            attempts["count"] += 1
            attempts["fallback_code"] = arguments.get("code", "")
            if "System.Windows.Forms" not in attempts["fallback_code"] or "CopyFromScreen" not in attempts["fallback_code"]:
                return core_tools.ToolResult("error", "bad fallback", error="bad fallback")
            return core_tools.ToolResult("ok", "Python completed.", data={"stdout": "fullscreen_screenshot.png", "stderr": ""})

    recovery = SelfRecoveryController(executor=Executor(), hooks=DEFAULT_HOOK_MANAGER, session_id="self_test")
    original = core_tools.ToolResult(
        "error",
        "Python failed.",
        data={"stderr": "AttributeError: 'ScreenShot' object has no attribute 'save'"},
        error="AttributeError: 'ScreenShot' object has no attribute 'save'",
    )
    args = {"code": "import mss\nwith mss.mss() as sct:\n    img = sct.grab(sct.monitors[0])\n    img.save('screen.png')", "timeout": 30}
    diagnosis = diagnose_tool_error("execute_python", args, original)
    plan = plan_recovery("execute_python", args, original, diagnosis)
    if diagnosis.category != "screenshot_python_failure" or not plan or plan.strategy != "screenshot_capture_fallback":
        raise AssertionError((diagnosis, plan))
    recovered, evidence = recovery.recover("execute_python", args, original, None, response_policy_for(InteractionMode.TOOL_TASK), 1)
    if recovered.status != "ok" or not evidence or evidence.get("reason") != "screenshot_capture_fallback":
        raise AssertionError((recovered.to_text(), evidence))
    if attempts["count"] != 1:
        raise AssertionError(attempts)
    return "screenshot runtime error recovered through safe fallback"


def self_recovery_diagnoses_and_plans_known_errors():
    cwd_result = core_tools.ToolResult(
        "error",
        "Command failed.",
        data={"cwd": "workspace", "retry_hint": "Command could not find a file. Verify cwd and paths."},
    )
    cwd_args = {"command": "python -m py_compile ../core_tools.py", "cwd": "workspace"}
    cwd_diagnosis = diagnose_tool_error("execute_command", cwd_args, cwd_result)
    cwd_plan = plan_recovery("execute_command", cwd_args, cwd_result, cwd_diagnosis)
    if cwd_diagnosis.category != "cwd_or_path_mismatch" or not cwd_plan or cwd_plan.strategy != "cwd_retry":
        raise AssertionError((cwd_diagnosis, cwd_plan))
    if cwd_plan.retry_args.get("cwd") != "project":
        raise AssertionError(cwd_plan)

    mss_result = core_tools.ToolResult("error", "Python failed.", error="ModuleNotFoundError: No module named 'mss'")
    mss_args = {"code": "import mss\nsct.grab(sct.monitors[0]).save('screen.png')", "timeout": 30}
    mss_diagnosis = diagnose_tool_error("execute_python", mss_args, mss_result)
    mss_plan = plan_recovery("execute_python", mss_args, mss_result, mss_diagnosis)
    if mss_diagnosis.category != "missing_python_module" or not mss_plan or mss_plan.strategy != "screenshot_capture_fallback":
        raise AssertionError((mss_diagnosis, mss_plan))

    transient = core_tools.ToolResult("error", "Connection aborted.", error="ConnectionResetError(10054)")
    transient_args = {"query": "hello"}
    transient_diagnosis = diagnose_tool_error("search_knowledge", transient_args, transient)
    transient_plan = plan_recovery("search_knowledge", transient_args, transient, transient_diagnosis, max_transient_retries=2)
    if transient_diagnosis.category != "transient_external_error" or not transient_plan or transient_plan.max_attempts != 2:
        raise AssertionError((transient_diagnosis, transient_plan))
    return "known recovery errors diagnose and plan deterministically"


def repair_planner_orders_safe_candidates():
    planner = RepairPlanner(max_transient_retries=2)

    cwd_result = core_tools.ToolResult(
        "error",
        "Command failed.",
        data={"cwd": "workspace", "retry_hint": "Command could not find a file. Verify cwd and paths."},
    )
    cwd_args = {"command": "python -m py_compile ../core_tools.py", "cwd": "workspace"}
    cwd_candidates = planner.candidates("execute_command", cwd_args, cwd_result)
    if [item.strategy for item in cwd_candidates] != ["cwd_retry", "command_file_probe"]:
        raise AssertionError(cwd_candidates)
    if cwd_candidates[0].details.get("candidate_order") != 1:
        raise AssertionError(cwd_candidates[0])
    if cwd_candidates[1].tool_name != "execute_python" or cwd_candidates[1].requires_same_tool:
        raise AssertionError(cwd_candidates[1])

    screenshot_result = core_tools.ToolResult("error", "Python failed.", error="ModuleNotFoundError: No module named 'mss'")
    screenshot_args = {"code": "import mss\nsct.grab(sct.monitors[0]).save('screen.png')", "timeout": 30}
    screenshot_candidates = plan_recovery_candidates(
        "execute_python",
        screenshot_args,
        screenshot_result,
        diagnose_tool_error("execute_python", screenshot_args, screenshot_result),
    )
    if [item.strategy for item in screenshot_candidates] != ["screenshot_capture_fallback", "screen_ui_snapshot_fallback"]:
        raise AssertionError(screenshot_candidates)
    if screenshot_candidates[1].tool_name != "get_screen_ui" or screenshot_candidates[1].requires_same_tool:
        raise AssertionError(screenshot_candidates[1])

    transient_result = core_tools.ToolResult("error", "Connection aborted.", error="ConnectionResetError(10054)")
    transient_candidates = planner.candidates("search_knowledge", {"query": "permission"}, transient_result)
    if [item.strategy for item in transient_candidates] != ["transient_retry"] or transient_candidates[0].max_attempts != 2:
        raise AssertionError(transient_candidates)
    return "repair planner produced ordered safe candidates"


def command_recovery_uses_file_probe_after_cwd_retry_fails():
    attempts: list[tuple[str, dict]] = []

    class Executor:
        def execute(self, tool_name, arguments, callback=None, response_policy=None):
            attempts.append((tool_name, dict(arguments or {})))
            if tool_name == "execute_command":
                return core_tools.ToolResult(
                    "error",
                    "Command failed.",
                    data={"cwd": "project", "retry_hint": "Command could not find a file. Verify cwd and paths."},
                    error="No such file or directory: missing_core_file.py",
                )
            if tool_name == "execute_python":
                code = arguments.get("code", "")
                if "missing_core_file.py" not in code or "rglob" not in code:
                    return core_tools.ToolResult("error", "bad probe", error=code)
                return core_tools.ToolResult("ok", "Python completed.", data={"stdout": "missing_core_file.py: not found"})
            raise AssertionError(tool_name)

    recovery = SelfRecoveryController(executor=Executor(), hooks=DEFAULT_HOOK_MANAGER, session_id="self_test")
    original = core_tools.ToolResult(
        "error",
        "Command failed.",
        data={"cwd": "workspace", "retry_hint": "Command could not find a file. Verify cwd and paths."},
        error="No such file or directory: missing_core_file.py",
    )
    args = {"command": "python -m py_compile missing_core_file.py", "cwd": "workspace", "timeout": 30}
    recovered, evidence = recovery.recover("execute_command", args, original, None, response_policy_for(InteractionMode.TOOL_TASK), 1)
    if recovered.status != "ok" or not evidence or evidence.get("reason") != "command_file_probe":
        raise AssertionError((recovered.to_text(), evidence))
    if [tool for tool, _args in attempts] != ["execute_command", "execute_python"]:
        raise AssertionError(attempts)
    prior = recovered.data.get("prior_recovery_attempts") if isinstance(recovered.data, dict) else None
    if not prior or prior[0].get("reason") != "cwd_retry":
        raise AssertionError(recovered.to_text())
    if _extract_command_file_names("python -m py_compile core_tools.py README.md") != ["core_tools.py", "README.md"]:
        raise AssertionError("file extraction changed")
    return "command recovery used file probe after cwd retry failed"


def self_recovery_uses_second_candidate_when_first_fails():
    attempts: list[tuple[str, dict]] = []

    class Executor:
        def execute(self, tool_name, arguments, callback=None, response_policy=None):
            attempts.append((tool_name, dict(arguments or {})))
            if tool_name == "execute_python":
                return core_tools.ToolResult("error", "PowerShell screenshot failed.", error="PowerShell screenshot failed")
            if tool_name == "get_screen_ui":
                return core_tools.ToolResult("ok", "Active window: test\nClickable/input elements:", data={"count": 0})
            raise AssertionError(tool_name)

    recovery = SelfRecoveryController(executor=Executor(), hooks=DEFAULT_HOOK_MANAGER, session_id="self_test")
    original = core_tools.ToolResult(
        "error",
        "Python failed.",
        data={"stderr": "ModuleNotFoundError: No module named 'mss'"},
        error="ModuleNotFoundError: No module named 'mss'",
    )
    args = {"code": "import mss\nsct.grab(sct.monitors[0]).save('screen.png')", "timeout": 30}
    recovered, evidence = recovery.recover("execute_python", args, original, None, response_policy_for(InteractionMode.SCREEN_OBSERVE), 1)
    if recovered.status != "ok" or not evidence or evidence.get("reason") != "screen_ui_snapshot_fallback":
        raise AssertionError((recovered.to_text(), evidence))
    if [tool for tool, _args in attempts] != ["execute_python", "get_screen_ui"]:
        raise AssertionError(attempts)
    prior = recovered.data.get("prior_recovery_attempts") if isinstance(recovered.data, dict) else None
    if not prior or prior[0].get("reason") != "screenshot_capture_fallback":
        raise AssertionError(recovered.to_text())
    return "second recovery candidate ran after first screenshot fallback failed"


def self_recovery_skips_already_attempted_candidate():
    class FailingExecutor:
        def __init__(self):
            self.calls = 0

        def execute(self, tool_name, arguments, callback=None, response_policy=None):
            self.calls += 1
            return core_tools.ToolResult("error", "Connection aborted.", error="ConnectionResetError(10054)")

    executor = FailingExecutor()
    recovery = SelfRecoveryController(executor=executor, hooks=DEFAULT_HOOK_MANAGER, session_id="self_test", max_transient_retries=1)
    original = core_tools.ToolResult("error", "Connection aborted.", error="ConnectionResetError(10054)")
    first, first_evidence = recovery.recover("search_knowledge", {"query": "permission"}, original, None, response_policy_for(InteractionMode.TOOL_TASK), 1)
    second, second_evidence = recovery.recover("search_knowledge", {"query": "permission"}, original, None, response_policy_for(InteractionMode.TOOL_TASK), 2)
    if executor.calls != 1:
        raise AssertionError(f"expected one retry attempt, got {executor.calls}")
    if not first_evidence or first.data.get("recovery_attempted", {}).get("reason") != "transient_retry":
        raise AssertionError((first.to_text(), first_evidence))
    if second is not original or second_evidence is not None:
        raise AssertionError((second.to_text(), second_evidence))
    return "self recovery skipped an already attempted candidate"


def self_recovery_does_not_plan_unsafe_unknown_errors():
    result = core_tools.ToolResult("error", "Python failed.", error="ModuleNotFoundError: No module named 'numpy'")
    args = {"code": "import numpy\nprint('x')"}
    diagnosis = diagnose_tool_error("execute_python", args, result)
    plan = plan_recovery("execute_python", args, result, diagnosis)
    if diagnosis.safe_to_auto_repair or plan is not None:
        raise AssertionError((diagnosis, plan))

    transient = core_tools.ToolResult("error", "Connection aborted.", error="ConnectionResetError(10054)")
    unsafe_args = {"code": "print('x')"}
    unsafe_diagnosis = diagnose_tool_error("execute_python", unsafe_args, transient)
    unsafe_plan = plan_recovery("execute_python", unsafe_args, transient, unsafe_diagnosis)
    if unsafe_diagnosis.safe_to_auto_repair or unsafe_plan is not None:
        raise AssertionError((unsafe_diagnosis, unsafe_plan))
    return "unknown or unsafe errors do not create auto-recovery plans"


def missing_mss_recovery_bypasses_chat_route_allowlist():
    attempts = {"count": 0, "policy": None}

    class Executor:
        def execute(self, tool_name, arguments, callback=None, response_policy=None):
            attempts["count"] += 1
            attempts["policy"] = response_policy
            if getattr(response_policy, "allowed_tools", ["blocked"]) is not None:
                return core_tools.ToolResult("blocked", "execute_python skipped by chat route policy.", data={"route": getattr(response_policy, "route", "")})
            return core_tools.ToolResult("ok", "Python completed.", data={"stdout": arguments.get("code", "")})

    recovery = SelfRecoveryController(executor=Executor(), hooks=DEFAULT_HOOK_MANAGER, session_id="self_test")
    original = core_tools.ToolResult(
        "error",
        "Python failed.",
        data={"stderr": "ModuleNotFoundError: No module named 'mss'"},
        error="ModuleNotFoundError: No module named 'mss'",
    )
    args = {"code": "import mss\nwith mss.mss() as sct:\n    img = sct.grab(sct.monitors[0])\n    img.save('screen.png')", "timeout": 30}
    recovered, evidence = recovery.recover("execute_python", args, original, None, response_policy_for(InteractionMode.CHAT), 1)
    if recovered.status != "ok" or not evidence:
        raise AssertionError((recovered.to_text(), evidence))
    policy = attempts["policy"]
    if getattr(policy, "allowed_tools", None) is not None or "recovery" not in getattr(policy, "route", ""):
        raise AssertionError(policy)
    return "mss fallback recovery bypassed chat route allowlist"


def missing_module_recovery_is_narrow():
    result = core_tools.ToolResult("error", "Python failed.", error="ModuleNotFoundError: No module named 'mss'")
    if _missing_module_name(result) != "mss":
        raise AssertionError("missing module parser failed")
    if not _looks_like_screenshot_code("import mss\nimg = sct.grab(monitor)\nimg.save('screen.png')"):
        raise AssertionError("screenshot detector missed mss screenshot code")
    if _looks_like_screenshot_code("import mss\nprint('hello')"):
        raise AssertionError("mss fallback should stay narrow")

    class Executor:
        def execute(self, tool_name, arguments, callback=None, response_policy=None):
            raise AssertionError("narrow recovery should not execute fallback")

    recovery = SelfRecoveryController(executor=Executor(), hooks=DEFAULT_HOOK_MANAGER, session_id="self_test")
    recovered, evidence = recovery.recover("execute_python", {"code": "import numpy\nprint('x')"}, result, None, response_policy_for(InteractionMode.TOOL_TASK), 1)
    if recovered is not result or evidence is not None:
        raise AssertionError((recovered.to_text(), evidence))
    return "missing module recovery stayed narrow"


def self_repair_prompt_includes_failed_deterministic_recovery():
    from agent_self_recovery import self_repair_instruction

    result = core_tools.ToolResult(
        "error",
        "Python failed.",
        error="ModuleNotFoundError: No module named 'mss'",
        data={
            "stderr": "ModuleNotFoundError: No module named 'mss'",
            "recovery_attempted": {
                "reason": "screenshot_capture_fallback",
                "attempts": 1,
                "retry_status": "error",
                "retry_message": "PowerShell screenshot failed",
                "details": {"strategy": "screenshot_capture_fallback"},
            },
        },
    )
    instruction = self_repair_instruction("execute_python", {"code": "import mss\n# screenshot"}, result)
    required = [
        "deterministic_recovery_already_tried",
        "screenshot_capture_fallback",
        "PowerShell screenshot failed",
        "Do not repeat that exact recovery path",
    ]
    missing = [item for item in required if item not in instruction]
    if missing:
        raise AssertionError({"missing": missing, "instruction": instruction})
    return "self repair prompt carries failed deterministic recovery evidence"


def tool_loop_prompts_self_repair_before_user_followup():
    agent = CompanionAgent(SelfRepairHintAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "self_repair_hint_test.json"))
    agent.interactive_mode = False
    agent.always_allow_tools = True
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    result = agent.chat("run a verifier and recover if it fails", response_policy=response_policy_for(InteractionMode.TOOL_TASK))
    if "已經跑通" not in result["content"]:
        raise AssertionError(result)
    if not agent.llm.saw_repair_prompt:
        raise AssertionError("self repair prompt was not presented to the model")
    system_messages = [message.get("content", "") for message in agent.memory if message.get("role") == "system"]
    if any("[SelfRepair]" in content for content in system_messages):
        raise AssertionError("transient self repair prompt leaked into persistent memory")
    tool_messages = [message.get("content", "") for message in agent.memory if message.get("role") == "tool"]
    if not any("definitely_missing_file.py" in content for content in tool_messages) or not any('"status": "ok"' in content for content in tool_messages):
        raise AssertionError(tool_messages[-4:])
    return "self repair hint led to safe follow-up tool before owner had to ask"


def trace_log_records_tool_events():
    try:
        os.remove(TRACE_LOG_FILE)
    except FileNotFoundError:
        pass
    result = core_tools.real_execute_python('print("trace ok")')
    if result.status != "ok":
        raise AssertionError(result.to_text())
    agent = CompanionAgent(TurnApprovalAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "trace_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.chat("do task")
    agent.chat("本輪允許")
    with open(TRACE_LOG_FILE, "r", encoding="utf-8") as file:
        lines = [json.loads(line) for line in file if line.strip()]
    events = {line["event"] for line in lines}
    required = {"UserMessage", "llm.response", "tool.blocked", "PermissionRequest", "PermissionGranted", "tool.start", "tool.end", "PostToolUse"}
    missing = required - events
    if missing:
        raise AssertionError(f"missing trace events: {missing}")
    return f"{len(lines)} trace events recorded"


class PlainReplyAdapter:
    def __init__(self, content="plain reply"):
        self.content = content

    def chat_with_tools(self, messages, tools):
        return {"role": "assistant", "content": self.content}


class CaptureReplyAdapter:
    def __init__(self, content="plain reply"):
        self.content = content
        self.calls = 0
        self.last_messages = []

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        self.last_messages = [dict(message) for message in messages]
        return {"role": "assistant", "content": self.content}


def reset_session_brain_file():
    for path in [SESSION_BRAIN_FILE, TASK_GRAPHS_FILE]:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def session_brain_plain_chat_stays_idle():
    reset_session_brain_file()
    agent = CompanionAgent(PlainReplyAdapter("chat ok"), "system self test", os.path.join(core_tools.HISTORY_DIR, "brain_chat.json"))
    result = agent.chat("早安，今天有點想聊天")
    if result["content"] != "chat ok":
        raise AssertionError(result)
    if agent.session_brain.state.state != "idle" or not agent.session_brain.state.last_turn_was_chat:
        raise AssertionError(agent.session_brain.state)
    return agent.session_brain.summary()


def runtime_context_keeps_plain_chat_lightweight():
    reset_session_brain_file()
    adapter = CaptureReplyAdapter("chat ok")
    agent = CompanionAgent(adapter, "system self test", os.path.join(core_tools.HISTORY_DIR, "runtime_context_chat.json"))
    result = agent.chat("月月，普通聊一下今天心情")
    if result["content"] != "chat ok":
        raise AssertionError(result)
    user_messages = [message["content"] for message in adapter.last_messages if message.get("role") == "user"]
    if not user_messages:
        raise AssertionError("no user message captured")
    last_user = user_messages[-1]
    forbidden = ["[SessionBrain]", "[TaskGraph]", "[目前任務筆記]", "[目前步驟]", "turn_intent:", "intent:"]
    leaked = [item for item in forbidden if item in last_user]
    if leaked:
        raise AssertionError(f"plain chat leaked task context: {leaked}\n{last_user}")
    if infer_route_from_messages(adapter.last_messages) != "chat":
        raise AssertionError("plain chat should route to chat model")
    return last_user


def runtime_context_keeps_task_state_for_tasks():
    reset_session_brain_file()
    adapter = CaptureReplyAdapter("task ok")
    agent = CompanionAgent(adapter, "system self test", os.path.join(core_tools.HISTORY_DIR, "runtime_context_task.json"))
    agent.chat("please implement a small fix")
    user_messages = [message["content"] for message in adapter.last_messages if message.get("role") == "user"]
    last_user = user_messages[-1]
    required = ["[目前任務筆記]", "[目前步驟]", "intent:"]
    missing = [item for item in required if item not in last_user]
    if missing:
        raise AssertionError(f"task context missing: {missing}\n{last_user}")
    leaked = [item for item in ["[SessionBrain]", "[TaskGraph]", "turn_intent:"] if item in last_user]
    if leaked:
        raise AssertionError(f"task context leaked framework labels: {leaked}\n{last_user}")
    if infer_route_from_messages(adapter.last_messages) == "chat":
        raise AssertionError("task turn should not route to chat model")
    return "task runtime context preserved"


def runtime_context_builder_policy_is_explicit():
    chat = build_runtime_context("hi", turn_intent="chat", session_summary="state", task_summary="task", include_task_context=False)
    task = build_runtime_context("fix", turn_intent="task", session_summary="state", task_summary="task", include_task_context=True)
    if any(marker in chat for marker in ["[SessionBrain]", "[TaskGraph]", "[目前任務筆記]", "[目前步驟]"]):
        raise AssertionError(chat)
    if "[目前任務筆記]" not in task or "intent: task" not in task:
        raise AssertionError(task)
    if "[SessionBrain]" in task or "turn_intent:" in task:
        raise AssertionError(task)
    if should_include_task_context("chat"):
        raise AssertionError("chat should not include task context by default")
    if not should_include_task_context("task") or not should_include_task_context("chat", grant="single"):
        raise AssertionError("task/grant should include context")
    if not should_include_task_context("chat", active_task=True):
        raise AssertionError("active workflow should keep context even for terse follow-up")
    return "runtime context policy separated chat from task"


def session_brain_task_enters_active_task():
    reset_session_brain_file()
    agent = CompanionAgent(PlainReplyAdapter("task noted"), "system self test", os.path.join(core_tools.HISTORY_DIR, "brain_task.json"))
    agent.chat("please implement a small fix")
    if agent.session_brain.state.state != "active_task" or agent.session_brain.state.last_turn_was_chat:
        raise AssertionError(agent.session_brain.state)
    if "please implement" not in agent.session_brain.state.current_objective:
        raise AssertionError(agent.session_brain.state.current_objective)
    return agent.session_brain.summary()


def session_brain_blocked_tool_awaits_permission():
    reset_session_brain_file()
    target = os.path.join(core_tools.PROJECT_CACHE_DIR, "permission_test.txt")
    try:
        os.remove(target)
    except FileNotFoundError:
        pass
    agent = CompanionAgent(PermissionAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "brain_permission.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.chat("write a file")
    if agent.session_brain.state.state != "awaiting_permission":
        raise AssertionError(agent.session_brain.state)
    if not agent.permission_manager.pending:
        raise AssertionError("permission manager lost pending tool")
    return agent.session_brain.summary()


def session_brain_approval_moves_to_validation():
    reset_session_brain_file()
    target = os.path.join(core_tools.PROJECT_CACHE_DIR, "permission_test.txt")
    try:
        os.remove(target)
    except FileNotFoundError:
        pass
    agent = CompanionAgent(PermissionAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "brain_validation.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.chat("write a file")
    agent.chat("可以")
    if agent.session_brain.state.state != "awaiting_validation":
        raise AssertionError(agent.session_brain.state)
    if not agent.session_brain.state.pending_validation:
        raise AssertionError(agent.session_brain.state)
    return agent.session_brain.summary()


def session_brain_cancel_returns_idle():
    reset_session_brain_file()
    agent = CompanionAgent(PlainReplyAdapter("cancel ok"), "system self test", os.path.join(core_tools.HISTORY_DIR, "brain_cancel.json"))
    agent.chat("please implement something")
    agent.chat("算了，停止")
    if agent.session_brain.state.state != "idle" or agent.session_brain.state.current_objective:
        raise AssertionError(agent.session_brain.state)
    return agent.session_brain.summary()


def session_brain_trace_events_are_recorded():
    reset_session_brain_file()
    try:
        os.remove(TRACE_LOG_FILE)
    except FileNotFoundError:
        pass
    agent = CompanionAgent(PlainReplyAdapter("trace ok"), "system self test", os.path.join(core_tools.HISTORY_DIR, "brain_trace.json"))
    agent.chat("please implement trace check")
    with open(TRACE_LOG_FILE, "r", encoding="utf-8") as file:
        events = [json.loads(line) for line in file if line.strip()]
    names = {event.get("event") for event in events}
    required = {"session_brain.classified", "session_brain.state_changed"}
    missing = required - names
    if missing:
        raise AssertionError(missing)
    return "session brain trace events recorded"


def hook_pre_tool_can_block_and_before_reply_can_annotate():
    class HookCommandAdapter:
        def __init__(self):
            self.calls = 0

        def chat_with_tools(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_cmd_hook",
                            "name": "execute_command",
                            "arguments": {"command": "echo blocked"},
                            "raw_arguments": '{"command":"echo blocked"}',
                        }
                    ],
                }
            return {"role": "assistant", "content": "hook reply"}

    def block_execute_command(event):
        if event.tool_name == "execute_command":
            return HookDecision.block_decision("blocked by self-test hook")
        return HookDecision.allow_decision()

    def annotate_reply(event):
        return HookDecision(annotate="\n[hook annotation]")

    DEFAULT_HOOK_MANAGER.clear()
    try:
        DEFAULT_HOOK_MANAGER.register("PreToolUse", block_execute_command)
        DEFAULT_HOOK_MANAGER.register("BeforeReply", annotate_reply)
        agent = CompanionAgent(HookCommandAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "hook_test.json"))
        agent.interactive_mode = False
        for tool in core_tools.ALL_TOOLS:
            agent.add_tool(tool)
        result = agent.chat("run blocked command")
        if "hook annotation" not in result["content"]:
            raise AssertionError(result)
        if not any("blocked by self-test hook" in m.get("content", "") for m in agent.memory if m.get("role") == "tool"):
            raise AssertionError("PreToolUse hook did not block command")
        return "hooks blocked a tool and annotated reply"
    finally:
        DEFAULT_HOOK_MANAGER.clear()


def skills_registry_discovers_and_selects():
    skills = DEFAULT_SKILL_REGISTRY.load()
    for name in ("debug", "vision", "telegram", "safe-computer-use", "code-review-lite"):
        if name not in skills:
            raise AssertionError(f"missing skill {name}")
    selected = DEFAULT_SKILL_REGISTRY.select("Telegram sticker reaction bug")
    names = {skill.name for skill in selected}
    if "telegram" not in names:
        raise AssertionError(names)
    return f"{len(skills)} skills loaded"


def context_pack_includes_selected_skill_but_is_bounded():
    skill = DEFAULT_SKILL_REGISTRY.load()["debug"]
    context = DEFAULT_CONTEXT_BUILDER.build([skill], base_prompt="base prompt")
    if "Skill: debug" not in context or "base prompt" not in context:
        raise AssertionError(context[:500])
    if len(context) > 12000:
        raise AssertionError(len(context))
    return f"context length {len(context)}"


def context_pack_writes_budget_report():
    DEFAULT_CONTEXT_BUILDER.build(base_prompt="base prompt", mode="task", user_input="permission replay execute_command cwd")
    if not os.path.exists(CONTEXT_BUDGET_REPORT_FILE):
        raise AssertionError("context budget report was not written")
    with open(CONTEXT_BUDGET_REPORT_FILE, "r", encoding="utf-8") as file:
        report = json.load(file)
    sections = report.get("sections", {})
    if report.get("mode") != "task" or sections.get("total_after", 0) <= 0 or sections.get("total_after", 0) > sections.get("max_chars", 0):
        raise AssertionError(report)
    return sections


def memory_compiler_includes_profile_and_personality():
    compiled = compile_memory("chat", "普通聊天")
    rendered = compiled.render()
    required = ["YueYue SOUL Core", "Xioshon", "cyber catgirl", "喵", "傲嬌", "主人", "Rolling Chat Summary"]
    missing = [item for item in required if item not in rendered]
    if missing:
        raise AssertionError(f"missing from compiled memory: {missing}\n{rendered[:800]}")
    if not os.path.exists(MEMORY_COMPILED_FILE):
        raise AssertionError("memory_compiled.json was not written")
    return compiled.mode

def memory_compiler_modes_control_engineering_context():
    chat = compile_memory("chat", "聊聊天")
    task = compile_memory("task", "permission replay failure replay debounce")
    social = compile_memory("social_sticker", "鬥圖 表情包")
    screen = compile_memory("screen_observe", "幫我截圖看看狀態")
    if chat.engineering_context:
        raise AssertionError("chat mode should not inject engineering context")
    if social.engineering_context:
        raise AssertionError("social sticker mode should not inject engineering context")
    if "permission" not in task.engineering_context.casefold() and "replay" not in task.engineering_context.casefold():
        raise AssertionError(task.engineering_context[:500])
    if not task.task_state or not screen.task_state:
        raise AssertionError("task/screen modes should include SessionBrain")
    if "Persona mode: social_sticker" not in social.personality_core or "Persona mode: screen_observe" not in screen.personality_core:
        raise AssertionError({"social": social.personality_core[:200], "screen": screen.personality_core[:200]})
    return "memory modes separated"

def memory_health_detects_mojibake_without_current_leak():
    if not looks_mojibake("浣犳槸鏈堟湀瑕嬶紝Xioshon 鐨勮秴绱氬皥灞?"):
        raise AssertionError("mojibake detector missed sample")
    health = memory_health_check()
    if health.mojibake_detected:
        raise AssertionError(health.to_dict())
    if not os.path.exists(MEMORY_HEALTH_FILE):
        raise AssertionError("memory_health.json was not written")
    return "memory health ok"


def rolling_summary_stores_summary_not_full_history():
    update_chat_summary("Owner: " + ("這是一段很長的聊天原文 " * 40) + "| YueYue: 簡短回覆")
    with open(ROLLING_SUMMARY_FILE, "r", encoding="utf-8") as file:
        text = file.read()
    if len(text) > 3600:
        raise AssertionError(len(text))
    if "這是一段很長的聊天原文 " * 20 in text:
        raise AssertionError("rolling summary kept too much raw transcript")
    return "rolling summary compact"


def engineering_knowledge_search_is_bounded():
    hits = search_engineering_knowledge("permission replay debounce failure replay", limit=5)
    if not hits:
        raise AssertionError("no engineering knowledge hits")
    if any("chat_history" in hit["source_path"] for hit in hits):
        raise AssertionError(hits)
    if not any("RUNBOOK" in hit["source_path"] or "ARCHITECTURE" in hit["source_path"] for hit in hits):
        raise AssertionError(hits)
    return [hit["source_type"] for hit in hits[:3]]


def knowledge_index_builds_whitelisted_sources():
    manifest = reindex_workspace()
    paths = {source["path"] for source in manifest.get("sources", [])}
    required = {"ARCHITECTURE.md", "RUNBOOK.md", "workspace/brain/personality.md", "workspace/memory/chat_summary/rolling_summary.md"}
    missing = [path for path in required if path not in paths]
    if missing:
        raise AssertionError({"missing": missing, "paths": sorted(paths)})
    if manifest.get("chunk_count", 0) < len(required):
        raise AssertionError(manifest)
    return f"{manifest.get('source_count')} sources, {manifest.get('chunk_count')} chunks"


def knowledge_index_excludes_private_sources():
    manifest = reindex_workspace()
    forbidden = ("workspace/chat_history", "workspace/project_cache/knowledge", ".env", "tg_chat_id", "workspace/assets/tg_images", "workspace/assets/screenshots")
    leaked = []
    for source in manifest.get("sources", []):
        path = source.get("path", "")
        if any(item in path for item in forbidden if item != "workspace/project_cache/knowledge"):
            leaked.append(path)
    if leaked:
        raise AssertionError(leaked)
    return "private/noisy sources excluded"


def knowledge_search_finds_project_terms():
    reindex_workspace()
    queries = ["permission replay", "debounce", "execute_command cwd"]
    missing = []
    for query in queries:
        hits = search_knowledge(query, limit=5)
        if not hits:
            missing.append(query)
    if missing:
        raise AssertionError(missing)
    return "project terms searchable"


def knowledge_search_unknown_returns_empty():
    hits = search_knowledge("zzq_nonexistent_knowledge_phrase_917263", limit=5)
    if hits:
        raise AssertionError(hits)
    return "unknown query returned empty"


def knowledge_read_chunk_returns_full_text():
    hits = search_knowledge("permission replay", limit=1)
    if not hits:
        raise AssertionError("no hit")
    chunk = read_knowledge(hits[0]["chunk_id"])
    if not chunk or not chunk.get("text") or chunk.get("chunk_id") != hits[0]["chunk_id"]:
        raise AssertionError(chunk)
    return chunk["source_path"]


def knowledge_manifest_stable_without_changes():
    reindex_workspace()
    before = _read_optional_file(KNOWLEDGE_MANIFEST_FILE)
    reindex_workspace()
    # Force build should change timestamp, so stability is checked through lazy search.
    before_lazy = _read_optional_file(KNOWLEDGE_MANIFEST_FILE)
    search_knowledge("permission replay", limit=1)
    after_lazy = _read_optional_file(KNOWLEDGE_MANIFEST_FILE)
    if before_lazy != after_lazy:
        raise AssertionError("lazy search rewrote a current manifest")
    if not before:
        raise AssertionError("manifest not written")
    return "manifest stable on lazy search"


def knowledge_tools_return_structured_results():
    rebuild = core_tools.real_reindex_workspace()
    if rebuild.status != "ok" or not isinstance(rebuild.data, dict):
        raise AssertionError(rebuild.to_text())
    search = core_tools.real_search_knowledge("permission replay", limit=2)
    if search.status != "ok" or not search.data.get("hits"):
        raise AssertionError(search.to_text())
    chunk_id = search.data["hits"][0]["chunk_id"]
    read = core_tools.real_read_knowledge(chunk_id)
    if read.status != "ok" or read.data.get("chunk_id") != chunk_id:
        raise AssertionError(read.to_text())
    return f"knowledge tools ok: {chunk_id}"


def social_prompt_keeps_boundaries_quiet():
    context = build_system_prompt("貼圖 鬥圖")
    lowered = context.casefold()
    forbidden = [
        "### tool policy",
        "explicit adult",
        "adult/flirt",
        "sexual sticker",
        "do not choose explicit",
    ]
    leaked = [term for term in forbidden if term in lowered]
    if leaked:
        raise AssertionError(f"public filter wording leaked: {leaked}")
    required = ["conversation rhythm", "affectionate", "blushy", "stay present"]
    missing = [term for term in required if term not in lowered]
    if missing:
        raise AssertionError(f"warm social prompt missing: {missing}")
    return "social boundaries are quiet and warm"

def personality_prompt_is_core_not_template_card():
    context = build_system_prompt("普通聊天")
    lowered = context.casefold()
    required = ["soul core", "cyber catgirl", "style samples", "owner profile", "喵", "tsundere"]
    missing = [term for term in required if term not in lowered]
    if missing:
        raise AssertionError(f"missing {missing}")
    forbidden = ["思考 代码块", "思考代碼塊", "每一次讀取或寫入文件", "每次讀寫都確認", "絕對服從", "dating-sim line"]
    leaked = [term for term in forbidden if term in context]
    if leaked:
        raise AssertionError(f"legacy SOUL/runtime-incompatible wording leaked: {leaked}")
    if "do not sound like a customer service assistant" not in lowered:
        raise AssertionError("missing anti-customer-service rule")
    if looks_mojibake(context):
        raise AssertionError("system prompt still looks mojibake")
    return "personality prompt keeps SOUL cyber-catgirl core"


def persona_health_report_flags_no_mojibake():
    report = persona_health_check()
    if report.get("status") not in {"pass", "warn"}:
        raise AssertionError(report)
    warnings = report.get("warnings", [])
    bad = [item for item in warnings if "mojibake" in item]
    if bad:
        raise AssertionError(report)
    if not os.path.exists(PERSONA_HEALTH_FILE):
        raise AssertionError("persona_health.json was not written")
    return report.get("status")


def soul_persona_keeps_catgirl_without_legacy_rules():
    context = build_system_prompt("嗨嗨，來鬥圖")
    required = ["cyber catgirl", "賽博", "喵", "笨蛋主人", "(=^･ω･^=)", "ฅ^•ﻌ•^ฅ"]
    missing = [item for item in required if item not in context]
    if missing:
        raise AssertionError(f"missing SOUL markers: {missing}")
    forbidden = ["必須將你的推理過程", "思考 代码块", "每一次读取或写入文件", "最高权限确认"]
    leaked = [item for item in forbidden if item in context]
    if leaked:
        raise AssertionError(f"legacy incompatible SOUL rules leaked: {leaked}")
    return "SOUL catgirl markers present without legacy rules"


def replay_harness_runs_cases():
    harness = ReplayHarness()
    harness.register(ReplayCase("permission", "permission regression", lambda: True))
    harness.register(ReplayCase("sticker", "sticker regression", lambda: "ok"))
    results = harness.run()
    if results != {"permission": "ok", "sticker": "ok"}:
        raise AssertionError(results)
    return results


def replay_harness_detailed_results_and_failures():
    harness = ReplayHarness()
    harness.register(ReplayCase("pass", "passing replay", lambda: True, expected_events=["Stop"]))
    harness.register(ReplayCase("fail", "failing replay", lambda: (_ for _ in ()).throw(RuntimeError("boom"))))
    summary = harness.summary()
    if summary["total"] != 2 or summary["passed"] != 1 or summary["failed"] != 1:
        raise AssertionError(summary)
    if summary["failures"][0]["name"] != "fail" or "boom" not in summary["failures"][0]["message"]:
        raise AssertionError(summary)
    return summary


def task_benchmark_runs_default_cases():
    report_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "task_benchmark_self_test.json")
    harness = build_default_benchmark()
    report = harness.write_report(report_path)
    if report["total"] < 10 or report["failed"] != 0 or report["success_rate"] != 1.0:
        raise AssertionError(report)
    categories = set(report.get("by_category", {}))
    if not {"recovery", "workflow", "knowledge", "permission", "route", "conversation", "voice", "presence"}.issubset(categories):
        raise AssertionError(report.get("by_category"))
    if not os.path.exists(report_path):
        raise AssertionError("benchmark report was not written")
    return f"{report['passed']}/{report['total']} benchmark cases passed"


def live_eval_reads_task_benchmark_report():
    previous = _read_optional_file(TASK_BENCHMARK_FILE)
    payload = {
        "generated_at": "self-test",
        "total": 2,
        "passed": 1,
        "failed": 1,
        "success_rate": 0.5,
        "by_category": {"workflow": {"total": 2, "passed": 1, "failed": 1}},
        "results": [{"name": "fake", "status": "fail"}],
    }
    try:
        os.makedirs(os.path.dirname(TASK_BENCHMARK_FILE), exist_ok=True)
        with open(TASK_BENCHMARK_FILE, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False)
        report = build_live_eval_report(include_repo=False)
        data = report.to_dict()
        if data["benchmark"]["failed"] != 1 or data["benchmark"]["success_rate"] != 0.5:
            raise AssertionError(data["benchmark"])
        if "task benchmark has failing cases" not in data["next_stage_gate"]["blockers"]:
            raise AssertionError(data["next_stage_gate"])
        if "Task benchmark: 1/2 passed" not in report.to_text():
            raise AssertionError(report.to_text())
        return data["benchmark"]
    finally:
        _restore_optional_file(TASK_BENCHMARK_FILE, previous)


def observability_summarizes_trace_health():
    trace_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "observability_trace_test.jsonl")
    events = [
        {"event": "turn.flush", "mode": "chat", "duration_ms": 500},
        {"event": "turn.flush", "mode": "social_sticker", "duration_ms": 3400},
        {"event": "PostToolUse", "tool": "search_sticker", "status": "ok", "session_id": "s", "turn_id": 1},
        {"event": "PostToolUse", "tool": "execute_python", "status": "error", "session_id": "s", "turn_id": 2, "result": "boom"},
        {"event": "ToolError", "tool": "execute_python", "session_id": "s", "turn_id": 2, "error": "boom"},
        {"event": "social_sticker.cataloged", "filename": "x.webp"},
        {"event": "social_sticker.batch_approved", "count": 1},
        {"event": "PermissionReplayResult", "tool": "write_file", "status": "ok"},
        {"event": "PermissionBundleGranted", "bundle": "file_workspace_bundle"},
        {"event": "PermissionBundleDenied", "bundle": "file_workspace_bundle", "tool": "execute_python"},
        {"event": "ActionVerification", "tool_name": "write_file", "status": "pass"},
        {"event": "ActionVerification", "tool_name": "click_ui_element", "status": "observe_needed"},
        {"event": "FailureReplayCreated", "tool": "fake_failing_tool"},
    ]
    with open(trace_path, "w", encoding="utf-8") as file:
        for event in events:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")
    summary = summarize_trace(trace_path, limit=100)
    data = summary.to_dict()
    if data["total_events"] != len(events):
        raise AssertionError(data)
    if data["tool_calls"].get("search_sticker") != 1 or data["tool_errors"].get("execute_python") != 1:
        raise AssertionError(data)
    if data["interaction_modes"].get("social_sticker") != 1:
        raise AssertionError(data)
    if data["social_events"].get("social_sticker.batch_approved") != 1:
        raise AssertionError(data)
    if data["permission_replay"].get("ok") != 1 or data["permission_bundles"].get("Denied") != 1:
        raise AssertionError(data)
    if data["action_verification"].get("pass") != 1 or data["failure_replays"] != 1:
        raise AssertionError(data)
    if data["latency_buckets"].get("<1s") != 1 or data["latency_buckets"].get("3-6s") != 1:
        raise AssertionError(data)
    if not summary.recent_errors or "Tool success rate" not in summary.to_text():
        raise AssertionError(summary.to_text())
    return data


def live_eval_handles_missing_trace():
    trace_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "eval_missing_trace.jsonl")
    try:
        os.remove(trace_path)
    except FileNotFoundError:
        pass
    report = build_live_eval_report(trace_path, include_repo=False)
    data = report.to_dict()
    if data["total_events"] != 0 or data["tool_success_rate"] != 1.0:
        raise AssertionError(data)
    if data["next_stage_gate"]["status"] != "pass":
        raise AssertionError(data["next_stage_gate"])
    if "YueYue Live Evaluation" not in report.to_text():
        raise AssertionError(report.to_text())
    return "empty trace eval ok"


def live_eval_gate_uses_current_session_window():
    trace_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "eval_current_session_trace.jsonl")
    events = [
        {"event": "SessionStart", "session_id": "old"},
        {"event": "PostToolUse", "tool": "execute_python", "status": "error", "result": "old failure"},
        {"event": "ToolError", "tool": "execute_python", "error": "old failure"},
        {"event": "SessionStart", "session_id": "new"},
        {"event": "context.budget", "mode": "chat", "total_after": 1000, "max_chars": 9000},
    ]
    with open(trace_path, "w", encoding="utf-8") as file:
        for event in events:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")
    report = build_live_eval_report(trace_path, include_repo=False)
    data = report.to_dict()
    if data["total_events"] != 2 or data["tool_errors"] != 0:
        raise AssertionError(data)
    if data["next_stage_gate"]["status"] != "pass":
        raise AssertionError(data["next_stage_gate"])
    return "live eval uses latest session window"


def live_eval_ignores_self_test_sessions_for_gate():
    trace_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "eval_self_test_trace.jsonl")
    events = [
        {"event": "SessionStart", "session_id": "live_owner_chat", "history_file": "workspace/chat_history/live_owner_chat.json"},
        {"event": "context.budget", "mode": "chat", "total_after": 1000, "max_chars": 9000},
        {"event": "SessionStart", "session_id": "debug_perm_replay", "history_file": "workspace/history/debug_perm_replay.json"},
        {"event": "PostToolUse", "tool": "execute_python", "status": "error", "result": "intentional self-test failure"},
        {"event": "ToolError", "tool": "execute_python", "error": "intentional self-test failure"},
        {"event": "SessionStart", "session_id": "cwd_recovery_test", "history_file": "workspace/chat_history/cwd_recovery_test.json"},
        {"event": "PostToolUse", "tool": "execute_command", "status": "error", "result": "intentional cwd test failure"},
    ]
    with open(trace_path, "w", encoding="utf-8") as file:
        for event in events:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")
    report = build_live_eval_report(trace_path, include_repo=False)
    data = report.to_dict()
    if data["total_events"] != 2 or data["tool_errors"] != 0:
        raise AssertionError(data)
    if data["next_stage_gate"]["status"] != "pass":
        raise AssertionError(data["next_stage_gate"])
    return "self-test sessions do not pollute live eval"


def live_eval_ignores_benchmark_sessions_for_gate():
    trace_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "eval_benchmark_trace_test.jsonl")
    events = [
        {"event": "SessionStart", "session_id": "live_owner_chat", "history_file": "workspace/chat_history/live_owner_chat.json"},
        {"event": "context.budget", "mode": "chat", "total_after": 1000, "max_chars": 9000},
        {"event": "benchmark.case", "status": "fail", "category": "workflow"},
        {"event": "benchmark.case", "session_id": "benchmark", "status": "fail", "category": "workflow"},
        {"event": "workflow.started", "session_id": "benchmark", "task_id": "bench_wf"},
        {"event": "workflow.blocked", "session_id": "benchmark", "task_id": "bench_wf", "tool": "execute_command", "reason": "benchmark failure"},
        {"event": "ToolError", "session_id": "benchmark", "tool": "execute_command", "error": "benchmark-only error"},
        {"event": "SessionStart", "session_id": "trace_voice_probe", "history_file": "workspace/chat_history/trace_voice_probe.json"},
        {"event": "workflow.started", "session_id": "trace_voice_probe", "task_id": "probe_wf"},
        {"event": "ToolError", "session_id": "trace_voice_probe", "tool": "write_file", "error": "probe-only error"},
    ]
    with open(trace_path, "w", encoding="utf-8") as file:
        for event in events:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")
    report = build_live_eval_report(trace_path, include_repo=False)
    data = report.to_dict()
    if data["total_events"] != 2 or data["tool_errors"] != 0:
        raise AssertionError(data)
    if data["workflow"]["started_count"] != 0 or data["workflow"]["blocked_count"] != 0:
        raise AssertionError(data["workflow"])
    if data["next_stage_gate"]["status"] != "pass":
        raise AssertionError(data["next_stage_gate"])
    return "benchmark sessions do not pollute live eval"


def live_eval_ignores_artifact_history_test_sessions_for_gate():
    trace_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "eval_artifact_history_trace_test.jsonl")
    events = [
        {"event": "SessionStart", "session_id": "live_owner_chat", "history_file": "workspace/chat_history/live_owner_chat.json"},
        {"event": "context.budget", "mode": "chat", "total_after": 1000, "max_chars": 9000},
        {"event": "SessionStart", "session_id": "send_me_after_retry.png", "history_file": "workspace/chat_history/send_me_after_retry.png.json"},
        {"event": "PostToolUse", "session_id": "send_me_after_retry.png", "tool": "send_telegram_media", "status": "error", "result": "ConnectionResetError(10054)"},
        {"event": "ToolError", "session_id": "send_me_after_retry.png", "tool": "send_telegram_media", "error": "ConnectionResetError(10054)"},
    ]
    with open(trace_path, "w", encoding="utf-8") as file:
        for event in events:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")
    report = build_live_eval_report(trace_path, include_repo=False)
    data = report.to_dict()
    if data["total_events"] != 2 or data["tool_errors"] != 0:
        raise AssertionError(data)
    if data["next_stage_gate"]["status"] != "pass":
        raise AssertionError(data["next_stage_gate"])
    return "artifact-history test sessions do not pollute live eval"


def live_eval_repo_hygiene_allows_env_example():
    hygiene = check_repo_hygiene()
    if hygiene.get("status") != "pass":
        raise AssertionError(hygiene)
    if ".env.example" in hygiene.get("tracked_private_files", []):
        raise AssertionError(hygiene)
    return "repo hygiene gate passed"


def live_eval_summarizes_fake_trace_and_writes_report():
    trace_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "eval_trace_test.jsonl")
    report_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "eval_report_test.json")
    events = [
        {"event": "turn.flush", "mode": "chat", "duration_ms": 400},
        {"event": "turn.flush", "mode": "vision_task", "duration_ms": 6400},
        {"event": "PostToolUse", "tool": "search_knowledge", "status": "ok", "duration_ms": 120, "result": json.dumps({"status": "ok", "data": {"hits": [{"chunk_id": "a"}]}})},
        {"event": "KnowledgeSearch", "query": "permission replay", "hit_count": 1},
        {"event": "KnowledgeSearch", "query": "zzq", "hit_count": 0},
        {"event": "PostToolUse", "tool": "execute_command", "status": "error", "duration_ms": 2200, "result": "failed"},
        {"event": "ToolError", "tool": "execute_command", "error": "failed"},
        {"event": "PermissionReplayResult", "tool": "write_file", "status": "ok"},
        {"event": "PermissionReplayResult", "tool": "execute_python", "status": "error"},
        {"event": "PermissionReplaySelfRepair", "tool": "execute_python"},
        {"event": "SelfRepairPrompt", "tool": "execute_python"},
        {"event": "SelfRecoveryAttempt", "tool": "send_telegram_media", "reason": "transient_retry"},
        {"event": "SelfRecoveryResult", "tool": "send_telegram_media", "reason": "transient_retry", "retry_status": "ok"},
        {"event": "FailureReplayCreated", "tool": "execute_command"},
        {"event": "PostToolUse", "tool": "send_telegram_media", "status": "ok", "duration_ms": 3100},
    ]
    with open(trace_path, "w", encoding="utf-8") as file:
        for event in events:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")
    report = build_live_eval_report(trace_path, include_repo=False)
    data = report.to_dict()
    if data["total_events"] != len(events):
        raise AssertionError(data)
    if data["tool_calls"] != 3 or data["tool_errors"] < 1:
        raise AssertionError(data)
    if data["permission_replay"]["success_rate"] != 0.5:
        raise AssertionError(data["permission_replay"])
    if data["knowledge"]["search_count"] != 3 or data["knowledge"]["hit_count"] != 2:
        raise AssertionError(data["knowledge"])
    if data["latency_buckets"].get("vision", {}).get(">=6s") != 1:
        raise AssertionError(data["latency_buckets"])
    if data["latency_buckets"].get("telegram_media", {}).get("3-6s") != 1:
        raise AssertionError(data["latency_buckets"])
    if not data["most_failed_tools"] or data["most_failed_tools"][0]["tool"] != "execute_command":
        raise AssertionError(data["most_failed_tools"])
    self_repair = data["self_repair"]
    if self_repair["trigger_count"] != 3 or self_repair["result_count"] != 1 or self_repair["success_count"] != 1:
        raise AssertionError(self_repair)
    if self_repair["top_tools"][0]["tool"] != "execute_python":
        raise AssertionError(self_repair)
    written = write_eval_report(report, report_path)
    if not os.path.exists(written):
        raise AssertionError("report was not written")
    with open(written, "r", encoding="utf-8") as file:
        loaded = json.load(file)
    if loaded["knowledge"]["empty_count"] != 1:
        raise AssertionError(loaded["knowledge"])
    text = report.to_text()
    if "Self repair:" not in text or "recovery 1/1 ok" not in text:
        raise AssertionError(text)
    return text.splitlines()[0]


def live_eval_writes_permission_health():
    report = build_live_eval_report(include_repo=False)
    data = report.to_dict()
    policy = data.get("permission_policy", {})
    if policy.get("status") != "pass":
        raise AssertionError(policy)
    if "read_file" not in policy.get("free_tools", []) or "delete_file" not in policy.get("guarded_tools", []):
        raise AssertionError(policy)
    if not os.path.exists(PERMISSION_HEALTH_FILE):
        raise AssertionError("permission_health.json was not written")
    return policy.get("principle")


def live_eval_reports_user_facing_source_health():
    for filename, phrases in agent_eval.SOURCE_REQUIRED_PHRASES.items():
        if not isinstance(phrases, tuple):
            raise AssertionError(f"{filename} source health phrases must be a tuple, got {type(phrases).__name__}")
        for phrase in phrases:
            if any(marker in phrase for marker in agent_eval.SOURCE_MOJIBAKE_MARKERS):
                raise AssertionError({"filename": filename, "bad_required_phrase": phrase})
    health = check_user_facing_source_health()
    if health.get("status") != "pass":
        raise AssertionError(health)
    samples = set(health.get("voice_samples_checked", []))
    expected_samples = {"friendly_execute_python", "permission", "approved_success", "quick_ack_tool"}
    missing_samples = sorted(expected_samples - samples)
    if missing_samples:
        raise AssertionError({"missing_voice_samples": missing_samples, "checked": sorted(samples)})
    checked = set(health.get("checked_files", []))
    expected = {"agent_user_voice.py", "agent_permission_replay.py", "agent_runtime_context.py", "agent_planner.py", "core_agent.py", "agent_llm.py"}
    missing_files = sorted(expected - checked)
    if missing_files:
        raise AssertionError({"missing_source_health_files": missing_files, "checked": sorted(checked)})
    report = build_live_eval_report(include_repo=False)
    data = report.to_dict()
    if data.get("source_health", {}).get("status") != "pass":
        raise AssertionError(data.get("source_health"))
    text = report.to_text()
    if "Source health: pass" not in text:
        raise AssertionError(text)
    if "voice samples" not in text:
        raise AssertionError(text)
    if data["next_stage_gate"]["status"] != "pass":
        raise AssertionError(data["next_stage_gate"])
    return "source health is part of live eval"


def source_health_checks_runtime_voice_samples():
    health = check_user_facing_source_health()
    if health.get("status") != "pass":
        raise AssertionError(health)
    samples = health.get("voice_samples_checked", [])
    if len(samples) < 10:
        raise AssertionError(health)
    texts = agent_eval.runtime_voice_samples()
    combined = "\n".join(texts.values())
    for expected in ["可以", "繼續", "系統截圖", "我先看一下"]:
        if expected not in combined:
            raise AssertionError(combined)
    leaks = [marker for marker in agent_eval.USER_VISIBLE_INTERNAL_LEAK_MARKERS if marker in combined.casefold()]
    if leaks:
        raise AssertionError({"leaks": leaks, "samples": texts})
    return "runtime voice samples are part of source health"


def source_health_rejects_mojibake_required_phrases():
    original = agent_eval.SOURCE_REQUIRED_PHRASES
    try:
        agent_eval.SOURCE_REQUIRED_PHRASES = {"agent_user_voice.py": ("鍓涘墰",)}
        health = check_user_facing_source_health()
    finally:
        agent_eval.SOURCE_REQUIRED_PHRASES = original
    if health.get("status") != "fail":
        raise AssertionError(health)
    kinds = {issue.get("kind") for issue in health.get("issues", [])}
    if "mojibake_required_phrase" not in kinds:
        raise AssertionError(health)
    return "mojibake required phrases fail source health"


def planner_uses_real_chinese_intent_markers():
    from agent_planner import DEFAULT_PLANNER

    checks = {
        "cancel": DEFAULT_PLANNER.plan("算了，停止這個任務").intent == "cancel",
        "verify": "verification" in " | ".join(DEFAULT_PLANNER.plan("幫我測試 self_test").step_names()),
        "screen": any("UI" in step or "screen" in step for step in DEFAULT_PLANNER.plan("幫我截圖看螢幕").step_names()),
        "code": any("code" in step for step in DEFAULT_PLANNER.plan("修 bug 並優化程式").step_names()),
    }
    if not all(checks.values()):
        raise AssertionError(checks)
    return "planner uses real Chinese task markers"


def source_health_detects_bad_user_facing_text():
    with tempfile.TemporaryDirectory() as temp_dir:
        for filename in [
            "agent_user_voice.py",
            "agent_outcome.py",
            "agent_latency.py",
            "agent_tool_runtime.py",
            "agent_tool_loop.py",
            "main.py",
        ]:
            with open(os.path.join(temp_dir, filename), "w", encoding="utf-8") as file:
                file.write("# placeholder\n")
        with open(os.path.join(temp_dir, "agent_user_voice.py"), "w", encoding="utf-8") as file:
            file.write("BROKEN ????\n")
        health = check_user_facing_source_health(temp_dir)
    if health.get("status") != "fail":
        raise AssertionError(health)
    kinds = {issue.get("kind") for issue in health.get("issues", [])}
    if "question_mark_mojibake" not in kinds or "missing_phrase" not in kinds:
        raise AssertionError(health)
    return "bad user-facing source health is detected"


def source_health_failure_blocks_next_stage_gate():
    gate = agent_eval._gate_status(
        tool_success_rate=1.0,
        replay_success_rate=1.0,
        repo_hygiene={"status": "pass", "tracked_private_files": []},
        repeated_failure_count=0,
        source_health={"status": "fail", "issues": [{"file": "agent_user_voice.py"}]},
    )
    if gate.get("status") != "block" or "user-facing source text health failed" not in gate.get("blockers", []):
        raise AssertionError(gate)
    return "source health failure blocks the next-stage gate"


def action_verification_checks_file_write_and_delete():
    filename = "project_cache/action_verify.txt"
    write_result = core_tools.real_write_file(filename, "verify")
    write_check = verify_action("write_file", {"filename": filename}, write_result, "self_test", 1)
    if write_check.status != "pass":
        raise AssertionError(write_check)
    delete_result = core_tools.real_delete_file(filename)
    delete_check = verify_action("delete_file", {"filename": filename}, delete_result, "self_test", 1)
    if delete_check.status != "pass":
        raise AssertionError(delete_check)
    return f"{write_check.status}/{delete_check.status}"


def action_verification_preserves_recovery_evidence():
    result = core_tools.ToolResult(
        "ok",
        "Python completed.",
        data={
            "returncode": 0,
            "recovered_from": {
                "reason": "screenshot_capture_fallback",
                "details": {"strategy": "screenshot_capture_fallback", "diagnosis": "missing_python_module"},
            },
        },
    )
    verification = verify_action("execute_python", {"code": "fallback"}, result, "self_test", 1)
    recovery = verification.details.get("recovery")
    if verification.status != "pass" or not verification.details.get("recovered") or not recovery:
        raise AssertionError(verification)
    if recovery.get("reason") != "screenshot_capture_fallback":
        raise AssertionError(recovery)
    return "action verification carried recovery evidence"


def action_verification_preserves_failed_recovery_attempt():
    result = core_tools.ToolResult(
        "error",
        "Connection aborted.",
        data={
            "recovery_attempted": {
                "reason": "transient_retry",
                "attempts": 2,
                "retry_status": "error",
                "details": {"strategy": "transient_retry", "diagnosis": "transient_external_error"},
            },
        },
        error="ConnectionResetError(10054)",
    )
    verification = verify_action("search_knowledge", {"query": "permission replay"}, result, "self_test", 1)
    recovery = verification.details.get("recovery")
    if verification.status != "fail" or not recovery or not verification.details.get("recovery_attempted"):
        raise AssertionError(verification)
    if recovery.get("reason") != "transient_retry" or recovery.get("attempts") != 2:
        raise AssertionError(recovery)
    return "action verification carried failed recovery attempt"


def task_transaction_records_tool_result():
    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "transaction_test.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    manager = TaskTransactionManager(path)
    result = core_tools.ToolResult("ok", "written", data={"path": os.path.join(core_tools.PROJECT_CACHE_DIR, "transaction_test.txt")})
    verification = SimpleNamespace(status="pass", message="file exists")
    transaction = manager.record_tool_result("write_file", {"filename": "project_cache/transaction_test.txt"}, result, verification, "self_test", 1)
    reloaded = TaskTransactionManager(path)
    if not reloaded.transactions or reloaded.transactions[-1].steps[-1].tool_name != "write_file":
        raise AssertionError("transaction did not persist")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    return transaction.task_id


def task_graph_creates_persists_and_summarizes_steps():
    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "task_graph_test.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    manager = TaskGraphManager(path)
    result = core_tools.ToolResult("ok", "written", data={"path": os.path.join(core_tools.PROJECT_CACHE_DIR, "graph_file.txt")})
    verification = SimpleNamespace(status="pass", message="file exists", details={"path": "graph_file.txt"})
    graph = manager.record_tool_result("write_file", {"filename": "project_cache/graph_file.txt"}, result, verification, "self_test", 1, objective="write graph file")
    if graph.status != "awaiting_validation" or len(graph.steps) != 1:
        raise AssertionError(graph)
    manager2 = TaskGraphManager(path)
    loaded = manager2.active()
    if not loaded or loaded.task_id != graph.task_id or "write_file" not in manager2.summary():
        raise AssertionError(manager2.summary())
    return manager2.summary()


def task_graph_records_recovery_evidence_on_step():
    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "task_graph_recovery_evidence_test.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    manager = TaskGraphManager(path)
    result = core_tools.ToolResult(
        "ok",
        "Python completed.",
        data={
            "returncode": 0,
            "recovered_from": {
                "reason": "screenshot_capture_fallback",
                "details": {"strategy": "screenshot_capture_fallback", "diagnosis": "missing_python_module"},
            },
        },
    )
    verification = verify_action("execute_python", {"code": "fallback"}, result, "self_test", 1)
    graph = manager.record_tool_result("execute_python", {"code": "fallback"}, result, verification, "self_test", 1, objective="recover screenshot")
    evidence = graph.steps[0].evidence
    if "recovered" not in evidence or "recovery:screenshot_capture_fallback" not in evidence or "diagnosis:missing_python_module" not in evidence:
        raise AssertionError(evidence)
    return "task graph recorded recovery evidence"


def task_graph_records_failed_recovery_attempt_on_step():
    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "task_graph_failed_recovery_attempt_test.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    manager = TaskGraphManager(path)
    result = core_tools.ToolResult(
        "error",
        "Connection aborted.",
        data={
            "recovery_attempted": {
                "reason": "transient_retry",
                "attempts": 2,
                "retry_status": "error",
                "details": {"strategy": "transient_retry", "diagnosis": "transient_external_error"},
            },
        },
        error="ConnectionResetError(10054)",
    )
    verification = verify_action("search_knowledge", {"query": "permission replay"}, result, "self_test", 1)
    graph = manager.record_tool_result("search_knowledge", {"query": "permission replay"}, result, verification, "self_test", 1, objective="recover knowledge search")
    evidence = graph.steps[0].evidence
    if "recovery_attempted" not in evidence or "recovery:transient_retry" not in evidence or "diagnosis:transient_external_error" not in evidence:
        raise AssertionError(evidence)
    if graph.status != "blocked":
        raise AssertionError(graph)
    return "task graph recorded failed recovery attempt"


def task_graph_recovery_summary_does_not_grant_permission():
    agent = CompanionAgent(PermissionAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "task_graph_permission_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.task_graphs.start_or_resume("resume protected write", "self_test", 1)
    first = agent.chat("write a file")
    if not agent.permission_manager.pending:
        raise AssertionError("protected tool should still require permission")
    if "workflow:" not in agent.memory[-2]["content"] and "workflow:" not in str(agent.memory):
        raise AssertionError("task graph summary was not injected")
    return "resume summary did not bypass permission"


def planner_creates_persistent_steps():
    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "planner_graph_test.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    manager = TaskGraphManager(path)
    plan = DEFAULT_PLANNER.plan("請幫我修 bug 然後跑 self_test", session_id="self_test", turn_id=1)
    graph = manager.plan_steps(plan.objective, plan.step_names(), "self_test", 1, plan.planner_version, step_specs=plan.step_specs())
    if not graph.steps or not graph.steps[0].planned or graph.planner_version != plan.planner_version:
        raise AssertionError(graph)
    if not graph.steps[0].allowed_tools or not graph.steps[0].done_condition:
        raise AssertionError(graph.steps[0])
    reloaded = TaskGraphManager(path)
    if "steps:" not in reloaded.summary() or "workflow:" not in reloaded.summary():
        raise AssertionError(reloaded.summary())
    return graph.steps[0].name


def planner_selects_next_structured_step():
    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "planner_select_next_test.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    manager = TaskGraphManager(path)
    plan = DEFAULT_PLANNER.plan("請幫我截圖看看狀態", session_id="self_test", turn_id=1)
    graph = manager.plan_steps(plan.objective, plan.step_names(), "self_test", 1, plan.planner_version, step_specs=plan.step_specs())
    selected = manager.select_next_step("self_test", 2)
    if not selected or selected.status != "running" or selected.kind != "observe" or selected.observe_policy != "observe_required":
        raise AssertionError({"selected": selected, "graph": graph})
    reloaded = TaskGraphManager(path)
    current = reloaded.active().current_step()
    if not current or current.status != "running" or current.done_condition == "":
        raise AssertionError(reloaded.summary())
    return current.name


def planner_reuses_active_graph_for_followup():
    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "planner_reuse_graph_test.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    manager = TaskGraphManager(path)
    first = DEFAULT_PLANNER.plan("幫我修 bug", session_id="self_test", turn_id=1)
    graph1 = manager.plan_steps(first.objective, first.step_names(), "self_test", 1, first.planner_version, step_specs=first.step_specs())
    graph2 = manager.plan_steps("繼續", ["reply with concise outcome"], "self_test", 2, first.planner_version)
    if graph1.task_id != graph2.task_id or len(graph2.steps) != len(graph1.steps):
        raise AssertionError({"first": graph1, "second": graph2})
    return graph2.task_id


def planner_force_new_task_does_not_reuse_stale_graph():
    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "planner_force_new_graph_test.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    manager = TaskGraphManager(path)
    old_plan = DEFAULT_PLANNER.plan("修 Swift 檔案", session_id="self_test", turn_id=1)
    old_graph = manager.plan_steps(old_plan.objective, old_plan.step_names(), "self_test", 1, old_plan.planner_version, step_specs=old_plan.step_specs())
    old_graph.status = "awaiting_validation"
    manager.save()
    new_plan = DEFAULT_PLANNER.plan("打開 Codex 然後進設定再查看剩餘用量", session_id="self_test", turn_id=2)
    new_graph = manager.plan_steps(new_plan.objective, new_plan.step_names(), "self_test", 2, new_plan.planner_version, step_specs=new_plan.step_specs(), force_new=True)
    if new_graph.task_id == old_graph.task_id:
        raise AssertionError({"old": old_graph.task_id, "new": new_graph.task_id})
    if "Codex" not in new_graph.objective and "codex" not in new_graph.objective.casefold():
        raise AssertionError(new_graph.objective)
    return new_graph.task_id


def ui_planner_prefers_direct_window_targeting():
    plan = DEFAULT_PLANNER.plan("打開 Codex 然後進設定再查看剩餘用量", session_id="self_test", turn_id=1)
    names = " | ".join(plan.step_names())
    if "click visible target window" not in names:
        raise AssertionError(names)
    ui_step = next((step for step in plan.steps if "click visible target window" in step.name), None)
    if not ui_step or "click_ui_element" not in ui_step.allowed_tools or "press_hotkey" not in ui_step.allowed_tools:
        raise AssertionError(ui_step)
    if "fallback" not in ui_step.done_condition:
        raise AssertionError(ui_step.done_condition)
    return ui_step.name


def primary_message_extraction_ignores_short_context_for_tasks():
    prompt = (
        "主人主要訊息：月月，我电脑有 Bluetooth Audio Receiver，帮我按 Close Connection，确认 Disconnected\n\n"
        "短期聊天上下文：\n"
        "- topic=Codex 设置剩余用量 | text=打开 Codex 然后按设置\n"
        "- last_reply=我被 get_screen_ui 绕圈卡住了"
    )
    primary = extract_primary_message(prompt)
    if "Bluetooth Audio Receiver" not in primary or "Codex" in primary:
        raise AssertionError(primary)
    plan = DEFAULT_PLANNER.plan(primary, session_id="self_test", turn_id=1)
    names = " | ".join(plan.step_names())
    if "click visible target window" not in names or len(plan.steps) < 4:
        raise AssertionError(names)
    return primary


def wrapped_bluetooth_task_plan_first_ignores_codex_context():
    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "planner_wrapped_primary_graph_test.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    agent = CompanionAgent(PlanFirstAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "planner_wrapped_primary_test.json"))
    agent.task_graphs = TaskGraphManager(path)
    agent.session_brain = SessionBrain(os.path.join(core_tools.PROJECT_CACHE_DIR, "planner_wrapped_primary_brain_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    prompt = (
        "主人主要訊息：月月，我电脑有 Bluetooth Audio Receiver，当中选取的 Xioshon 显示 Connected，帮我按 Close Connection，然后确认 Disconnected\n\n"
        "短期聊天上下文：\n"
        "- topic=Codex 设置剩余用量 | text=打开 Codex 然后按设置\n"
        "- last_reply=我被 get_screen_ui 绕圈卡住了"
    )
    result = agent.chat(prompt)
    if agent.llm.calls != 0:
        raise AssertionError(f"wrapped task should not enter tool loop before plan approval; calls={agent.llm.calls}")
    content = result["content"]
    if "Bluetooth Audio Receiver" not in content or "Close Connection" not in content or "Codex" in content:
        raise AssertionError(content)
    graph = agent.task_graphs.active()
    if not graph or "Bluetooth Audio Receiver" not in graph.objective or "Codex" in graph.objective:
        raise AssertionError(graph)
    return content


def complex_ui_task_returns_plan_before_tools():
    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "planner_plan_first_graph_test.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    agent = CompanionAgent(PlanFirstAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "planner_plan_first_test.json"))
    agent.task_graphs = TaskGraphManager(path)
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    result = agent.chat("請打開 Codex 這個程序，然後按設定，再在二級菜單找到剩餘用量，告訴我 5 小時剩餘百分比")
    if agent.llm.calls != 0:
        raise AssertionError(f"complex UI task should not enter tool loop before plan approval; calls={agent.llm.calls}")
    if "Codex" not in result["content"] or "可以" not in result["content"] or "設定" not in result["content"]:
        raise AssertionError(result)
    graph = agent.task_graphs.active()
    if not graph or graph.status != "awaiting_plan_approval":
        raise AssertionError(graph)
    return result["content"]


def plan_approval_grants_first_computer_control_step():
    calls: list[str] = []

    def fake_hotkey(keys: str):
        calls.append(f"press_hotkey:{keys}")
        return core_tools.ToolResult("ok", "fake hotkey")

    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "planner_plan_approval_graph_test.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    agent = CompanionAgent(PlanApprovalAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "planner_plan_approval_test.json"))
    agent.task_graphs = TaskGraphManager(path)
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.add_tool(core_tools.AgentTool("press_hotkey", "fake hotkey", fake_hotkey, {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]}, True))
    first = agent.chat("請打開 Codex 這個程序，然後按設定，再在二級菜單找到剩餘用量，告訴我 5 小時剩餘百分比")
    if agent.llm.calls != 0 or "Codex" not in first["content"] or "可以" not in first["content"]:
        raise AssertionError(first)
    second = agent.chat("可以")
    if "press_hotkey:alt+tab" not in calls:
        raise AssertionError({"calls": calls, "second": second})
    if _contains_internal_policy_leak(second["content"]):
        raise AssertionError(second)
    return calls


def url_platform_classifies_douyin_and_common_sites():
    checks = {
        "douyin": classify_url_platform("https://www.douyin.com/video/123") == "douyin",
        "not_tiktok": classify_url_platform("https://www.douyin.com/video/123") != "tiktok",
        "youtube": classify_url_platform("https://youtu.be/abc") == "youtube",
        "bilibili": classify_url_platform("https://www.bilibili.com/video/BV123") == "bilibili",
        "instagram": classify_url_platform("https://www.instagram.com/reel/abc/") == "instagram",
        "direct_image": classify_url_platform("https://example.com/a.jpg") == "direct_image",
    }
    if not all(checks.values()):
        raise AssertionError(checks)
    return checks


def url_metadata_parses_open_graph():
    html = """
    <html><head>
    <title>Fallback Title</title>
    <meta property="og:title" content="OG Title">
    <meta name="description" content="A useful description">
    <meta property="og:image" content="https://example.com/cover.jpg">
    </head><body>hello</body></html>
    """
    data = parse_html_metadata(html)
    if data.get("title") != "OG Title" or data.get("description") != "A useful description" or data.get("image") != "https://example.com/cover.jpg":
        raise AssertionError(data)
    return data


def douyin_html_metadata_extracts_description_author_and_cover():
    html_text = '''
    <html><head>
      <meta data-react-helmet="true" name="description" content="400名警察联军抓捕21杀校园枪手！ 究极拉跨，人间地狱，全员崩溃~#美警执法 - 爱看热闹的大鹏于20260403发布在抖音，已经收获了161999个喜欢"/>
    </head><body>
      <img src="https://p26-sign.douyinpic.com/tos-cn-i-dy/cover.webp?x=1&amp;y=2"/>
      <script>window.__DATA__={"videoInfoRes":{"item_list":[{"aweme_id":"7624423517320236340","desc":"400名警察联军抓捕21杀校园枪手！ 究极拉跨，人间地狱，全员崩溃~#美警执法","create_time":1775207150,"author":{"nickname":"爱看热闹的大鹏"},"statistics":{"digg_count":161999}}]}}</script>
    </body></html>
    '''
    base = parse_html_metadata(html_text)
    extra = parse_douyin_metadata(html_text)
    base.update({key: value for key, value in extra.items() if value})
    if "400名警察" not in base.get("title", "") or base.get("author") != "爱看热闹的大鹏" or "douyinpic" not in base.get("image", ""):
        raise AssertionError(base)
    if "video_id=7624423517320236340" not in base.get("extra", ""):
        raise AssertionError(base)
    return {"title": base["title"][:24], "author": base["author"], "image": bool(base["image"])}


def url_context_cache_reuses_metadata():
    import agent_url_context

    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "url_context_cache_test.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    cache = URLContextCache(path)
    calls = {"count": 0}
    original = agent_url_context._http_get

    def fake_get(url, timeout=6, max_bytes=0, mobile=False):
        calls["count"] += 1
        return {
            "status_code": "200",
            "url": url,
            "content_type": "text/html",
            "text": "<title>Cached Page</title><meta name=\"description\" content=\"cached desc\">",
            "error": "",
        }

    try:
        agent_url_context._http_get = fake_get
        first = cache.inspect("https://example.com/page", depth="metadata")
        second = cache.inspect("https://example.com/page", depth="metadata")
    finally:
        agent_url_context._http_get = original
    if calls["count"] != 1 or first.title != "Cached Page" or second.title != "Cached Page":
        raise AssertionError({"calls": calls, "first": first, "second": second})
    return {"key": url_cache_key("https://example.com/page"), "calls": calls["count"]}


def short_context_resolves_reference_to_recent_url():
    import agent_short_context

    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "short_context_test.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    buffer = ShortContextBuffer(path, max_turns=20)
    original = agent_short_context.inspect_urls_for_text
    fake_entry = agent_short_context.URLContextEntry(
        url="https://www.bilibili.com/video/BV1",
        platform="bilibili",
        content_type="video",
        title="很抽象的視頻",
        status="ok",
    )

    try:
        agent_short_context.inspect_urls_for_text = lambda text, depth="metadata": [fake_entry] if "bilibili" in text else []
        buffer.observe_turn("chat1", "https://www.bilibili.com/video/BV1 這個好抽象", depth="metadata")
        rendered = buffer.render_for_turn("chat1", "你覺得呢")
    finally:
        agent_short_context.inspect_urls_for_text = original
    if "可能指代" not in rendered or "很抽象的視頻" not in rendered:
        raise AssertionError(rendered)
    return rendered[:120]


def url_tools_return_structured_results():
    import agent_url_context

    original = agent_url_context._http_get

    def fake_get(url, timeout=6, max_bytes=0, mobile=False):
        return {
            "status_code": "200",
            "url": url,
            "content_type": "text/html",
            "text": "<title>Tool Page</title><meta name=\"description\" content=\"tool desc\">",
            "error": "",
        }

    try:
        agent_url_context._http_get = fake_get
        result = core_tools.real_inspect_url("https://example.com/tool-test", depth="metadata")
    finally:
        agent_url_context._http_get = original
    data = result.data if isinstance(result.data, dict) else {}
    if result.status != "ok" or data.get("title") != "Tool Page" or data.get("platform") != "website":
        raise AssertionError(result.to_text())
    read_back = core_tools.real_read_url_context("https://example.com/tool-test")
    if read_back.status != "ok" or not isinstance(read_back.data, dict):
        raise AssertionError(read_back.to_text())
    return data


def url_preview_uses_real_chinese_markers():
    samples = ["这个视频怎么样", "這個影片你覺得呢", "她怎麼這樣", "好抽象，吐槽一下"]
    failed = [sample for sample in samples if not should_preview_url(sample)]
    if failed:
        raise AssertionError(failed)
    return {"checked": len(samples)}


def read_webpage_social_url_degrades_to_url_context():
    import agent_url_context

    original_http_get = agent_url_context._http_get
    original_ytdlp = agent_url_context._try_ytdlp_metadata
    original_screenshot = agent_url_context._try_playwright_screenshot

    def fake_get(url, timeout=6, max_bytes=0, mobile=False):
        return {
            "status_code": "401",
            "url": url,
            "content_type": "application/json",
            "text": "",
            "error": '{"code":401,"message":"You have been blocked from performing anonymous queries due to bad network reputation (AS9009). Please authenticate."}',
        }

    try:
        agent_url_context._http_get = fake_get
        agent_url_context._try_ytdlp_metadata = lambda entry: setattr(entry, "failure_reason", "extractor_failed")
        agent_url_context._try_playwright_screenshot = lambda entry: None
        result = core_tools.real_read_webpage("https://www.douyin.com/video/123456")
    finally:
        agent_url_context._http_get = original_http_get
        agent_url_context._try_ytdlp_metadata = original_ytdlp
        agent_url_context._try_playwright_screenshot = original_screenshot
    data = result.data if isinstance(result.data, dict) else {}
    if result.status != "ok" or data.get("platform") != "douyin":
        raise AssertionError(result.to_text())
    text = result.to_text()
    if "AuthenticationRequiredError" in text or "&quot;" in text or "AS9009" in text:
        raise AssertionError(text)
    if data.get("human_failure_reason") not in {"网页读取服务被目标或网络信誉限制挡住了", "视频解析器没拿到可用信息", "平台限制或需要登录"}:
        raise AssertionError(data)
    return {"platform": data.get("platform"), "failure": data.get("failure_reason")}


def task_graph_updates_planned_step_with_tool_result():
    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "planner_tool_graph_test.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    manager = TaskGraphManager(path)
    manager.plan_steps("verify runtime", ["run deterministic regression checks"], "self_test", 1)
    result = core_tools.ToolResult("ok", "compiled", data={"returncode": 0})
    verification = SimpleNamespace(status="pass", message="process result accepted", details={"returncode": 0})
    graph = manager.record_tool_result("execute_command", {"command": "python -m py_compile core_tools.py"}, result, verification, "self_test", 2)
    if len(graph.steps) != 1 or graph.steps[0].tool_name != "execute_command" or graph.steps[0].status != "verified":
        raise AssertionError(graph)
    return graph.steps[0].status


def worker_result_assimilation_updates_task_graph_only_from_main_thread():
    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "worker_assim_graph_test.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    manager = TaskGraphManager(path)
    graph = manager.plan_steps("verify with worker", ["run deterministic verification worker"], "self_test", 1)
    result = {
        "job_id": "job_assim_1",
        "kind": "py_compile",
        "status": "done",
        "evidence": ["command: python -m py_compile", "returncode: 0"],
        "metadata": {"task_id": graph.task_id, "step_id": graph.steps[0].step_id},
    }
    assimilated = manager.assimilate_worker_results([result], "self_test", 2)
    if not assimilated or manager.active().steps[0].verification.status != "pass":
        raise AssertionError({"assimilated": assimilated, "graph": manager.active()})
    again = manager.assimilate_worker_results([result], "self_test", 3)
    if again:
        raise AssertionError("worker result was assimilated twice")
    return assimilated


def observe_needed_stays_awaiting_validation():
    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "observe_graph_test.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    manager = TaskGraphManager(path)
    result = core_tools.ToolResult("ok", "clicked")
    verification = SimpleNamespace(status="observe_needed", message="UI action requires observation", details={})
    graph = manager.record_tool_result("click_ui_element", {"target": "button"}, result, verification, "self_test", 1, objective="click UI")
    if graph.steps[0].status != "observe_needed" or graph.status != "awaiting_validation":
        raise AssertionError(graph)
    return graph.status


def workflow_replay_records_blocked_graph():
    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "workflow_replay_graph_test.json")
    replay_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "workflow_replay_test.jsonl")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    try:
        os.remove(replay_path)
    except FileNotFoundError:
        pass
    manager = TaskGraphManager(path)
    graph = manager.start_or_resume("failing workflow", "self_test", 1)
    result = core_tools.ToolResult("error", "failed", error="boom")
    manager.record_tool_result("execute_command", {"command": "bad"}, result, SimpleNamespace(status="fail", message="bad command", details={}), "self_test", 1)
    from agent_task_graph import record_workflow_replay

    case = record_workflow_replay(graph, "test failure", "execute_command", {"command": "bad"}, result, "self_test", 1)
    if case["task_id"] != graph.task_id or case["tool"] != "execute_command":
        raise AssertionError(case)
    return case["name"]


def live_eval_counts_workflow_metrics():
    trace_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "workflow_eval_trace_test.jsonl")
    events = [
        {"event": "workflow.started", "task_id": "wf1"},
        {"event": "workflow.step_recorded", "task_id": "wf1", "tool": "write_file", "status": "verified"},
        {"event": "workflow.completed", "task_id": "wf1", "step_count": 1},
        {"event": "workflow.started", "task_id": "wf2"},
        {"event": "workflow.step_recorded", "task_id": "wf2", "tool": "execute_command", "status": "fail"},
        {"event": "workflow.blocked", "task_id": "wf2", "tool": "execute_command", "reason": "failed"},
        {"event": "WorkflowReplayCreated", "task_id": "wf2", "tool": "execute_command"},
        {"event": "ToolRecoveryResult", "tool": "execute_command", "retry_status": "ok"},
    ]
    with open(trace_path, "w", encoding="utf-8") as file:
        for event in events:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")
    report = build_live_eval_report(trace_path, include_repo=False)
    workflow = report.to_dict()["workflow"]
    if workflow["started_count"] != 2 or workflow["completed_count"] != 1 or workflow["blocked_count"] != 1:
        raise AssertionError(workflow)
    if workflow["verified_waiting_count"] != 0 or workflow["effective_success_count"] != 1:
        raise AssertionError(workflow)
    if workflow["success_rate"] != 0.5 or workflow["recovery_count"] != 1:
        raise AssertionError(workflow)
    if workflow["top_failure_steps"][0]["tool"] != "execute_command":
        raise AssertionError(workflow)
    if report.next_stage_gate["status"] != "pass" or not report.next_stage_gate["warnings"]:
        raise AssertionError(report.next_stage_gate)
    return workflow


def live_eval_counts_verified_waiting_workflow_as_healthy():
    trace_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "workflow_verified_waiting_trace_test.jsonl")
    events = [
        {"event": "workflow.started", "task_id": "wf_verified"},
        {"event": "workflow.step_recorded", "task_id": "wf_verified", "tool": "write_file", "status": "verified"},
    ]
    with open(trace_path, "w", encoding="utf-8") as file:
        for event in events:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")
    report = build_live_eval_report(trace_path, include_repo=False)
    workflow = report.to_dict()["workflow"]
    if workflow["completed_count"] != 0 or workflow["verified_waiting_count"] != 1:
        raise AssertionError(workflow)
    if workflow["effective_success_count"] != 1 or workflow["success_rate"] != 1.0:
        raise AssertionError(workflow)
    if report.next_stage_gate["status"] != "pass":
        raise AssertionError(report.next_stage_gate)
    if "Verified waiting workflows: 1" not in report.to_text():
        raise AssertionError(report.to_text())
    return workflow


def worker_queue_submits_and_runs_success_job():
    jobs_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "worker_jobs_test.jsonl")
    results_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "worker_results_test.jsonl")
    for path in (jobs_path, results_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    queue = WorkerQueue(jobs_path, results_path)
    job = queue.submit_verifier("py_compile", timeout=60)
    result = VerifierWorker(queue).run_job(job)
    if result.status != "done" or result.returncode != 0:
        raise AssertionError(result)
    jobs = queue.list_jobs()
    results = queue.list_results()
    if not any(item.get("status") == "pending" for item in jobs) or not any(item.get("status") == "done" for item in jobs):
        raise AssertionError(jobs)
    if not results or results[-1]["job_id"] != job.job_id:
        raise AssertionError(results)
    return result.evidence[:2]


def worker_records_failed_command_evidence():
    jobs_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "worker_fail_jobs_test.jsonl")
    results_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "worker_fail_results_test.jsonl")
    for path in (jobs_path, results_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    queue = WorkerQueue(jobs_path, results_path, allowed_commands={"fail_test": ["python", "-c", "import sys; print('bad'); sys.exit(7)"]})
    job = queue.submit_verifier("fail_test", timeout=60)
    result = VerifierWorker(queue).run_job(job)
    if result.status != "failed" or result.returncode != 7 or "returncode: 7" not in "\n".join(result.evidence):
        raise AssertionError(result)
    return result.evidence[:3]


def worker_timeout_is_structured():
    jobs_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "worker_timeout_jobs_test.jsonl")
    results_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "worker_timeout_results_test.jsonl")
    for path in (jobs_path, results_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    queue = WorkerQueue(jobs_path, results_path, allowed_commands={"timeout_test": ["python", "-c", "import time; time.sleep(3)"]})
    job = queue.submit_verifier("timeout_test", timeout=1)
    result = VerifierWorker(queue).run_job(job)
    if result.status != "failed" or result.error != "timeout":
        raise AssertionError(result)
    return result.evidence


def worker_rejects_unallowed_verifier_command():
    queue = WorkerQueue(
        os.path.join(core_tools.PROJECT_CACHE_DIR, "worker_reject_jobs_test.jsonl"),
        os.path.join(core_tools.PROJECT_CACHE_DIR, "worker_reject_results_test.jsonl"),
    )
    try:
        queue.submit_verifier("delete_everything", timeout=1)
    except ValueError:
        return "unallowed command rejected"
    raise AssertionError("unallowed command was accepted")


def verifier_subagent_can_submit_background_job():
    jobs_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "worker_subagent_jobs_test.jsonl")
    results_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "worker_subagent_results_test.jsonl")
    for path in (jobs_path, results_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    queue = WorkerQueue(jobs_path, results_path)
    verifier = get_subagent("Verifier")
    job = verifier.submit_verifier_job("trace_summary", timeout=60, queue=queue)
    deadline = time.time() + 10
    result = None
    while time.time() < deadline:
        result = queue.latest_result(job.job_id)
        if result:
            break
        time.sleep(0.1)
    if not result or result.get("status") != "done":
        raise AssertionError({"job": job, "result": result, "jobs": queue.list_jobs()})
    return result["kind"]


def live_eval_counts_worker_metrics():
    trace_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "worker_eval_trace_test.jsonl")
    events = [
        {"event": "worker.result", "job_id": "w1", "kind": "py_compile", "status": "done", "duration_ms": 100},
        {"event": "worker.result", "job_id": "w2", "kind": "self_test", "status": "failed", "duration_ms": 250, "error": "timeout"},
    ]
    with open(trace_path, "w", encoding="utf-8") as file:
        for event in events:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")
    report = build_live_eval_report(trace_path, include_repo=False)
    worker = report.to_dict()["worker"]
    if worker["total_results"] != 2 or worker["done_count"] != 1 or worker["timeout_count"] != 1:
        raise AssertionError(worker)
    if worker["success_rate"] != 0.5 or worker["average_duration_ms"] != 175:
        raise AssertionError(worker)
    if not report.next_stage_gate["warnings"]:
        raise AssertionError(report.next_stage_gate)
    return worker


def live_eval_counts_planner_context_subagent_and_assimilation():
    trace_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "control_plane_eval_trace_test.jsonl")
    events = [
        {"event": "planner.plan_created", "task_id": "wf1", "step_count": 4},
        {"event": "ActionVerification", "tool_name": "click_ui_element", "status": "observe_needed"},
        {"event": "worker.result_assimilated", "task_id": "wf1", "step_id": "step_2", "job_id": "w1", "status": "done"},
        {"event": "context.budget", "mode": "task", "total_after": 6000, "max_chars": 14000},
        {"event": "subagent.run", "subagent": "Reviewer", "status": "ok"},
    ]
    with open(trace_path, "w", encoding="utf-8") as file:
        for event in events:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")
    report = build_live_eval_report(trace_path, include_repo=False)
    data = report.to_dict()
    if data["planner"]["plan_count"] != 1 or data["planner"]["planned_step_count"] != 4 or data["planner"]["observe_needed_count"] != 1:
        raise AssertionError(data["planner"])
    if data["worker"]["assimilated_count"] != 1:
        raise AssertionError(data["worker"])
    if data["context"]["last_total_after"] != 6000 or data["subagents"]["ok_count"] != 1:
        raise AssertionError(data)
    return {"planner": data["planner"], "context": data["context"], "subagents": data["subagents"]}


class _FakePresenceComposer:
    def __init__(self, draft: PresenceMessageCandidate | None = None, quality: PresenceQualityDecision | None = None, raise_error: bool = False):
        self.draft = draft or PresenceMessageCandidate(
            should_send=True,
            message="剛剛那個 CPU 影片我還在想，六顆不同品牌排排站也太像硬體選秀了吧。",
            message_type="followup",
            topic_source="cpu_video",
            confidence=0.9,
            reason="specific_recent_topic",
            model="fake",
        )
        self.quality = quality or PresenceQualityDecision(True, "quality_pass", score=0.9)
        self.raise_error = raise_error
        self.model = "fake-presence-composer"
        self.debug_file = os.path.join(core_tools.PROJECT_CACHE_DIR, "fake_presence_composer_debug.jsonl")
        self.topic_history_file = os.path.join(core_tools.PROJECT_CACHE_DIR, "fake_presence_topic_history.jsonl")
        self.sent: list[dict[str, Any]] = []

    def compose(self, opportunity, short_context, recent_candidates=None):
        if self.raise_error:
            raise RuntimeError("fake composer exploded")
        return self.draft, self.quality

    def record_sent(self, candidate, opportunity, sent_at=None):
        self.sent.append({"candidate": candidate.to_dict(), "opportunity": opportunity.to_dict(), "sent_at": sent_at})


def _presence_test_engine(name: str, config: PresenceConfig | None = None, composer=None) -> PresenceEngine:
    base = os.path.join(core_tools.PROJECT_CACHE_DIR, name)
    os.makedirs(base, exist_ok=True)
    for filename in ("state.json", "candidates.jsonl", "health.json", "debug.jsonl"):
        try:
            os.remove(os.path.join(base, filename))
        except FileNotFoundError:
            pass
    return PresenceEngine(
        state_file=os.path.join(base, "state.json"),
        candidates_file=os.path.join(base, "candidates.jsonl"),
        health_file=os.path.join(base, "health.json"),
        debug_file=os.path.join(base, "debug.jsonl"),
        config=config or PresenceConfig(mode="shadow", min_interval_minutes=120, quiet_hours="00:00-00:00"),
        composer=composer or _FakePresenceComposer(),
    )


def presence_shadow_mode_records_candidate_without_send():
    engine = _presence_test_engine("presence_shadow_test")
    decision = engine.evaluate(
        "self-test-chat",
        short_context={"primary_text": "剛剛那個抖音影片有點好笑", "topic": "douyin video"},
        session_summary="idle",
        now=12 * 60 * 60,
    )
    health = engine.write_health()
    if decision.status != "shadow" or not decision.candidate:
        raise AssertionError(decision)
    if decision.candidate.status != "shadow" or health["shadow_count"] != 1 or health["candidate_count"] != 1:
        raise AssertionError({"decision": decision.to_dict(), "health": health})
    return decision.candidate.to_dict()


def presence_cooldown_prevents_spam():
    engine = _presence_test_engine("presence_cooldown_test")
    first = engine.evaluate("self-test-chat", short_context={"primary_text": "今天有點累"}, session_summary="idle", now=12 * 60 * 60)
    second = engine.evaluate("self-test-chat", short_context={"primary_text": "再看看"}, session_summary="idle", now=12 * 60 * 60 + 60)
    if first.status != "shadow" or second.status != "suppressed" or second.reason != "cooldown":
        raise AssertionError({"first": first.to_dict(), "second": second.to_dict()})
    return second.to_dict()


def presence_suppresses_during_active_task():
    engine = _presence_test_engine("presence_active_task_test")
    decision = engine.evaluate("self-test-chat", short_context={"primary_text": "普通聊天"}, session_summary="active_task: running py_compile", now=12 * 60 * 60)
    if decision.status != "suppressed" or decision.reason != "task_or_permission_active":
        raise AssertionError(decision.to_dict())
    return decision.to_dict()


def live_eval_reports_presence_health():
    previous_state = _read_optional_file(PRESENCE_STATE_FILE)
    previous_candidates = _read_optional_file(PRESENCE_CANDIDATES_FILE)
    previous_health = _read_optional_file(PRESENCE_HEALTH_FILE)
    try:
        for path in (PRESENCE_STATE_FILE, PRESENCE_CANDIDATES_FILE, PRESENCE_HEALTH_FILE, PRESENCE_DEBUG_FILE):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        engine = PresenceEngine(config=PresenceConfig(mode="shadow", min_interval_minutes=120, quiet_hours="00:00-00:00"))
        engine.evaluate("self-test-chat", short_context={"primary_text": "想聊一下剛剛的影片"}, session_summary="idle", now=12 * 60 * 60)
        report = build_live_eval_report(include_repo=False)
        presence = report.to_dict()["presence"]
        if presence["mode"] != "shadow" or presence["candidate_count"] < 1:
            raise AssertionError(presence)
        if "Presence: mode=shadow" not in report.to_text():
            raise AssertionError(report.to_text())
        return presence
    finally:
        _restore_optional_file(PRESENCE_STATE_FILE, previous_state)
        _restore_optional_file(PRESENCE_CANDIDATES_FILE, previous_candidates)
        _restore_optional_file(PRESENCE_HEALTH_FILE, previous_health)


def _presence_fixed_time(hour: int, minute: int = 0) -> float:
    return time.mktime((2026, 1, 1, hour, minute, 0, 0, 1, -1))


def presence_quiet_hours_are_soft_when_owner_recently_active():
    engine = _presence_test_engine("presence_soft_quiet_test", PresenceConfig(mode="shadow", min_interval_minutes=120, quiet_hours="23:30-08:00"))
    now = _presence_fixed_time(1, 30)
    quiet = engine.evaluate("self-test-chat", short_context={"primary_text": "深夜測試", "created_at": now - 4 * 60}, session_summary="idle", now=now)
    asleep = engine.evaluate("other-chat", short_context={"primary_text": "深夜測試", "created_at": now - 4 * 60 * 60}, session_summary="idle", now=now, force=False)
    if quiet.status != "shadow" or not quiet.debug.get("owner_likely_awake"):
        raise AssertionError(quiet.to_dict())
    if asleep.status != "suppressed" or asleep.reason != "quiet_hours_no_recent_interaction":
        raise AssertionError(asleep.to_dict())
    return {"awake": quiet.reason, "asleep": asleep.reason}


def presence_stale_permission_does_not_block_forever():
    engine = _presence_test_engine("presence_stale_task_test", PresenceConfig(mode="shadow", min_interval_minutes=120, quiet_hours="00:00-00:00", stale_task_minutes=60))
    now = _presence_fixed_time(12)
    stale = engine.evaluate(
        "self-test-chat",
        short_context={"primary_text": "普通聊天", "created_at": now - 10},
        session_summary="state: awaiting_permission\nlast_tool: send_telegram_media blocked",
        session_updated_at=now - 3 * 60 * 60,
        now=now,
    )
    fresh = engine.evaluate(
        "fresh-chat",
        short_context={"primary_text": "普通聊天", "created_at": now - 10},
        session_summary="state: awaiting_permission\nlast_tool: send_telegram_media blocked",
        session_updated_at=now - 10 * 60,
        now=now,
    )
    if stale.status != "shadow" or not stale.debug.get("task_debug", {}).get("stale"):
        raise AssertionError(stale.to_dict())
    if fresh.status != "suppressed" or fresh.reason != "task_or_permission_active":
        raise AssertionError(fresh.to_dict())
    return {"stale": stale.reason, "fresh": fresh.reason}


def presence_notify_tick_sends_once_and_respects_daily_limit():
    engine = _presence_test_engine("presence_notify_test", PresenceConfig(mode="notify", daily_limit=1, min_interval_minutes=1, quiet_hours="00:00-00:00"))
    now = _presence_fixed_time(12)
    sent: list[str] = []
    first = engine.tick(
        "self-test-chat",
        short_context={"primary_text": "今天有點累", "created_at": now - 60},
        session_summary="idle",
        now=now,
        random_value=0.0,
        send_callback=sent.append,
    )
    second = engine.tick(
        "self-test-chat",
        short_context={"primary_text": "今天有點累", "created_at": now - 60},
        session_summary="idle",
        now=now + 2 * 60,
        random_value=0.0,
        send_callback=sent.append,
    )
    if first.candidate is None or first.candidate.status != "sent" or len(sent) != 1:
        raise AssertionError({"first": first.to_dict(), "sent": sent})
    if second.status != "suppressed" or second.reason != "daily_limit" or len(sent) != 1:
        raise AssertionError({"second": second.to_dict(), "sent": sent})
    return {"sent": sent[0], "second": second.reason}


def presence_shadow_tick_never_sends():
    engine = _presence_test_engine("presence_shadow_tick_test", PresenceConfig(mode="shadow", min_interval_minutes=1, quiet_hours="00:00-00:00"))
    sent: list[str] = []
    decision = engine.tick(
        "self-test-chat",
        short_context={"primary_text": "剛剛那個抖音影片", "created_at": _presence_fixed_time(12) - 60},
        session_summary="idle",
        now=_presence_fixed_time(12),
        random_value=0.0,
        send_callback=sent.append,
    )
    if decision.status != "shadow" or sent:
        raise AssertionError({"decision": decision.to_dict(), "sent": sent})
    return decision.to_dict()


def presence_composer_quality_message_sends():
    composer = _FakePresenceComposer()
    engine = _presence_test_engine("presence_composer_send_test", PresenceConfig(mode="notify", daily_limit=3, min_interval_minutes=1, quiet_hours="00:00-00:00"), composer=composer)
    now = _presence_fixed_time(12)
    sent: list[str] = []
    decision = engine.tick(
        "self-test-chat",
        short_context={"primary_text": "剛剛那個 CPU 核心數影片", "created_at": now - 60},
        session_summary="idle",
        now=now,
        random_value=0.0,
        send_callback=sent.append,
    )
    if decision.status != "ready" or not decision.candidate or decision.candidate.status != "sent" or len(sent) != 1:
        raise AssertionError({"decision": decision.to_dict(), "sent": sent})
    if not composer.sent:
        raise AssertionError("composer did not record sent topic")
    return {"sent": sent[0], "quality": decision.debug.get("quality")}


def presence_composer_rejects_generic_checkin():
    composer = _FakePresenceComposer(
        draft=PresenceMessageCandidate(
            should_send=True,
            message="主人今天怎麼樣呀？",
            message_type="care",
            topic_source="generic",
            confidence=0.8,
            reason="generic_checkin",
            model="fake",
        ),
        quality=PresenceQualityDecision(False, "generic_checkin_without_context", score=0.0),
    )
    engine = _presence_test_engine("presence_composer_reject_test", PresenceConfig(mode="notify", daily_limit=3, min_interval_minutes=1, quiet_hours="00:00-00:00"), composer=composer)
    sent: list[str] = []
    decision = engine.tick(
        "self-test-chat",
        short_context={"primary_text": "普通空上下文", "created_at": _presence_fixed_time(12) - 60},
        session_summary="idle",
        now=_presence_fixed_time(12),
        random_value=0.0,
        send_callback=sent.append,
    )
    if not decision.candidate or decision.candidate.status != "composer_rejected" or sent:
        raise AssertionError({"decision": decision.to_dict(), "sent": sent})
    return decision.debug.get("quality")


def presence_composer_error_does_not_crash_or_send():
    composer = _FakePresenceComposer(raise_error=True)
    engine = _presence_test_engine("presence_composer_error_test", PresenceConfig(mode="notify", daily_limit=3, min_interval_minutes=1, quiet_hours="00:00-00:00"), composer=composer)
    sent: list[str] = []
    decision = engine.tick(
        "self-test-chat",
        short_context={"primary_text": "剛剛那個話題", "created_at": _presence_fixed_time(12) - 60},
        session_summary="idle",
        now=_presence_fixed_time(12),
        random_value=0.0,
        send_callback=sent.append,
    )
    if not decision.candidate or decision.candidate.status != "composer_rejected" or sent:
        raise AssertionError({"decision": decision.to_dict(), "sent": sent})
    if decision.reason != "composer_error":
        raise AssertionError(decision.to_dict())
    return decision.to_dict()


def presence_default_quota_is_adaptive_not_tiny_daily_cap():
    config = PresenceConfig()
    if config.daily_limit < 6 or config.min_interval_minutes > 90 or config.icebreak_after_minutes != 360:
        raise AssertionError(config.to_dict())
    return config.to_dict()


def presence_long_silence_becomes_icebreak_opportunity():
    engine = _presence_test_engine("presence_icebreak_test", PresenceConfig(mode="shadow", min_interval_minutes=1, quiet_hours="00:00-00:00", icebreak_after_minutes=360))
    now = _presence_fixed_time(18)
    decision = engine.evaluate(
        "self-test-chat",
        short_context={"primary_text": "", "created_at": now - 7 * 60 * 60},
        session_summary="idle",
        now=now,
    )
    if decision.status != "shadow" or decision.reason != "long_silence_icebreak" or not decision.candidate or decision.candidate.kind != "icebreak":
        raise AssertionError(decision.to_dict())
    return decision.to_dict()


def presence_debug_records_suppression_reason():
    engine = _presence_test_engine("presence_debug_test", PresenceConfig(mode="shadow", quiet_hours="23:30-08:00"))
    decision = engine.evaluate("self-test-chat", short_context={"primary_text": "深夜測試"}, session_summary="idle", now=_presence_fixed_time(2))
    debug = engine.recent_debug(limit=1)
    if decision.reason != "quiet_hours_no_recent_interaction" or not debug or debug[-1].get("reason") != decision.reason:
        raise AssertionError({"decision": decision.to_dict(), "debug": debug})
    return debug[-1]


def failure_replay_persists_minimal_case():
    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "failure_replay_test.jsonl")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    case = record_failure_replay("fake_tool", {"value": "x" * 700}, core_tools.ToolResult("error", "boom", error="bad"), "self_test", 9, 3, path)
    with open(path, "r", encoding="utf-8") as file:
        saved = json.loads(file.readline())
    if saved["tool_name"] != "fake_tool" or len(saved["arguments"]["value"]) > 530:
        raise AssertionError(saved)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    return case["name"]


def subagent_lite_returns_isolated_summary():
    verifier = get_subagent("Verifier")
    result = verifier.run("run tests", evidence=["self_test.py"])
    if result.name != "Verifier" or "self_test.py" not in result.evidence:
        raise AssertionError(result)
    if "Explorer" not in BUILTIN_SUBAGENTS or "Reviewer" not in BUILTIN_SUBAGENTS:
        raise AssertionError(BUILTIN_SUBAGENTS)
    return result.summary


def verifier_subagent_runs_safe_command():
    verifier = get_subagent("Verifier")
    result = verifier.verify_command(ALLOWED_VERIFIER_COMMANDS["trace_summary"], cwd=os.path.dirname(__file__), timeout=30)
    if result.status != "ok" or not any("returncode: 0" in item for item in result.evidence):
        raise AssertionError(result)
    return result.summary


def subagent_boundaries_reject_disallowed_tools_and_commands():
    explorer = get_subagent("Explorer")
    try:
        explorer.assert_tool_allowed("execute_command")
    except PermissionError:
        pass
    else:
        raise AssertionError("Explorer should not be able to execute commands")
    verifier = get_subagent("Verifier")
    result = verifier.verify_command(["python", "-c", "print('not allowlisted')"], cwd=os.path.dirname(__file__), timeout=10)
    if result.status != "error" or "allowlist" not in result.summary:
        raise AssertionError(result)
    return "subagent boundaries held"


def session_brain_verification_pass_clears_pending():
    reset_session_brain_file()
    brain = SessionBrain()
    brain.mark_validation_needed("run tests", turn_id=1, session_id="test")
    brain.mark_verification_result("ok", ["SUMMARY 1 passed"], turn_id=2, session_id="test")
    if brain.state.state != "idle" or brain.state.pending_validation:
        raise AssertionError(brain.state)
    return brain.summary()


def session_brain_verification_failure_keeps_validation():
    reset_session_brain_file()
    brain = SessionBrain()
    brain.mark_validation_needed("run tests", turn_id=1, session_id="test")
    brain.mark_verification_result("error", ["failed"], turn_id=2, session_id="test")
    if brain.state.state != "awaiting_validation" or not brain.state.pending_validation:
        raise AssertionError(brain.state)
    return brain.summary()


def verification_planner_recommends_runtime_gates():
    plan = DEFAULT_VERIFICATION_PLANNER.plan("runtime session change", changed_files=["core_agent.py", "agent_session.py"])
    names = [command.name for command in plan.commands]
    if "py_compile" not in names or "self_test" not in names:
        raise AssertionError(plan.summary())
    return plan.summary()


def verification_planner_handles_docs_only():
    plan = DEFAULT_VERIFICATION_PLANNER.plan("docs", changed_files=["RUNBOOK.md"])
    if not plan.commands or plan.commands[0].required:
        raise AssertionError(plan.summary())
    if not plan.notes:
        raise AssertionError(plan.summary())
    return plan.summary()


def session_brain_validation_includes_plan_and_clears_it():
    reset_session_brain_file()
    brain = SessionBrain()
    brain.mark_validation_needed("runtime session change", changed_files=["agent_session.py"], turn_id=1, session_id="test")
    if not brain.state.verification_plan or "py_compile" not in brain.summary():
        raise AssertionError(brain.summary())
    brain.mark_verification_result("ok", ["SUMMARY 1 passed"], turn_id=2, session_id="test")
    if brain.state.verification_plan or brain.state.pending_validation:
        raise AssertionError(brain.state)
    return brain.summary()


class VisionCallAdapter:
    def __init__(self):
        self.calls = 0

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_vision",
                        "name": "analyze_media",
                        "arguments": {"file_path": "project_cache/does_not_exist.png"},
                        "raw_arguments": '{"file_path":"project_cache/does_not_exist.png"}',
                    }
                ],
            }
        return {"role": "assistant", "content": "vision policy handled"}


class CommandCallAdapter:
    def __init__(self, command: str):
        self.command = command
        self.calls = 0

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_command",
                        "name": "execute_command",
                        "arguments": {"command": self.command},
                        "raw_arguments": json.dumps({"command": self.command}, ensure_ascii=False),
                    }
                ],
            }
        return {"role": "assistant", "content": "驗證跑完啦"}


def latency_policy_classifies_modes():
    if classify_interaction("hi", False) != InteractionMode.CHAT:
        raise AssertionError("plain chat should be chat")
    if classify_interaction("可以幫我截取電腦螢幕的畫面嗎", False) != InteractionMode.SCREEN_OBSERVE:
        raise AssertionError("screen requests should be screen_observe")
    if classify_interaction("可以幫我截圖看看現在什麼狀態嗎", False) != InteractionMode.SCREEN_OBSERVE:
        raise AssertionError("traditional Chinese screenshot request should be screen_observe")
    if classify_interaction("幫我看一下屏幕", False) != InteractionMode.SCREEN_OBSERVE:
        raise AssertionError("simplified Chinese screen request should be screen_observe")
    if classify_interaction("", True, "sticker") != InteractionMode.SOCIAL_STICKER:
        raise AssertionError("plain sticker should be social_sticker")
    if classify_interaction("幫我看圖", True, "photo") != InteractionMode.VISION_TASK:
        raise AssertionError("explicit image request should be vision_task")
    if classify_interaction("run test", False) != InteractionMode.TOOL_TASK:
        raise AssertionError("tool intent should be tool_task")
    return "modes classified"


def computer_action_text_routes_to_tool_task():
    examples = [
        "幫我暫停播放",
        "按一下空格鍵",
        "看一下螢幕然後幫我暫停",
        "幫我點擊播放器",
    ]
    for text in examples:
        mode = classify_interaction(text, False)
        if mode != InteractionMode.TOOL_TASK:
            raise AssertionError({"text": text, "mode": mode})
    return "computer action text routes to tool_task"


def observe_only_media_status_does_not_require_plan_approval():
    text = "你幫我看一下現在我的電腦有沒有在播放媒體或者音樂"
    if classify_interaction(text, False) != InteractionMode.SCREEN_OBSERVE:
        raise AssertionError(classify_interaction(text, False))
    brain_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "observe_only_session_brain_test.json")
    try:
        os.remove(brain_path)
    except FileNotFoundError:
        pass
    classification = SessionBrain(brain_path).classify_turn(text, turn_id=1, session_id="self_test")
    if classification.intent != "screen_observe" or classification.is_chat:
        raise AssertionError(classification)
    plan = DEFAULT_PLANNER.plan(text, intent=classification.intent, session_id="self_test", turn_id=1)
    if len(plan.steps) != 2 or "observe current screen once" not in plan.step_names():
        raise AssertionError(plan.step_names())
    agent = CompanionAgent(PlanFirstAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "observe_only_plan_test.json"))
    agent.task_graphs = TaskGraphManager(os.path.join(core_tools.PROJECT_CACHE_DIR, "observe_only_plan_graph_test.json"))
    agent.session_brain = SessionBrain(os.path.join(core_tools.PROJECT_CACHE_DIR, "observe_only_plan_brain_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    result = agent.chat(text)
    if "你回" in result["content"] or "可以" in result["content"]:
        raise AssertionError(result["content"])
    if agent.llm.calls != 1:
        raise AssertionError(agent.llm.calls)
    return "observe-only media status ran without plan approval"


def observe_only_media_status_overrides_stale_validation_state():
    text = "你幫我看一下現在我的電腦有沒有在播放媒體或者音樂"
    brain_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "observe_stale_validation_brain_test.json")
    try:
        os.remove(brain_path)
    except FileNotFoundError:
        pass
    brain = SessionBrain(brain_path)
    brain.state.state = "awaiting_validation"
    brain.state.pending_validation = ["verify previous unrelated task"]
    brain.state.last_tool = "execute_python"
    brain.state.last_tool_status = "ok"
    brain.save()
    classification = brain.classify_turn(text, turn_id=2, session_id="self_test")
    if classification.intent != "screen_observe" or classification.reason != "screen_observe_intent":
        raise AssertionError(classification)
    if brain.state.current_objective != text:
        raise AssertionError(brain.state.current_objective)
    return "fresh observe intent overrides stale validation"


def observe_only_media_status_starts_fresh_graph_over_stale_active_graph():
    text = "你幫我看一下現在我的電腦有沒有在播放媒體或者音樂"
    graph_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "observe_stale_graph_override_test.json")
    for path in [graph_path, os.path.join(core_tools.PROJECT_CACHE_DIR, "observe_stale_graph_brain_test.json")]:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    agent = CompanionAgent(PlanFirstAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "observe_stale_graph_test.json"))
    agent.task_graphs = TaskGraphManager(graph_path)
    agent.session_brain = SessionBrain(os.path.join(core_tools.PROJECT_CACHE_DIR, "observe_stale_graph_brain_test.json"))
    agent.task_graphs.plan_steps("old unrelated workflow", ["old pending validation step"], "self_test", 1)
    stale = agent.task_graphs.active()
    stale.status = "awaiting_validation"
    agent.task_graphs.save()
    agent.session_brain.state.state = "awaiting_validation"
    agent.session_brain.state.pending_validation = ["verify old unrelated workflow"]
    agent.session_brain.save()
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    result = agent.chat(text)
    active = agent.task_graphs.active()
    if not active or active.objective != text:
        raise AssertionError({"reply": result, "active": active})
    if len(active.steps) != 2 or "observe current screen once" not in [step.name for step in active.steps]:
        raise AssertionError(active)
    if "你回" in result["content"] or "可以" in result["content"]:
        raise AssertionError(result["content"])
    return "fresh observe intent starts a fresh graph over stale active graph"


def chat_policy_blocks_vision_tool():
    agent = CompanionAgent(VisionCallAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "latency_policy_test.json"))
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    result = agent.chat("plain chat", response_policy=response_policy_for(InteractionMode.CHAT))
    tool_messages = [m for m in agent.memory if m.get("role") == "tool"]
    if not any("看圖任務" in m.get("content", "") or "停在" in m.get("content", "") for m in tool_messages):
        raise AssertionError(agent.memory)
    if _contains_internal_policy_leak(result["content"]):
        raise AssertionError(result)
    return result["content"]


def _contains_internal_policy_leak(text: str) -> bool:
    lowered = (text or "").casefold()
    leaks = [
        "route policy",
        "chat route",
        "screen_observe",
        "tool_task",
        "social_sticker",
        "skipped by",
        "response policy",
        "loop controller",
        "tool_not_allowed_for_route",
    ]
    return any(item in lowered for item in leaks)


def user_visible_tool_blocks_hide_internal_route_terms():
    agent = CompanionAgent(PlainReplyAdapter("ok"), "system self test", os.path.join(core_tools.HISTORY_DIR, "route_voice_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    result = agent.executor.execute("execute_python", {"code": "print('x')"}, None, response_policy_for(InteractionMode.CHAT))
    if result.status != "blocked":
        raise AssertionError(result.to_text())
    if _contains_internal_policy_leak(result.message):
        raise AssertionError(result.to_text())
    if "繼續" not in result.message:
        raise AssertionError(result.to_text())
    return result.message


def user_voice_strings_are_unicode_safe():
    import agent_user_voice

    root = os.path.dirname(__file__)
    runtime_source = open(os.path.join(root, "agent_tool_runtime.py"), "r", encoding="utf-8").read()
    loop_source = open(os.path.join(root, "agent_tool_loop.py"), "r", encoding="utf-8").read()
    samples = [
        agent_user_voice.friendly_tool_block("execute_python"),
        agent_user_voice.friendly_tool_block("read_file", core_tools.ToolResult("blocked", "Need a cleaner path.", data={"retry_hint": "請換一個檔案路徑。"})),
        agent_user_voice.repeated_tool_stop_reply("execute_python", "case_1"),
        agent_user_voice.failsafe_reply(" [系統截圖: screen.png]"),
        agent_user_voice.failure_replay_reply("execute_command", "case_2", "trace.jsonl"),
        agent_user_voice.tool_loop_timeout_reply(),
        agent_user_voice.empty_reply_fallback(),
        agent_user_voice.permission_request_reply("execute_command"),
        agent_user_voice.approved_tool_success_reply("execute_python", "Python completed.", "stdout: ok", True),
        agent_user_voice.approved_tool_blocked_reply("execute_python", core_tools.ToolResult("blocked", "needs permission", requires_permission=True)),
        agent_user_voice.approved_tool_error_reply("execute_python", core_tools.ToolResult("error", "Python failed.", error="RuntimeError: boom")),
    ]
    bad_markers = ["鍓", "鐪", "绲", "鎴", "锛", "灞", "�"]
    for sample in samples:
        if any(marker in sample for marker in bad_markers):
            raise AssertionError(sample)
    combined = "\n".join(samples)
    for expected in ["可以", "繼續", "系統截圖", "主人"]:
        if expected not in combined:
            raise AssertionError(combined)
    if any(_contains_internal_policy_leak(sample) for sample in samples):
        raise AssertionError(combined)
    if "你可以說「繼續」接回原任務" not in runtime_source:
        raise AssertionError("runtime retry hint is not Unicode-safe")
    if "[系統截圖: {screen}]" not in loop_source:
        raise AssertionError("failsafe screenshot marker is not Unicode-safe")
    return "user-visible runtime voice is unicode-safe"


def user_facing_source_files_are_unicode_safe():
    root = os.path.dirname(__file__)
    files = [
        "agent_user_voice.py",
        "agent_permission_replay.py",
        "agent_outcome.py",
        "agent_latency.py",
        "agent_runtime_context.py",
        "agent_tool_runtime.py",
        "agent_tool_loop.py",
        "core_agent.py",
        "agent_llm.py",
        "main.py",
    ]
    bad_markers = ["鍓", "鐪", "绲", "鎴", "锛", "灞", "闆", "铻", "妯", "楹", "�"]
    required_pairs = {
        "agent_user_voice.py": ["我先等你點頭", "可以", "繼續"],
        "agent_permission_replay.py": ["approved_tool_success_reply"],
        "agent_outcome.py": ["有結果", "發給我", "分析一下", "繼續"],
        "agent_latency.py": ["我先看一下", "我先處理一下", "收到"],
        "agent_runtime_context.py": ["目前任務筆記", "目前步驟", "驗證結果"],
        "agent_tool_runtime.py": ["你可以說「繼續」接回原任務"],
        "agent_tool_loop.py": ["系統截圖"],
        "core_agent.py": ["任務提醒"],
        "agent_llm.py": ["目前任務筆記"],
    }
    for filename in files:
        path = os.path.join(root, filename)
        with open(path, "rb") as file:
            raw = file.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AssertionError(f"{filename} is not valid UTF-8: {exc}") from exc
        variants = [text]
        decoded = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
        decoded = re.sub(r"\\U([0-9a-fA-F]{8})", lambda m: chr(int(m.group(1), 16)), decoded)
        if decoded != text:
            variants.append(decoded)
        if any(marker in variant for marker in bad_markers for variant in variants):
            raise AssertionError(f"{filename} contains mojibake markers")
        if "????" in text:
            raise AssertionError(f"{filename} contains question-mark mojibake")
        for expected in required_pairs.get(filename, []):
            if not any(expected in variant for variant in variants):
                raise AssertionError(f"{filename} is missing expected phrase: {expected}")
    return "user-facing source files are UTF-8 and voice-safe"


def runtime_context_uses_owner_facing_labels():
    task = build_runtime_context(
        "繼續",
        turn_intent="task_continuation",
        session_summary="state: awaiting_validation",
        task_summary="step: verify screenshot",
        worker_results=[{"step_id": "step_1", "status": "done", "job_id": "job_1"}],
        include_task_context=True,
    )
    required = ["[目前任務筆記]", "[目前步驟]", "[驗證結果]", "intent: task_continuation"]
    missing = [item for item in required if item not in task]
    if missing:
        raise AssertionError((missing, task))
    forbidden = ["[SessionBrain]", "[TaskGraph]", "[WorkerEvidence]", "turn_intent:", "worker="]
    leaked = [item for item in forbidden if item in task]
    if leaked:
        raise AssertionError((leaked, task))
    if infer_route_from_messages([{"role": "user", "content": task}]) != "task_continuation":
        raise AssertionError(task)
    return "runtime context uses owner-facing labels"


def semantic_intent_upgrades_chat_policy_without_user_modes():
    chat_policy = response_policy_for(InteractionMode.CHAT)
    upgraded = policy_for_semantic_intent("task_continuation", chat_policy)
    if upgraded.route != "task_continuation" or upgraded.max_tool_iterations <= chat_policy.max_tool_iterations:
        raise AssertionError(upgraded)
    if upgraded.allowed_tools is not None:
        raise AssertionError("task continuation should not inherit chat allowed_tools")
    screen = policy_for_semantic_intent("screen_observe", chat_policy)
    if "get_screen_ui" not in (screen.allowed_tools or []):
        raise AssertionError(screen)
    return "semantic intent maps to internal policy"


def safe_verifier_command_runs_in_screen_observe_route():
    agent = CompanionAgent(CommandCallAdapter("python -m py_compile core_tools.py"), "system self test", os.path.join(core_tools.HISTORY_DIR, "safe_verifier_route_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    result = agent.chat("幫我看一下狀態，順便跑安全驗證", response_policy=response_policy_for(InteractionMode.SCREEN_OBSERVE))
    if agent.permission_manager.pending:
        raise AssertionError("safe verifier command should not require pending permission")
    if not any(m.get("role") == "tool" and "Command completed" in m.get("content", "") for m in agent.memory):
        raise AssertionError(agent.memory)
    if "驗證跑完" not in result["content"]:
        raise AssertionError(result)
    return result["content"]


def arbitrary_command_still_requires_permission():
    agent = CompanionAgent(CommandCallAdapter("echo unsafe command"), "system self test", os.path.join(core_tools.HISTORY_DIR, "unsafe_command_permission_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    agent.chat("run arbitrary command", response_policy=response_policy_for(InteractionMode.TOOL_TASK))
    if not agent.permission_manager.pending or agent.permission_manager.pending.tool_name != "execute_command":
        raise AssertionError("arbitrary command should still require permission")
    return agent.permission_manager.pending.tool_name


def workspace_media_send_is_low_friction_but_external_media_is_guarded():
    agent = CompanionAgent(PlainReplyAdapter("ok"), "system self test", os.path.join(core_tools.HISTORY_DIR, "media_permission_test.json"))
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    local_media = os.path.join(core_tools.PROJECT_CACHE_DIR, "permission_media.png")
    with open(local_media, "wb") as file:
        file.write(b"not a real png; permission check only")
    send_tool = agent.registry.get("send_telegram_media")
    if agent.executor._requires_confirm(send_tool, {"file_path": local_media}):
        raise AssertionError("workspace generated media should not require permission")
    external = os.path.join(os.path.expanduser("~"), "outside_media.png")
    if not agent.executor._requires_confirm(send_tool, {"file_path": external}):
        raise AssertionError("external media path should require permission")
    return "workspace media free, external guarded"


def memory_update_quality_gate_allows_clean_and_rejects_broken():
    ok = core_tools.real_update_memory("主人喜歡月月聊天時更萌一點，但不要浮誇。")
    if ok.status != "ok":
        raise AssertionError(ok.to_text())
    broken = core_tools.real_update_memory("锛锛锛涓讳汉鍙堟槸涓€鍫嗕贡鐮佽 記住")
    if broken.status != "error" or "mojibake" not in broken.message.casefold():
        raise AssertionError(broken.to_text())
    return "clean memory accepted; broken memory rejected"


def media_cache_hits_second_analysis():
    # A tiny valid PNG.
    import base64

    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "tiny_cache_test.png")
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    with open(path, "wb") as file:
        file.write(png)
    DEFAULT_MEDIA_CACHE.set_vision_summary(path, "A tiny cached test image with a single pixel.")
    result = core_tools.real_analyze_media(path)
    if result.status != "ok" or "cache hit" not in result.message.casefold():
        raise AssertionError(result.to_text())
    return result.to_text()


def dynamic_media_skips_image_vision():
    path = os.path.join(core_tools.PROJECT_CACHE_DIR, "fake_sticker.webm")
    with open(path, "wb") as file:
        file.write(b"not really webm but extension is enough for routing")
    result = core_tools.real_analyze_media(path)
    if result.status != "ok" or "Dynamic media" not in result.message:
        raise AssertionError(result.to_text())
    return result.to_text()


def quick_ack_exists_for_slow_modes():
    if not quick_ack_for(InteractionMode.VISION_TASK):
        raise AssertionError("missing vision quick ack")
    if not quick_ack_for(InteractionMode.SCREEN_OBSERVE):
        raise AssertionError("missing screen quick ack")
    if quick_ack_for(InteractionMode.CHAT):
        raise AssertionError("chat should not send quick ack")
    return quick_ack_for(InteractionMode.VISION_TASK)


def dsml_cleaner_handles_spaced_tags():
    dirty = "正常文字 < | DSML | > hidden </ | DSML | > 結尾"
    cleaned = clean_assistant_output(dirty)
    if "DSML" in cleaned or "hidden" in cleaned:
        raise AssertionError(cleaned)
    return cleaned


class FailSafeAdapter:
    def chat_with_tools(self, messages, tools):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_failsafe", "name": "fake_failsafe", "arguments": {}, "raw_arguments": "{}"}],
        }


class RepeatToolAdapter:
    def chat_with_tools(self, messages, tools):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"call_repeat_{len(messages)}", "name": "fake_repeat", "arguments": {"value": 1}, "raw_arguments": '{"value":1}'}],
        }


class InterleavedUiNavigationAdapter:
    def __init__(self):
        self.calls = 0

    def chat_with_tools(self, messages, tools):
        self.calls += 1
        if self.calls in {1, 3, 5}:
            args = {"keys": "alt+tab"}
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"call_hotkey_{self.calls}", "name": "press_hotkey", "arguments": args, "raw_arguments": json.dumps(args)}],
            }
        if self.calls in {2, 4, 6}:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"call_screen_{self.calls}", "name": "get_screen_ui", "arguments": {}, "raw_arguments": "{}"}],
            }
        return {"role": "assistant", "content": "找到目標窗口了"}


def repeated_tool_call_stops_before_timeout():
    agent = CompanionAgent(RepeatToolAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "repeat_tool_test.json"))
    agent.add_tool(core_tools.AgentTool("fake_repeat", "fake repeat tool", lambda value=1: core_tools.ToolResult("ok", "repeat ok"), {"type": "object", "properties": {"value": {"type": "integer"}}}))
    result = agent.chat("repeat tool")
    if "繞圈" not in result["content"] and "重複" not in result["content"]:
        raise AssertionError(result)
    if _contains_internal_policy_leak(result["content"]):
        raise AssertionError(result)
    return result["content"]


def interleaved_ui_navigation_is_not_treated_as_repeated_loop():
    calls: list[str] = []

    def fake_hotkey(keys: str):
        calls.append(f"press_hotkey:{keys}")
        return core_tools.ToolResult("ok", "fake hotkey")

    def fake_screen():
        calls.append("get_screen_ui")
        return core_tools.ToolResult("ok", "fake screen", data={"active_window": "not yet"})

    agent = CompanionAgent(InterleavedUiNavigationAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "interleaved_ui_loop_test.json"))
    agent.always_allow_tools = True
    agent.add_tool(core_tools.AgentTool("press_hotkey", "fake hotkey", fake_hotkey, {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]}, True))
    agent.add_tool(core_tools.AgentTool("get_screen_ui", "fake screen", fake_screen, {"type": "object", "properties": {}}, True))
    result = agent._tool_loop_controller(response_policy_for(InteractionMode.TOOL_TASK)).run(agent.memory, "interleaved ui navigation", None)
    if result.content != "找到目標窗口了":
        raise AssertionError(result)
    if calls.count("press_hotkey:alt+tab") != 3 or calls.count("get_screen_ui") != 3:
        raise AssertionError(calls)
    return calls


def screen_observe_policy_blocks_unrelated_vision_tool():
    agent = CompanionAgent(VisionCallAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "screen_policy_test.json"))
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    result = agent.chat("幫我截圖看看狀態", response_policy=response_policy_for(InteractionMode.SCREEN_OBSERVE))
    if "看圖任務" not in result["content"] or _contains_internal_policy_leak(result["content"]):
        raise AssertionError(result)
    return result["content"]


def prompt_mode_routes_screen_observe_persona():
    if _prompt_mode_for_seed("幫我截取電腦螢幕畫面") != "screen_observe":
        raise AssertionError("screen prompt did not route to screen_observe")
    context = build_system_prompt("幫我截取電腦螢幕畫面")
    if "Persona mode: screen_observe" not in context:
        raise AssertionError(context[:800])
    return "screen_observe prompt routed"


def fail_safe_returns_without_retry():
    agent = CompanionAgent(FailSafeAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "failsafe_test.json"))
    agent.add_tool(core_tools.AgentTool("fake_failsafe", "fake fail-safe tool", lambda: core_tools.ToolResult("error", "pyautogui FailSafeException: mouse moved"), {"type": "object", "properties": {}}))
    result = agent.chat("trigger failsafe")
    if "卡在工具迴圈" not in result["content"] and "Fail-safe" not in json.dumps(agent.memory, ensure_ascii=False):
        raise AssertionError(result)
    return "fail-safe was surfaced"


def gateway_sticker_fuzzy_match():
    path = find_sticker_file("?.jpg")
    if not path or not os.path.exists(path):
        raise AssertionError(path)
    return path


class FakeBot:
    def __init__(self):
        self.sent = []

    def send_chat_action(self, *args, **kwargs):
        self.sent.append(("action", args, kwargs))

    def send_message(self, *args, **kwargs):
        self.sent.append(("message", args, kwargs))

    def send_animation(self, *args, **kwargs):
        self.sent.append(("animation", args, kwargs))

    def send_sticker(self, *args, **kwargs):
        self.sent.append(("sticker", args, kwargs))

    def send_photo(self, *args, **kwargs):
        self.sent.append(("photo", args, kwargs))

    def reply_to(self, *args, **kwargs):
        self.sent.append(("reply", args, kwargs))


class FlakyTelegramBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.failures_left = 1

    def send_message(self, *args, **kwargs):
        if self.failures_left:
            self.failures_left -= 1
            raise ConnectionResetError(10054, "遠端主機已強制關閉一個現存的連線。")
        super().send_message(*args, **kwargs)


def fake_message(chat_id=123, message_id=1, text="", caption=""):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        message_id=message_id,
        text=text,
        caption=caption,
        reply_to_message=None,
    )


class FakeGatewayAgent:
    def __init__(self, content: str):
        self.content = content
        self.interactive_mode = False

    def chat(self, prompt, tool_callback=None, response_policy=None):
        return {"content": self.content}


def telegram_gateway_retries_transient_send_errors():
    gateway = object.__new__(TelegramGateway)
    gateway.bot = FlakyTelegramBot()
    gateway.agent = FakeGatewayAgent("hello after retry")
    gateway.turn_coalescer = None
    gateway.send_reply_with_stickers(123, {"content": "hello after retry"}, 9)
    messages = [item for item in gateway.bot.sent if item[0] == "message"]
    if len(messages) != 1 or "hello after retry" not in messages[0][1]:
        raise AssertionError(gateway.bot.sent)
    if gateway.bot.failures_left != 0:
        raise AssertionError("transient failure was not consumed")
    return "telegram send retried after transient error"


def turn_coalesces_text_and_sticker():
    text_msg = fake_message(message_id=10, text="今天真的有點累")
    sticker_msg = fake_message(message_id=11)
    coalescer = MessageCoalescer(debounce_seconds=60)
    flushed = []
    coalescer.add(InboundMessagePart(123, 10, "text", text=text_msg.text, message=text_msg, timestamp=1.0), flushed.append)
    coalescer.add(
        InboundMessagePart(
            123,
            11,
            "sticker",
            path="C:\\Agent\\workspace\\telegram_images\\tired.webp",
            media_type="image",
            media_kind="sticker",
            message=sticker_msg,
            timestamp=1.1,
        ),
        flushed.append,
    )
    turn = coalescer.flush_chat(123, flushed.append)
    if len(flushed) != 1:
        raise AssertionError(flushed)
    if turn.primary_message_id != 10 or turn.mode != InteractionMode.CHAT:
        raise AssertionError(turn)
    prompt = build_turn_prompt("", turn)
    if "今天真的有點累" not in prompt or "sticker:" not in prompt or "不要主動調用 analyze_media" not in prompt:
        raise AssertionError(prompt)
    return "text plus sticker became one chat turn"


def turn_coalesces_sticker_then_text_with_text_primary():
    sticker_msg = fake_message(message_id=20)
    text_msg = fake_message(message_id=21, text="這個就是我的心情")
    turn = build_aggregated_turn(
        [
            InboundMessagePart(123, 20, "sticker", path="mood.webp", media_type="image", media_kind="sticker", message=sticker_msg, timestamp=1.0),
            InboundMessagePart(123, 21, "text", text=text_msg.text, message=text_msg, timestamp=1.2),
        ]
    )
    if turn.primary_message_id != 21 or turn.primary_text != "這個就是我的心情" or turn.mode != InteractionMode.CHAT:
        raise AssertionError(turn)
    return "text stayed primary even when sticker arrived first"


def turn_sticker_only_is_social_sticker():
    sticker_msg = fake_message(message_id=30)
    turn = build_aggregated_turn(
        [
            InboundMessagePart(123, 30, "sticker", path="cute.webp", media_type="image", media_kind="sticker", message=sticker_msg),
        ]
    )
    if turn.mode != InteractionMode.SOCIAL_STICKER:
        raise AssertionError(turn)
    prompt = build_turn_prompt("", turn)
    if "不要主動調用 analyze_media" not in prompt:
        raise AssertionError(prompt)
    return "sticker-only turn stayed social"


def turn_explicit_vision_request_uses_vision_task():
    text_msg = fake_message(message_id=40, text="幫我看圖，這是什麼")
    photo_msg = fake_message(message_id=41)
    turn = build_aggregated_turn(
        [
            InboundMessagePart(123, 40, "text", text=text_msg.text, message=text_msg, timestamp=1.0),
            InboundMessagePart(123, 41, "photo", path="photo.jpg", media_type="image", media_kind="photo", message=photo_msg, timestamp=1.1),
        ]
    )
    if turn.mode != InteractionMode.VISION_TASK:
        raise AssertionError(turn)
    prompt = build_turn_prompt("", turn)
    if "analyze_media" not in prompt or "明確要求" not in prompt:
        raise AssertionError(prompt)
    return "explicit media analysis became vision task"


def turn_debounce_default_is_55_seconds():
    old = os.environ.pop(TURN_DEBOUNCE_ENV, None)
    try:
        if DEFAULT_TURN_DEBOUNCE_SECONDS != 5.5:
            raise AssertionError(DEFAULT_TURN_DEBOUNCE_SECONDS)
        value = configured_turn_debounce_seconds()
        if value != 5.5:
            raise AssertionError(value)
        coalescer = MessageCoalescer()
        if coalescer.debounce_seconds != 5.5:
            raise AssertionError(coalescer.debounce_seconds)
        return f"default debounce is {value}s"
    finally:
        if old is not None:
            os.environ[TURN_DEBOUNCE_ENV] = old


def turn_debounce_env_override_is_used():
    old = os.environ.get(TURN_DEBOUNCE_ENV)
    os.environ[TURN_DEBOUNCE_ENV] = "3"
    try:
        value = configured_turn_debounce_seconds()
        if value != 3.0:
            raise AssertionError(value)
        coalescer = MessageCoalescer()
        if coalescer.debounce_seconds != 3.0:
            raise AssertionError(coalescer.debounce_seconds)
        return "env debounce override used"
    finally:
        if old is None:
            os.environ.pop(TURN_DEBOUNCE_ENV, None)
        else:
            os.environ[TURN_DEBOUNCE_ENV] = old


def turn_debounce_invalid_env_falls_back():
    old = os.environ.get(TURN_DEBOUNCE_ENV)
    os.environ[TURN_DEBOUNCE_ENV] = "not-a-number"
    try:
        value = configured_turn_debounce_seconds()
        if value != 5.5:
            raise AssertionError(value)
        return "invalid debounce env fell back"
    finally:
        if old is None:
            os.environ.pop(TURN_DEBOUNCE_ENV, None)
        else:
            os.environ[TURN_DEBOUNCE_ENV] = old


def turn_coalescer_records_trace_events():
    if os.path.exists(TRACE_LOG_FILE):
        os.remove(TRACE_LOG_FILE)
    msg = fake_message(message_id=50, text="trace this")
    coalescer = MessageCoalescer(debounce_seconds=60)
    coalescer.add(InboundMessagePart(123, 50, "text", text=msg.text, message=msg), lambda turn: None)
    coalescer.flush_chat(123)
    with open(TRACE_LOG_FILE, "r", encoding="utf-8") as file:
        events = [json.loads(line) for line in file if line.strip()]
    names = [event.get("event") for event in events]
    if "turn.part" not in names or "turn.flush" not in names:
        raise AssertionError(names)
    flush = next(event for event in events if event.get("event") == "turn.flush")
    if flush.get("part_count") != 1 or flush.get("primary_message_id") != 50 or flush.get("mode") != "chat":
        raise AssertionError(flush)
    return "turn aggregation trace events recorded"


def turn_parts_after_window_split_into_two_turns():
    first = build_aggregated_turn([InboundMessagePart(123, 60, "text", text="第一句", message=fake_message(message_id=60), timestamp=1.0)])
    second = build_aggregated_turn(
        [
            InboundMessagePart(
                123,
                61,
                "sticker",
                path="late.webp",
                media_type="image",
                media_kind="sticker",
                message=fake_message(message_id=61),
                timestamp=7.0,
            )
        ]
    )
    if first.primary_message_id == second.primary_message_id or first.mode != InteractionMode.CHAT or second.mode != InteractionMode.SOCIAL_STICKER:
        raise AssertionError((first, second))
    return "parts outside debounce window remain separate turns"


def gateway_autonomous_sticker_send():
    gateway = object.__new__(TelegramGateway)
    gateway.bot = FakeBot()
    gateway.agent = None
    sticker = os.path.basename(gateway_sticker_fuzzy_match())
    gateway.send_reply_with_stickers(123, {"content": f"喵 [表情包: {sticker}]"}, 9)
    kinds = [item[0] for item in gateway.bot.sent]
    if "message" not in kinds:
        raise AssertionError(kinds)
    if not any(kind in kinds for kind in ("photo", "animation", "sticker")):
        raise AssertionError(kinds)
    return "gateway sent sticker without tool approval"


def gateway_ascii_sticker_alias():
    gateway = object.__new__(TelegramGateway)
    gateway.bot = FakeBot()
    gateway.agent = None
    sticker = os.path.basename(gateway_sticker_fuzzy_match())
    gateway.send_reply_with_stickers(123, {"content": f"meow [sticker: {sticker}]"}, 9)
    kinds = [item[0] for item in gateway.bot.sent]
    if not any(kind in kinds for kind in ("photo", "animation", "sticker")):
        raise AssertionError(kinds)
    return "gateway accepted [sticker: ...] alias"


def gateway_dedupes_screenshot_markers():
    gateway = object.__new__(TelegramGateway)
    gateway.bot = FakeBot()
    gateway.agent = None
    screenshot_name = "dedupe_screen.png"
    screenshot_path = os.path.join(core_tools.PROJECT_CACHE_DIR, screenshot_name)
    with open(screenshot_path, "wb") as file:
        file.write(b"fake image bytes")
    gateway.send_reply_with_stickers(
        123,
        {"content": f"看這張 [系統截圖: {screenshot_name}] [screenshot: {screenshot_name}] [screenshot: {screenshot_name}]"},
        9,
    )
    photos = [item for item in gateway.bot.sent if item[0] == "photo"]
    if len(photos) != 1:
        raise AssertionError(gateway.bot.sent)
    if photos[0][2].get("caption") != "最後畫面截圖":
        raise AssertionError(photos)
    return "screenshot markers deduped"


def gateway_auto_attaches_social_sticker_for_battle_reply():
    gateway = object.__new__(TelegramGateway)
    gateway.bot = FakeBot()
    gateway.agent = FakeGatewayAgent("接招～")
    sticker = os.path.basename(gateway_sticker_fuzzy_match())
    message = fake_message(chat_id=777, message_id=70)
    gateway._chat_and_reply(
        message,
        "social sticker turn",
        InteractionMode.SOCIAL_STICKER,
        suggested_stickers=[sticker],
        allow_auto_sticker=True,
    )
    kinds = [item[0] for item in gateway.bot.sent]
    if not any(kind in kinds for kind in ("photo", "animation", "sticker")):
        raise AssertionError(gateway.bot.sent)
    return "auto social sticker attached"


def gateway_does_not_duplicate_existing_social_sticker():
    gateway = object.__new__(TelegramGateway)
    gateway.bot = FakeBot()
    sticker = os.path.basename(gateway_sticker_fuzzy_match())
    gateway.agent = FakeGatewayAgent(f"我自己選好了\n[表情包: {sticker}]")
    message = fake_message(chat_id=778, message_id=71)
    gateway._chat_and_reply(
        message,
        "social sticker turn",
        InteractionMode.SOCIAL_STICKER,
        suggested_stickers=[sticker],
        allow_auto_sticker=True,
    )
    media_count = sum(1 for item in gateway.bot.sent if item[0] in {"photo", "animation", "sticker"})
    if media_count != 1:
        raise AssertionError(gateway.bot.sent)
    return "existing social sticker was not duplicated"


def gateway_records_sent_sticker_in_social_session():
    gateway = object.__new__(TelegramGateway)
    gateway.bot = FakeBot()
    gateway.agent = None
    sticker = os.path.basename(gateway_sticker_fuzzy_match())
    main_module.DEFAULT_SOCIAL_SESSION_MANAGER.sessions.pop("555", None)
    if not gateway._send_sticker_asset(555, sticker):
        raise AssertionError(sticker)
    state = main_module.DEFAULT_SOCIAL_SESSION_MANAGER.sessions.get("555")
    if not state or sticker not in state.recent_sent:
        raise AssertionError(state)
    return state.recent_sent


def social_sticker_tag_inference():
    tags = infer_sticker_tags("Looking at you angrily.gif")
    if "angry" not in tags:
        raise AssertionError(tags)
    intent = infer_intent_tags("來鬥圖，哈哈")
    if "battle" not in intent or "happy" not in intent:
        raise AssertionError(intent)
    return {"tags": tags, "intent": intent}


def social_session_infers_modes():
    if infer_social_mode("battle me", has_sticker=True) != "sticker_battle":
        raise AssertionError("battle was not detected")
    if infer_social_mode("love heart", has_sticker=False) != "affection":
        raise AssertionError("affection was not detected")
    if infer_social_mode("", has_sticker=True, turn_mode="social_sticker") != "sticker_battle":
        raise AssertionError("sticker-only social turn was not detected")
    return "social modes inferred"


def social_reply_policy_guides_social_rhythm():
    battle = social_reply_policy_for("sticker_battle", ["battle"], has_sticker=True)
    affection = social_reply_policy_for("affection", ["affection", "cute"], has_sticker=False)
    idle = social_reply_policy_for("idle", [], has_sticker=False)
    if battle.max_sentences > 2 or not battle.should_attach_sticker or battle.allow_tools:
        raise AssertionError(battle)
    if "warm" not in affection.tone or not affection.should_attach_sticker or affection.allow_tools:
        raise AssertionError(affection)
    if idle.should_attach_sticker or idle.allow_tools:
        raise AssertionError(idle)
    note = battle.to_prompt_note()
    if "Social reply policy" not in note or "do not use tools" not in note:
        raise AssertionError(note)
    return {"battle": battle.tone, "affection": affection.tone}


def social_session_suggests_and_avoids_recent_stickers():
    temp_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "social_session_stickers")
    temp_index = os.path.join(core_tools.PROJECT_CACHE_DIR, "social_session_index.json")
    os.makedirs(temp_dir, exist_ok=True)
    for name in ("Pointing at you.gif", "laugh.gif", "heart_love.webp"):
        with open(os.path.join(temp_dir, name), "wb") as file:
            file.write(b"sticker")
    index = SocialStickerIndex(path=temp_index, sticker_dir=temp_dir)
    index.rebuild_from_files()
    sessions = SocialSessionManager(ttl_seconds=60)
    state = sessions.observe_turn(42, text="battle me haha", has_sticker=True, mode="social_sticker")
    if state.mode != "sticker_battle":
        raise AssertionError(state)
    first = sessions.suggest_stickers(42, index, "battle me", limit=2)
    if not first:
        raise AssertionError((state, index.entries))
    sessions.mark_sticker_sent(42, first[0])
    second = sessions.suggest_stickers(42, index, "battle me", limit=2)
    if first[0] in second:
        raise AssertionError((first, second))
    note = sessions.build_prompt_note(42, second)
    if "Social session" not in note or "sticker-battle" not in note:
        raise AssertionError(note)
    return {"first": first, "second": second}


def social_sticker_index_rebuild_and_choose():
    temp_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "social_sticker_test")
    temp_index = os.path.join(core_tools.PROJECT_CACHE_DIR, "social_sticker_test.json")
    os.makedirs(temp_dir, exist_ok=True)
    for name in ("laugh.gif", "question mark.gif", "plain.bin"):
        with open(os.path.join(temp_dir, name), "wb") as file:
            file.write(b"x")
    index = SocialStickerIndex(path=temp_index, sticker_dir=temp_dir)
    count = index.rebuild_from_files()
    picks = index.choose("happy")
    if count < 2 or "laugh.gif" not in picks:
        raise AssertionError((count, picks, index.entries))
    return picks


def social_sticker_catalog_incoming():
    temp_index = os.path.join(core_tools.PROJECT_CACHE_DIR, "incoming_sticker_test.json")
    index = SocialStickerIndex(path=temp_index, sticker_dir=core_tools.PROJECT_CACHE_DIR)
    entry = index.catalog_incoming(os.path.join(core_tools.PROJECT_CACHE_DIR, "tg_sticker_1.webp"), media_type="image")
    if entry.source != "incoming" or "incoming" not in entry.tags or entry.approved_for_autouse:
        raise AssertionError(entry)
    return entry.tags


def social_sticker_metadata_tags_and_dedup():
    source_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "sticker_metadata_source")
    temp_index = os.path.join(core_tools.PROJECT_CACHE_DIR, "sticker_metadata_index.json")
    os.makedirs(source_dir, exist_ok=True)
    first_path = os.path.join(source_dir, "tg_sticker_100.webp")
    second_path = os.path.join(source_dir, "tg_sticker_101.webp")
    for path in (first_path, second_path):
        with open(path, "wb") as file:
            file.write(b"same telegram sticker")
    if "affection" not in infer_metadata_tags({"emoji": "🥰"}):
        raise AssertionError("emoji tags were not inferred")
    index = SocialStickerIndex(path=temp_index, sticker_dir=source_dir)
    first = index.catalog_incoming(first_path, media_type="image", metadata={"file_unique_id": "unique-1", "emoji": "🥰", "set_name": "cute_pack"})
    second = index.catalog_incoming(second_path, media_type="image", metadata={"file_unique_id": "unique-1", "emoji": "🥰", "set_name": "cute_pack"})
    candidates = index.list_candidates(limit=10)
    if len(candidates) != 1 or first.filename != second.filename:
        raise AssertionError((first, second, candidates, index.entries))
    entry = candidates[0]
    if "affection" not in entry.tags or entry.file_unique_id != "unique-1" or entry.emoji != "🥰" or not entry.content_hash:
        raise AssertionError(entry)
    return {"filename": entry.filename, "tags": entry.tags}


def social_sticker_filters_mature_content():
    unsafe_name = "I want to make love to you(flirt).jpg"
    if is_safe_sticker(unsafe_name):
        raise AssertionError(unsafe_name)
    if infer_sticker_tags(unsafe_name) != ["restricted"]:
        raise AssertionError(infer_sticker_tags(unsafe_name))
    temp_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "social_sticker_safe_test")
    temp_index = os.path.join(core_tools.PROJECT_CACHE_DIR, "social_sticker_safe_test.json")
    os.makedirs(temp_dir, exist_ok=True)
    for name in ("laugh.gif", unsafe_name):
        with open(os.path.join(temp_dir, name), "wb") as file:
            file.write(b"x")
    index = SocialStickerIndex(path=temp_index, sticker_dir=temp_dir)
    index.rebuild_from_files()
    picks = index.choose("flirt")
    if unsafe_name in index.entries or unsafe_name in picks:
        raise AssertionError((index.entries, picks))
    return picks


def social_sticker_index_migrates_unsafe_old_entries():
    temp_index = os.path.join(core_tools.PROJECT_CACHE_DIR, "social_sticker_migration_test.json")
    unsafe_name = "unsafe flirt.jpg"
    with open(temp_index, "w", encoding="utf-8") as file:
        json.dump({unsafe_name: {"filename": unsafe_name, "tags": ["happy"], "source": "local", "uses": 0}}, file)
    index = SocialStickerIndex(path=temp_index, sticker_dir=core_tools.PROJECT_CACHE_DIR)
    entry = index.entries[unsafe_name]
    if entry.safe_for_minor or entry.approved_for_autouse or "restricted" not in entry.tags:
        raise AssertionError(entry)
    if index.choose("happy"):
        raise AssertionError(index.choose("happy"))
    return entry.tags


def social_sticker_candidate_approval_copies_and_selects():
    source_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "sticker_candidate_source")
    target_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "sticker_candidate_target")
    index_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "sticker_candidate_index.json")
    os.makedirs(source_dir, exist_ok=True)
    os.makedirs(target_dir, exist_ok=True)
    source = os.path.join(source_dir, "cute_meow.webp")
    with open(source, "wb") as file:
        file.write(b"sticker")
    index = SocialStickerIndex(path=index_path, sticker_dir=target_dir)
    candidate = index.catalog_incoming(source, media_type="image", tags=["cute"])
    if candidate.approved_for_autouse:
        raise AssertionError(candidate)
    approved = index.approve_candidate(candidate.filename, tags=["affection"])
    if not approved.approved_for_autouse or approved.source != "approved_incoming":
        raise AssertionError(approved)
    if not os.path.exists(os.path.join(target_dir, approved.filename)):
        raise AssertionError(approved)
    if approved.filename not in index.choose("cute"):
        raise AssertionError(index.choose("cute"))
    return approved.filename


def social_sticker_safe_affection_and_teasing_allowed():
    temp_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "sticker_affection_target")
    temp_index = os.path.join(core_tools.PROJECT_CACHE_DIR, "sticker_affection_index.json")
    os.makedirs(temp_dir, exist_ok=True)
    for name in ("愛心.webp", "嘴硬.webp", "unsafe_flirt.webp"):
        with open(os.path.join(temp_dir, name), "wb") as file:
            file.write(b"sticker")
    index = SocialStickerIndex(path=temp_index, sticker_dir=temp_dir)
    index.rebuild_from_files()
    affection = index.choose("心動 貼貼")
    teasing = index.choose("嘴硬")
    if "愛心.webp" not in affection or "嘴硬.webp" not in teasing:
        raise AssertionError((affection, teasing, index.entries))
    if "unsafe_flirt.webp" in index.entries:
        raise AssertionError(index.entries)
    return {"affection": affection, "teasing": teasing}


def social_sticker_candidate_reject_blocks_selection():
    source_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "sticker_reject_source")
    index_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "sticker_reject_index.json")
    os.makedirs(source_dir, exist_ok=True)
    source = os.path.join(source_dir, "question_mark.webp")
    with open(source, "wb") as file:
        file.write(b"sticker")
    index = SocialStickerIndex(path=index_path, sticker_dir=source_dir)
    candidate = index.catalog_incoming(source, media_type="image", tags=["confused"])
    rejected = index.reject_candidate(candidate.filename, reason="not wanted")
    if not rejected.rejected or rejected.approved_for_autouse:
        raise AssertionError(rejected)
    if index.choose("confused"):
        raise AssertionError(index.choose("confused"))
    return rejected.filename


def social_sticker_unsafe_candidate_cannot_be_approved():
    source_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "sticker_unsafe_source")
    target_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "sticker_unsafe_target")
    index_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "sticker_unsafe_index.json")
    os.makedirs(source_dir, exist_ok=True)
    os.makedirs(target_dir, exist_ok=True)
    source = os.path.join(source_dir, "unsafe_flirt.webp")
    with open(source, "wb") as file:
        file.write(b"sticker")
    index = SocialStickerIndex(path=index_path, sticker_dir=target_dir)
    candidate = index.catalog_incoming(source, media_type="image")
    try:
        index.approve_candidate(candidate.filename)
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe candidate was approved")
    if not index.entries[candidate.filename].rejected:
        raise AssertionError(index.entries[candidate.filename])
    return index.entries[candidate.filename].tags


def sticker_curation_command_payload_parses_quotes():
    filename, tags = _split_sticker_command_payload('"cute meow.webp" cute affection')
    if filename != "cute meow.webp" or tags != ["cute", "affection"]:
        raise AssertionError((filename, tags))
    filename, tags = _split_sticker_command_payload("plain.webp happy,agree")
    if filename != "plain.webp" or tags != ["happy", "agree"]:
        raise AssertionError((filename, tags))
    return filename


def social_sticker_batch_approval_and_summary():
    source_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "sticker_batch_source")
    target_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "sticker_batch_target")
    index_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "sticker_batch_index.json")
    os.makedirs(source_dir, exist_ok=True)
    os.makedirs(target_dir, exist_ok=True)
    for name, emoji in (("batch_heart.webp", "🥰"), ("batch_laugh.webp", "😂"), ("batch_question.webp", "🤔")):
        path = os.path.join(source_dir, name)
        with open(path, "wb") as file:
            file.write(name.encode("utf-8"))
    index = SocialStickerIndex(path=index_path, sticker_dir=target_dir)
    index.catalog_incoming(os.path.join(source_dir, "batch_heart.webp"), media_type="image", metadata={"file_unique_id": "batch-1", "emoji": "🥰", "set_name": "cute_pack"})
    index.catalog_incoming(os.path.join(source_dir, "batch_laugh.webp"), media_type="image", metadata={"file_unique_id": "batch-2", "emoji": "😂", "set_name": "fun_pack"})
    index.catalog_incoming(os.path.join(source_dir, "batch_question.webp"), media_type="image", metadata={"file_unique_id": "batch-3", "emoji": "🤔", "set_name": "question_pack"})
    summary = index.summarize_candidates(limit=2)
    if len(summary) != 2 or "emoji=" not in summary[0] or "id=batch-" not in summary[0]:
        raise AssertionError(summary)
    approved = index.approve_recent_candidates(2, tags=["batch"])
    if len(approved) != 2 or len(index.list_candidates(limit=10)) != 1:
        raise AssertionError((approved, index.list_candidates(limit=10)))
    rejected = index.reject_recent_candidates(5, reason="self test")
    if len(rejected) != 1 or index.list_candidates(limit=10):
        raise AssertionError((rejected, index.entries))
    return {"approved": [item.filename for item in approved], "rejected": [item.filename for item in rejected]}


def social_curation_reminder_is_low_noise():
    reminder = SocialCurationReminder(threshold=3, cooldown_seconds=60)
    if reminder.should_remind(123, 2, now=100):
        raise AssertionError("reminded below threshold")
    if not reminder.should_remind(123, 3, now=100):
        raise AssertionError("did not remind at threshold")
    if reminder.should_remind(123, 4, now=120):
        raise AssertionError("reminded during cooldown")
    if not reminder.should_remind(123, 5, now=200):
        raise AssertionError("did not remind after cooldown and count increase")
    if "approve recent 3 stickers" not in reminder.message(5):
        raise AssertionError(reminder.message(5))
    return "curation reminder is throttled"


def gateway_batch_approves_recent_sticker_candidates():
    source_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "gateway_batch_source")
    target_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "gateway_batch_target")
    index_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "gateway_batch_index.json")
    os.makedirs(source_dir, exist_ok=True)
    os.makedirs(target_dir, exist_ok=True)
    for name in ("gateway_batch_1.webp", "gateway_batch_2.webp"):
        path = os.path.join(source_dir, name)
        with open(path, "wb") as file:
            file.write(name.encode("utf-8"))
    index = SocialStickerIndex(path=index_path, sticker_dir=target_dir)
    index.catalog_incoming(os.path.join(source_dir, "gateway_batch_1.webp"), media_type="image", tags=["cute"])
    index.catalog_incoming(os.path.join(source_dir, "gateway_batch_2.webp"), media_type="image", tags=["happy"])
    old_index = main_module.DEFAULT_SOCIAL_STICKER_INDEX
    gateway = object.__new__(TelegramGateway)
    gateway.bot = FakeBot()
    try:
        main_module.DEFAULT_SOCIAL_STICKER_INDEX = index
        handled = gateway._handle_sticker_curation_command(fake_message(text="approve recent 2 stickers affection"))
    finally:
        main_module.DEFAULT_SOCIAL_STICKER_INDEX = old_index
    if not handled or index.list_candidates(limit=10):
        raise AssertionError((handled, index.list_candidates(limit=10), gateway.bot.sent))
    replies = [item[1][1] for item in gateway.bot.sent if item[0] == "reply"]
    if not replies or "gateway_batch" not in replies[-1] or "affection" not in replies[-1]:
        raise AssertionError(gateway.bot.sent)
    return replies[-1]


def gateway_approves_latest_sticker_candidate():
    source_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "gateway_latest_source")
    target_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "gateway_latest_target")
    index_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "gateway_latest_index.json")
    os.makedirs(source_dir, exist_ok=True)
    os.makedirs(target_dir, exist_ok=True)
    source = os.path.join(source_dir, "latest_meow.webp")
    with open(source, "wb") as file:
        file.write(b"sticker")
    index = SocialStickerIndex(path=index_path, sticker_dir=target_dir)
    index.catalog_incoming(source, media_type="image", tags=["cute"])
    old_index = main_module.DEFAULT_SOCIAL_STICKER_INDEX
    gateway = object.__new__(TelegramGateway)
    gateway.bot = FakeBot()
    try:
        main_module.DEFAULT_SOCIAL_STICKER_INDEX = index
        handled = gateway._handle_sticker_curation_command(fake_message(text="批准最新貼圖 affection"))
    finally:
        main_module.DEFAULT_SOCIAL_STICKER_INDEX = old_index
    if not handled or not any("已批准貼圖" in (item[1][1] if len(item[1]) > 1 else "") for item in gateway.bot.sent if item[0] == "reply"):
        raise AssertionError(gateway.bot.sent)
    if not index.choose("affection"):
        raise AssertionError(index.entries)
    return gateway.bot.sent[-1][1][1]


def gateway_rejects_latest_sticker_candidate():
    source_dir = os.path.join(core_tools.PROJECT_CACHE_DIR, "gateway_reject_source")
    index_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "gateway_reject_index.json")
    os.makedirs(source_dir, exist_ok=True)
    source = os.path.join(source_dir, "latest_question.webp")
    with open(source, "wb") as file:
        file.write(b"sticker")
    index = SocialStickerIndex(path=index_path, sticker_dir=source_dir)
    index.catalog_incoming(source, media_type="image", tags=["confused"])
    old_index = main_module.DEFAULT_SOCIAL_STICKER_INDEX
    gateway = object.__new__(TelegramGateway)
    gateway.bot = FakeBot()
    try:
        main_module.DEFAULT_SOCIAL_STICKER_INDEX = index
        handled = gateway._handle_sticker_curation_command(fake_message(text="拒絕最新貼圖"))
    finally:
        main_module.DEFAULT_SOCIAL_STICKER_INDEX = old_index
    if not handled or not any("已拒絕貼圖" in (item[1][1] if len(item[1]) > 1 else "") for item in gateway.bot.sent if item[0] == "reply"):
        raise AssertionError(gateway.bot.sent)
    if index.choose("confused"):
        raise AssertionError(index.entries)
    return gateway.bot.sent[-1][1][1]


def search_sticker_uses_social_index():
    result = core_tools.real_search_sticker("happy")
    if result.status != "ok":
        raise AssertionError(result.to_text())
    data = result.data or []
    if not data:
        raise AssertionError(result.to_text())
    return result.to_text()


def search_sticker_blocks_mature_query_results():
    result = core_tools.real_search_sticker("flirt")
    if result.status != "ok":
        raise AssertionError(result.to_text())
    data = result.data or []
    if data:
        raise AssertionError(data)
    if "safe" in result.message.casefold():
        raise AssertionError(result.to_text())
    return result.to_text()


def search_sticker_supports_safe_battle_query():
    result = core_tools.real_search_sticker("鬥圖")
    if result.status != "ok":
        raise AssertionError(result.to_text())
    data = result.data or []
    if not data or any(not is_safe_sticker(name) for name in data):
        raise AssertionError(result.to_text())
    return result.to_text()


def react_to_message_missing_context():
    old_context = dict(core_tools.TELEGRAM_CONTEXT)
    core_tools.set_telegram_context("", "")
    try:
        result = core_tools.real_react_to_message("👍")
        if result.status != "error" or "Missing Telegram" not in result.message:
            raise AssertionError(result.to_text())
        return result.message
    finally:
        core_tools.TELEGRAM_CONTEXT.update(old_context)


def backup_task_plan():
    global _task_plan_backup
    if os.path.exists(core_tools.TASK_PLAN_FILE):
        with open(core_tools.TASK_PLAN_FILE, "r", encoding="utf-8") as file:
            _task_plan_backup = file.read()
    else:
        _task_plan_backup = None


def backup_memory():
    global _memory_backup
    if os.path.exists(core_tools.MEMORY_FILE):
        with open(core_tools.MEMORY_FILE, "r", encoding="utf-8") as file:
            _memory_backup = file.read()
    else:
        _memory_backup = None


def backup_session_brain():
    global _session_brain_backup
    if os.path.exists(SESSION_BRAIN_FILE):
        with open(SESSION_BRAIN_FILE, "r", encoding="utf-8") as file:
            _session_brain_backup = file.read()
    else:
        _session_brain_backup = None


def backup_reliability_state():
    global _transactions_backup, _failure_replay_backup, _rolling_summary_backup, _memory_compiled_backup, _memory_health_backup
    global _knowledge_manifest_backup, _knowledge_chunks_backup, _knowledge_index_backup
    global _eval_report_backup, _task_benchmark_backup
    global _task_graphs_backup, _workflow_replay_backup
    global _worker_jobs_backup, _worker_results_backup
    global _context_budget_report_backup, _subagent_runs_backup
    global _trace_log_backup
    global _presence_state_backup, _presence_candidates_backup, _presence_health_backup, _presence_debug_backup
    _trace_log_backup = _read_optional_file(TRACE_LOG_FILE)
    _transactions_backup = _read_optional_file(TASK_TRANSACTIONS_FILE)
    _failure_replay_backup = _read_optional_file(FAILURE_REPLAY_FILE)
    _rolling_summary_backup = _read_optional_file(ROLLING_SUMMARY_FILE)
    _memory_compiled_backup = _read_optional_file(MEMORY_COMPILED_FILE)
    _memory_health_backup = _read_optional_file(MEMORY_HEALTH_FILE)
    _knowledge_manifest_backup = _read_optional_file(KNOWLEDGE_MANIFEST_FILE)
    _knowledge_chunks_backup = _read_optional_file(KNOWLEDGE_CHUNKS_FILE)
    _knowledge_index_backup = _read_optional_file(KNOWLEDGE_INDEX_FILE)
    _eval_report_backup = _read_optional_file(EVAL_REPORT_FILE)
    _task_benchmark_backup = _read_optional_file(TASK_BENCHMARK_FILE)
    _task_graphs_backup = _read_optional_file(TASK_GRAPHS_FILE)
    _workflow_replay_backup = _read_optional_file(WORKFLOW_REPLAY_FILE)
    _worker_jobs_backup = _read_optional_file(WORKER_JOBS_FILE)
    _worker_results_backup = _read_optional_file(WORKER_RESULTS_FILE)
    _context_budget_report_backup = _read_optional_file(CONTEXT_BUDGET_REPORT_FILE)
    _subagent_runs_backup = _read_optional_file(SUBAGENT_RUNS_FILE)
    _presence_state_backup = _read_optional_file(PRESENCE_STATE_FILE)
    _presence_candidates_backup = _read_optional_file(PRESENCE_CANDIDATES_FILE)
    _presence_health_backup = _read_optional_file(PRESENCE_HEALTH_FILE)
    _presence_debug_backup = _read_optional_file(PRESENCE_DEBUG_FILE)


def restore_task_plan():
    if _task_plan_backup is None:
        try:
            os.remove(core_tools.TASK_PLAN_FILE)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(core_tools.TASK_PLAN_FILE), exist_ok=True)
    with open(core_tools.TASK_PLAN_FILE, "w", encoding="utf-8") as file:
        file.write(_task_plan_backup)


def restore_memory():
    if _memory_backup is None:
        try:
            os.remove(core_tools.MEMORY_FILE)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(core_tools.MEMORY_FILE), exist_ok=True)
    with open(core_tools.MEMORY_FILE, "w", encoding="utf-8") as file:
        file.write(_memory_backup)


def restore_session_brain():
    if _session_brain_backup is None:
        try:
            os.remove(SESSION_BRAIN_FILE)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(SESSION_BRAIN_FILE), exist_ok=True)
    with open(SESSION_BRAIN_FILE, "w", encoding="utf-8") as file:
        file.write(_session_brain_backup)


def restore_reliability_state():
    _restore_optional_file(TRACE_LOG_FILE, _trace_log_backup)
    _restore_optional_file(TASK_TRANSACTIONS_FILE, _transactions_backup)
    _restore_optional_file(FAILURE_REPLAY_FILE, _failure_replay_backup)
    _restore_optional_file(ROLLING_SUMMARY_FILE, _rolling_summary_backup)
    _restore_optional_file(MEMORY_COMPILED_FILE, _memory_compiled_backup)
    _restore_optional_file(MEMORY_HEALTH_FILE, _memory_health_backup)
    _restore_optional_file(KNOWLEDGE_MANIFEST_FILE, _knowledge_manifest_backup)
    _restore_optional_file(KNOWLEDGE_CHUNKS_FILE, _knowledge_chunks_backup)
    _restore_optional_file(KNOWLEDGE_INDEX_FILE, _knowledge_index_backup)
    _restore_optional_file(EVAL_REPORT_FILE, _eval_report_backup)
    _restore_optional_file(TASK_BENCHMARK_FILE, _task_benchmark_backup)
    _restore_optional_file(TASK_GRAPHS_FILE, _task_graphs_backup)
    _restore_optional_file(WORKFLOW_REPLAY_FILE, _workflow_replay_backup)
    _restore_optional_file(WORKER_JOBS_FILE, _worker_jobs_backup)
    _restore_optional_file(WORKER_RESULTS_FILE, _worker_results_backup)
    _restore_optional_file(CONTEXT_BUDGET_REPORT_FILE, _context_budget_report_backup)
    _restore_optional_file(SUBAGENT_RUNS_FILE, _subagent_runs_backup)
    _restore_optional_file(PRESENCE_STATE_FILE, _presence_state_backup)
    _restore_optional_file(PRESENCE_CANDIDATES_FILE, _presence_candidates_backup)
    _restore_optional_file(PRESENCE_HEALTH_FILE, _presence_health_backup)
    _restore_optional_file(PRESENCE_DEBUG_FILE, _presence_debug_backup)


def _read_optional_file(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as file:
        return file.read()


def _restore_optional_file(path: str, content: str | None) -> None:
    if content is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        file.write(content)


def cleanup_self_test_files():
    for name in ("self_test.txt", "permission_test.txt", "wrong_tool.txt", "turn.txt", "delete_me_self_test.txt", "download_test.html", "dedupe_screen.png", "retry_last_safe_step.png", "retry_last_safe_step_original_graph.png", "benchmark_outcome_retry.png", "self_test_lock_unit_test.lock", "observability_trace_test.jsonl", "eval_trace_test.jsonl", "eval_missing_trace.jsonl", "eval_benchmark_trace_test.jsonl", "eval_artifact_history_trace_test.jsonl", "eval_report_test.json", "task_benchmark_self_test.json", "benchmark_task_graph.json", "benchmark_blocked_graph.json", "benchmark_outcome_retry_graph.json", "workflow_eval_trace_test.jsonl", "worker_eval_trace_test.jsonl", "control_plane_eval_trace_test.jsonl", "worker_jobs_test.jsonl", "worker_results_test.jsonl", "worker_fail_jobs_test.jsonl", "worker_fail_results_test.jsonl", "worker_timeout_jobs_test.jsonl", "worker_timeout_results_test.jsonl", "worker_reject_jobs_test.jsonl", "worker_reject_results_test.jsonl", "worker_subagent_jobs_test.jsonl", "worker_subagent_results_test.jsonl", "continue_worker_jobs_test.jsonl", "continue_worker_results_test.jsonl", "continue_reject_jobs_test.jsonl", "continue_reject_results_test.jsonl", "action_verify.txt", "transaction_test.txt", "transaction_test.json", "task_graph_test.json", "task_graph_permission_test.json", "task_graph_recovery_evidence_test.json", "task_graph_failed_recovery_attempt_test.json", "planner_graph_test.json", "planner_tool_graph_test.json", "worker_assim_graph_test.json", "observe_graph_test.json", "workflow_replay_graph_test.json", "workflow_replay_test.jsonl", "outcome_safe_retry_graph_test.json", "outcome_original_graph_retry_test.json", "graph_file.txt", "failure_replay_test.jsonl"):
        try:
            os.remove(os.path.join(core_tools.PROJECT_CACHE_DIR, name))
        except FileNotFoundError:
            pass
    for name in (
        "social_sticker_test.json",
        "incoming_sticker_test.json",
        "sticker_metadata_index.json",
        "social_sticker_safe_test.json",
        "social_sticker_migration_test.json",
        "sticker_candidate_index.json",
        "sticker_reject_index.json",
        "sticker_unsafe_index.json",
        "sticker_affection_index.json",
        "social_session_index.json",
        "sticker_batch_index.json",
        "gateway_batch_index.json",
        "gateway_latest_index.json",
        "gateway_reject_index.json",
    ):
        try:
            os.remove(os.path.join(core_tools.PROJECT_CACHE_DIR, name))
        except FileNotFoundError:
            pass
    for name in (
        "sticker_candidate_source",
        "sticker_candidate_target",
        "sticker_reject_source",
        "sticker_unsafe_source",
        "sticker_unsafe_target",
        "sticker_affection_target",
        "sticker_metadata_source",
        "sticker_batch_source",
        "sticker_batch_target",
        "social_session_stickers",
        "gateway_batch_source",
        "gateway_batch_target",
        "gateway_latest_source",
        "gateway_latest_target",
        "gateway_reject_source",
        "presence_shadow_test",
        "presence_cooldown_test",
        "presence_active_task_test",
        "presence_soft_quiet_test",
        "presence_stale_task_test",
        "presence_notify_test",
        "presence_shadow_tick_test",
        "presence_composer_send_test",
        "presence_composer_reject_test",
        "presence_composer_error_test",
        "presence_icebreak_test",
        "presence_debug_test",
        "benchmark_presence",
    ):
        path = os.path.join(core_tools.PROJECT_CACHE_DIR, name)
        if os.path.isdir(path):
            for child in os.listdir(path):
                try:
                    os.remove(os.path.join(path, child))
                except FileNotFoundError:
                    pass
            try:
                os.rmdir(path)
            except OSError:
                pass


def delete_file_round_trip():
    filename = "project_cache/delete_me_self_test.txt"
    write_result = core_tools.real_write_file(filename, "delete me")
    if write_result.status != "ok":
        raise AssertionError(write_result.to_text())
    delete_result = core_tools.real_delete_file(filename)
    if delete_result.status != "ok":
        raise AssertionError(delete_result.to_text())
    return delete_result.to_text()


def execute_command_defaults_to_project_root():
    result = core_tools.real_execute_command("python -m py_compile core_tools.py", timeout=60)
    if result.status != "ok":
        raise AssertionError(result.to_text())
    data = result.data or {}
    if data.get("cwd") != "project" or data.get("resolved_cwd") != core_tools.ROOT_DIR:
        raise AssertionError(data)
    return data.get("resolved_cwd")


def execute_command_workspace_cwd_is_supported():
    result = core_tools.real_execute_command("python -m py_compile ../core_tools.py", timeout=60, cwd="workspace")
    if result.status != "ok":
        raise AssertionError(result.to_text())
    if (result.data or {}).get("cwd") != "workspace":
        raise AssertionError(result.data)
    return result.data.get("resolved_cwd")


def execute_command_rejects_invalid_cwd():
    result = core_tools.real_execute_command("echo no", cwd="C:/")
    if result.status != "error" or "Invalid cwd" not in result.message:
        raise AssertionError(result.to_text())
    return result.data


def execute_command_missing_file_has_retry_hint():
    result = core_tools.real_execute_command("python -m py_compile missing_core_file.py", timeout=60)
    if result.status != "error":
        raise AssertionError(result.to_text())
    data = result.data or {}
    if not data.get("resolved_cwd") or "retry_hint" not in data:
        raise AssertionError(data)
    return data.get("retry_hint") or "cwd metadata present"


def gitignore_exists_for_private_runtime_files():
    path = os.path.join(os.path.dirname(__file__), ".gitignore")
    with open(path, "r", encoding="utf-8") as file:
        text = file.read()
    required = ["workspace/chat_history/", "workspace/logs/", "workspace/project_cache/", "workspace/tg_chat_id.txt", "__pycache__/", ".env"]
    missing = [item for item in required if item not in text]
    if missing:
        raise AssertionError(missing)
    return "private runtime files ignored"


def git_index_excludes_private_runtime_files():
    if not os.path.isdir(os.path.join(os.path.dirname(__file__), ".git")):
        return "skipped; not a git repo"
    result = subprocess.run(["git", "ls-files", "-z"], cwd=os.path.dirname(__file__), capture_output=True, timeout=30)
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    forbidden = ("__pycache__/", "workspace/chat_history/", "workspace/logs/", "workspace/project_cache/", "workspace/tg_chat_id.txt")
    files = result.stdout.decode("utf-8", errors="replace").split("\0")
    leaked = [line for line in files if line.startswith(forbidden) or line.endswith(".pyc")]
    if leaked:
        raise AssertionError(leaked[:20])
    return "git index excludes private runtime files"


def launcher_script_has_single_instance_guard():
    path = os.path.join(os.path.dirname(__file__), "start_yueyue.ps1")
    with open(path, "r", encoding="utf-8") as file:
        text = file.read()
    required = [
        "[switch]$Restart",
        "yueyue_launcher.pid",
        "Assert-SingleLauncher",
        "Write-LauncherPid",
        "Clear-LauncherPid",
        "Test-ProcessAlive",
        "Stop-ProcessTree",
        "Get-CimInstance Win32_Process -Filter",
        "ParentProcessId",
        "Use -Restart",
    ]
    missing = [item for item in required if item not in text]
    if missing:
        raise AssertionError(missing)
    if "if (-not $CheckOnly) {\n        Assert-SingleLauncher" not in text:
        raise AssertionError("single-instance guard should not run in CheckOnly mode")
    return "launcher has duplicate-start and restart guard"


def launcher_docs_explain_restart():
    root = os.path.dirname(__file__)
    with open(os.path.join(root, "README.md"), "r", encoding="utf-8") as file:
        readme = file.read()
    with open(os.path.join(root, "RUNBOOK.md"), "r", encoding="utf-8") as file:
        runbook = file.read()
    for text, name in ((readme, "README.md"), (runbook, "RUNBOOK.md")):
        if "-Restart" not in text:
            raise AssertionError(f"{name} does not document -Restart")
    if "yueyue_launcher.pid" not in runbook:
        raise AssertionError("RUNBOOK.md does not explain launcher pid file")
    return "launcher restart documented"


def self_test_lock_blocks_parallel_run_and_recovers_stale_lock():
    lock_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "self_test_lock_unit_test.lock")
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass
    if not acquire_self_test_lock(lock_path):
        raise AssertionError("first lock acquire failed")
    try:
        if acquire_self_test_lock(lock_path):
            raise AssertionError("second lock acquire should fail")
    finally:
        release_self_test_lock(lock_path)
    with open(lock_path, "w", encoding="utf-8") as file:
        file.write("stale\n")
    old = time.time() - (SELF_TEST_LOCK_STALE_SECONDS + 60)
    os.utime(lock_path, (old, old))
    if not acquire_self_test_lock(lock_path):
        raise AssertionError("stale lock was not recovered")
    release_self_test_lock(lock_path)
    if os.path.exists(lock_path):
        raise AssertionError("lock file was not released")
    return "self_test lock blocks parallel runs and recovers stale lock"


def live_telegram_smoke():
    if os.getenv("RUN_LIVE_TELEGRAM_SMOKE") != "1":
        return "skipped; set RUN_LIVE_TELEGRAM_SMOKE=1 to send real Telegram smoke messages"
    if not core_tools.TG_TOKEN or not os.path.exists(core_tools.CHAT_ID_FILE):
        return "skipped; Telegram token or chat id missing"
    import requests

    with open(core_tools.CHAT_ID_FILE, "r", encoding="utf-8") as file:
        chat_id = file.read().strip()
    response = requests.post(
        f"https://api.telegram.org/bot{core_tools.TG_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": "Codex smoke test: runtime online."},
        timeout=15,
    )
    data = response.json()
    if not response.ok or not data.get("ok"):
        raise AssertionError(response.text)
    message_id = data["result"]["message_id"]
    core_tools.set_telegram_context(chat_id, message_id)
    reaction = core_tools.real_react_to_message("👍")
    if reaction.status != "ok":
        raise AssertionError(reaction.to_text())
    sticker_result = core_tools.real_send_telegram_media(os.path.basename(gateway_sticker_fuzzy_match()), "Codex smoke test sticker")
    if sticker_result.status != "ok":
        raise AssertionError(sticker_result.to_text())
    return "live Telegram message, reaction, and media smoke passed"


def send_telegram_media_falls_back_to_text_after_upload_failure():
    class DummyResponse:
        def __init__(self, ok: bool, text: str = "", payload=None):
            self.ok = ok
            self.text = text
            self.status_code = 500 if not ok else 200
            self.headers = {"content-type": "application/json"}
            self._payload = payload if payload is not None else {"ok": ok}

        def json(self):
            return self._payload

    media_path = os.path.join(core_tools.PROJECT_CACHE_DIR, "media_fallback.png")
    with open(media_path, "wb") as file:
        file.write(b"fake png")
    old_token = core_tools.TG_TOKEN
    old_post = core_tools.requests.post
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        if "sendPhoto" in url or "sendAnimation" in url:
            raise ConnectionResetError(10054, "遠端主機已強制關閉一個現存的連線。")
        if "sendMessage" in url:
            return DummyResponse(True, payload={"ok": True, "result": {"message_id": 1}})
        return DummyResponse(False, "unexpected endpoint")

    try:
        core_tools.TG_TOKEN = "123456:fake-token-for-test"
        with open(core_tools.CHAT_ID_FILE, "w", encoding="utf-8") as file:
            file.write("42")
        core_tools.requests.post = fake_post
        result = core_tools.real_send_telegram_media(media_path, "fallback caption")
    finally:
        core_tools.TG_TOKEN = old_token
        core_tools.requests.post = old_post

    if result.status != "ok" or "text fallback" not in result.message:
        raise AssertionError(result.to_text())
    if not any("sendPhoto" in call["url"] for call in calls) or not any("sendMessage" in call["url"] for call in calls):
        raise AssertionError(calls)
    return result.to_text()


def main():
    if not acquire_self_test_lock():
        print(f"[LOCKED] self_test is already running. Lock file: {SELF_TEST_LOCK_FILE}")
        print("If this is stale after an interrupted run, remove the lock file or wait for the stale-lock timeout.")
        raise SystemExit(2)
    checks = [
        ("tool_schemas", validate_tool_schemas),
        ("protocol_constants_are_unicode_safe", protocol_constants_are_unicode_safe),
        ("write_file", lambda: result_text(core_tools.real_write_file("project_cache/self_test.txt", "hello self test"))),
        ("read_file", lambda: result_text(core_tools.real_read_file("project_cache/self_test.txt"))),
        ("list_files", lambda: result_text(core_tools.real_list_files("project_cache", False, 20))),
        ("search_in_files", lambda: result_text(core_tools.real_search_in_files("hello self test", "project_cache"))),
        ("read_webpage_bad_url", lambda: result_text(core_tools.real_read_webpage("not-a-url"))),
        ("download_file_bad_url", lambda: result_text(core_tools.real_download_file("not-a-url", "project_cache/nope.bin"))),
        ("delete_file_round_trip", delete_file_round_trip),
        ("execute_command_defaults_to_project_root", execute_command_defaults_to_project_root),
        ("execute_command_workspace_cwd_is_supported", execute_command_workspace_cwd_is_supported),
        ("execute_command_rejects_invalid_cwd", execute_command_rejects_invalid_cwd),
        ("execute_command_missing_file_has_retry_hint", execute_command_missing_file_has_retry_hint),
        ("gitignore_exists_for_private_runtime_files", gitignore_exists_for_private_runtime_files),
        ("git_index_excludes_private_runtime_files", git_index_excludes_private_runtime_files),
        ("launcher_script_has_single_instance_guard", launcher_script_has_single_instance_guard),
        ("launcher_docs_explain_restart", launcher_docs_explain_restart),
        ("self_test_lock_blocks_parallel_run_and_recovers_stale_lock", self_test_lock_blocks_parallel_run_and_recovers_stale_lock),
        ("create_plan", lambda: result_text(core_tools.real_create_plan("self test objective", ["step one", "step two"]))),
        ("update_plan", lambda: result_text(core_tools.real_update_plan(1, "完成", "self test ok"))),
        ("update_memory", lambda: result_text(core_tools.real_update_memory("self test memory entry"))),
        ("search_sticker", lambda: result_text(core_tools.real_search_sticker("laugh"))),
        ("execute_python", lambda: result_text(core_tools.real_execute_python('print("python self test ok")'))),
        ("analyze_media_missing_file", lambda: result_text(core_tools.real_analyze_media("project_cache/does_not_exist.png"))),
        ("send_telegram_media_missing_file", lambda: result_text(core_tools.real_send_telegram_media("project_cache/does_not_exist.png"))),
        ("send_telegram_media_falls_back_to_text_after_upload_failure", send_telegram_media_falls_back_to_text_after_upload_failure),
        ("react_to_message_missing_context", react_to_message_missing_context),
        ("agent_init_all_tools", init_agent),
        ("unknown_tool_fallback", unknown_tool_fallback),
        ("permission_followup_allows_exact_tool", permission_followup_allows_exact_tool),
        ("permission_replay_bypasses_chat_route_policy", permission_replay_bypasses_chat_route_policy),
        ("permission_replay_delivers_safe_artifact_after_success", permission_replay_delivers_safe_artifact_after_success),
        ("permission_replay_recovers_transient_artifact_delivery", permission_replay_recovers_transient_artifact_delivery),
        ("permission_replay_recovers_transient_error_before_reply", permission_replay_recovers_transient_error_before_reply),
        ("permission_replay_summarizes_media_status_instead_of_raw_stdout", permission_replay_summarizes_media_status_instead_of_raw_stdout),
        ("permission_replay_failed_python_enters_self_repair_loop", permission_replay_failed_python_enters_self_repair_loop),
        ("permission_replay_lives_in_controller_not_core_loop", permission_replay_lives_in_controller_not_core_loop),
        ("tool_loop_lives_in_controller_not_core_loop", tool_loop_lives_in_controller_not_core_loop),
        ("tool_runtime_services_are_outside_core_agent", tool_runtime_services_are_outside_core_agent),
        ("llm_adapter_lives_outside_core_agent", llm_adapter_lives_outside_core_agent),
        ("routed_llm_adapter_selects_fast_chat_and_strong_task_models", routed_llm_adapter_selects_fast_chat_and_strong_task_models),
        ("main_build_agent_uses_routed_llm_adapter", main_build_agent_uses_routed_llm_adapter),
        ("task_result_followup_uses_last_outcome_without_replanning", task_result_followup_uses_last_outcome_without_replanning),
        ("outcome_context_handles_result_followup_even_when_classified_chat", outcome_context_handles_result_followup_even_when_classified_chat),
        ("outcome_send_artifact_uses_stored_artifact_without_replanning", outcome_send_artifact_uses_stored_artifact_without_replanning),
        ("outcome_send_artifact_recovers_transient_error_without_replanning", outcome_send_artifact_recovers_transient_error_without_replanning),
        ("outcome_continue_retries_last_safe_failed_step_without_replanning", outcome_continue_retries_last_safe_failed_step_without_replanning),
        ("outcome_retry_records_result_on_original_failed_graph", outcome_retry_records_result_on_original_failed_graph),
        ("outcome_analyze_artifact_uses_stored_artifact_without_replanning", outcome_analyze_artifact_uses_stored_artifact_without_replanning),
        ("outcome_action_without_artifact_is_clear", outcome_action_without_artifact_is_clear),
        ("outcome_intent_helpers_are_deterministic_and_bounded", outcome_intent_helpers_are_deterministic_and_bounded),
        ("unicode_intent_routing_uses_real_chinese_not_mojibake", unicode_intent_routing_uses_real_chinese_not_mojibake),
        ("outcome_continue_starts_allowlisted_verifier_worker", outcome_continue_starts_allowlisted_verifier_worker),
        ("outcome_continue_rejects_non_allowlisted_verifier_plan", outcome_continue_rejects_non_allowlisted_verifier_plan),
        ("single_approval_does_not_allow_unrelated_tool", single_approval_does_not_allow_unrelated_tool),
        ("turn_approval_allows_tool_chain", turn_approval_allows_tool_chain),
        ("turn_approval_allows_computer_control_bundle", turn_approval_allows_computer_control_bundle),
        ("plain_approval_allows_computer_control_operation", plain_approval_allows_computer_control_operation),
        ("approval_restores_pending_from_task_graph_after_restart", approval_restores_pending_from_task_graph_after_restart),
        ("command_cwd_failure_recovers_inside_agent_loop", command_cwd_failure_recovers_inside_agent_loop),
        ("transient_tool_error_recovers_before_user_followup", transient_tool_error_recovers_before_user_followup),
        ("self_recovery_does_not_retry_unsafe_python", self_recovery_does_not_retry_unsafe_python),
        ("self_recovery_failed_retry_leaves_evidence_for_reply", self_recovery_failed_retry_leaves_evidence_for_reply),
        ("missing_mss_screenshot_recovery_uses_safe_fallback", missing_mss_screenshot_recovery_uses_safe_fallback),
        ("screenshot_runtime_error_recovery_uses_safe_fallback", screenshot_runtime_error_recovery_uses_safe_fallback),
        ("self_recovery_diagnoses_and_plans_known_errors", self_recovery_diagnoses_and_plans_known_errors),
        ("repair_planner_orders_safe_candidates", repair_planner_orders_safe_candidates),
        ("command_recovery_uses_file_probe_after_cwd_retry_fails", command_recovery_uses_file_probe_after_cwd_retry_fails),
        ("self_recovery_uses_second_candidate_when_first_fails", self_recovery_uses_second_candidate_when_first_fails),
        ("self_recovery_skips_already_attempted_candidate", self_recovery_skips_already_attempted_candidate),
        ("self_recovery_does_not_plan_unsafe_unknown_errors", self_recovery_does_not_plan_unsafe_unknown_errors),
        ("missing_mss_recovery_bypasses_chat_route_allowlist", missing_mss_recovery_bypasses_chat_route_allowlist),
        ("missing_module_recovery_is_narrow", missing_module_recovery_is_narrow),
        ("self_repair_prompt_includes_failed_deterministic_recovery", self_repair_prompt_includes_failed_deterministic_recovery),
        ("tool_loop_prompts_self_repair_before_user_followup", tool_loop_prompts_self_repair_before_user_followup),
        ("trace_log_records_tool_events", trace_log_records_tool_events),
        ("session_brain_plain_chat_stays_idle", session_brain_plain_chat_stays_idle),
        ("session_brain_task_enters_active_task", session_brain_task_enters_active_task),
        ("session_brain_blocked_tool_awaits_permission", session_brain_blocked_tool_awaits_permission),
        ("session_brain_approval_moves_to_validation", session_brain_approval_moves_to_validation),
        ("session_brain_cancel_returns_idle", session_brain_cancel_returns_idle),
        ("session_brain_trace_events_are_recorded", session_brain_trace_events_are_recorded),
        ("hook_pre_tool_can_block_and_before_reply_can_annotate", hook_pre_tool_can_block_and_before_reply_can_annotate),
        ("skills_registry_discovers_and_selects", skills_registry_discovers_and_selects),
        ("context_pack_includes_selected_skill_but_is_bounded", context_pack_includes_selected_skill_but_is_bounded),
        ("context_pack_writes_budget_report", context_pack_writes_budget_report),
        ("memory_compiler_includes_profile_and_personality", memory_compiler_includes_profile_and_personality),
        ("memory_compiler_modes_control_engineering_context", memory_compiler_modes_control_engineering_context),
        ("memory_health_detects_mojibake_without_current_leak", memory_health_detects_mojibake_without_current_leak),
        ("persona_health_report_flags_no_mojibake", persona_health_report_flags_no_mojibake),
        ("memory_update_quality_gate_allows_clean_and_rejects_broken", memory_update_quality_gate_allows_clean_and_rejects_broken),
        ("rolling_summary_stores_summary_not_full_history", rolling_summary_stores_summary_not_full_history),
        ("engineering_knowledge_search_is_bounded", engineering_knowledge_search_is_bounded),
        ("knowledge_index_builds_whitelisted_sources", knowledge_index_builds_whitelisted_sources),
        ("knowledge_index_excludes_private_sources", knowledge_index_excludes_private_sources),
        ("knowledge_search_finds_project_terms", knowledge_search_finds_project_terms),
        ("knowledge_search_unknown_returns_empty", knowledge_search_unknown_returns_empty),
        ("knowledge_read_chunk_returns_full_text", knowledge_read_chunk_returns_full_text),
        ("knowledge_manifest_stable_without_changes", knowledge_manifest_stable_without_changes),
        ("knowledge_tools_return_structured_results", knowledge_tools_return_structured_results),
        ("social_prompt_keeps_boundaries_quiet", social_prompt_keeps_boundaries_quiet),
        ("personality_prompt_is_core_not_template_card", personality_prompt_is_core_not_template_card),
        ("soul_persona_keeps_catgirl_without_legacy_rules", soul_persona_keeps_catgirl_without_legacy_rules),
        ("replay_harness_runs_cases", replay_harness_runs_cases),
        ("replay_harness_detailed_results_and_failures", replay_harness_detailed_results_and_failures),
        ("task_benchmark_runs_default_cases", task_benchmark_runs_default_cases),
        ("live_eval_reads_task_benchmark_report", live_eval_reads_task_benchmark_report),
        ("observability_summarizes_trace_health", observability_summarizes_trace_health),
        ("live_eval_handles_missing_trace", live_eval_handles_missing_trace),
        ("live_eval_gate_uses_current_session_window", live_eval_gate_uses_current_session_window),
        ("live_eval_ignores_self_test_sessions_for_gate", live_eval_ignores_self_test_sessions_for_gate),
        ("live_eval_ignores_benchmark_sessions_for_gate", live_eval_ignores_benchmark_sessions_for_gate),
        ("live_eval_ignores_artifact_history_test_sessions_for_gate", live_eval_ignores_artifact_history_test_sessions_for_gate),
        ("live_eval_repo_hygiene_allows_env_example", live_eval_repo_hygiene_allows_env_example),
        ("live_eval_summarizes_fake_trace_and_writes_report", live_eval_summarizes_fake_trace_and_writes_report),
        ("live_eval_writes_permission_health", live_eval_writes_permission_health),
        ("live_eval_reports_user_facing_source_health", live_eval_reports_user_facing_source_health),
        ("source_health_checks_runtime_voice_samples", source_health_checks_runtime_voice_samples),
        ("source_health_rejects_mojibake_required_phrases", source_health_rejects_mojibake_required_phrases),
        ("planner_uses_real_chinese_intent_markers", planner_uses_real_chinese_intent_markers),
        ("source_health_detects_bad_user_facing_text", source_health_detects_bad_user_facing_text),
        ("source_health_failure_blocks_next_stage_gate", source_health_failure_blocks_next_stage_gate),
        ("action_verification_checks_file_write_and_delete", action_verification_checks_file_write_and_delete),
        ("action_verification_preserves_recovery_evidence", action_verification_preserves_recovery_evidence),
        ("action_verification_preserves_failed_recovery_attempt", action_verification_preserves_failed_recovery_attempt),
        ("task_transaction_records_tool_result", task_transaction_records_tool_result),
        ("task_graph_creates_persists_and_summarizes_steps", task_graph_creates_persists_and_summarizes_steps),
        ("task_graph_records_recovery_evidence_on_step", task_graph_records_recovery_evidence_on_step),
        ("task_graph_records_failed_recovery_attempt_on_step", task_graph_records_failed_recovery_attempt_on_step),
        ("task_graph_recovery_summary_does_not_grant_permission", task_graph_recovery_summary_does_not_grant_permission),
        ("planner_creates_persistent_steps", planner_creates_persistent_steps),
        ("planner_selects_next_structured_step", planner_selects_next_structured_step),
        ("planner_reuses_active_graph_for_followup", planner_reuses_active_graph_for_followup),
        ("planner_force_new_task_does_not_reuse_stale_graph", planner_force_new_task_does_not_reuse_stale_graph),
        ("ui_planner_prefers_direct_window_targeting", ui_planner_prefers_direct_window_targeting),
        ("primary_message_extraction_ignores_short_context_for_tasks", primary_message_extraction_ignores_short_context_for_tasks),
        ("wrapped_bluetooth_task_plan_first_ignores_codex_context", wrapped_bluetooth_task_plan_first_ignores_codex_context),
        ("complex_ui_task_returns_plan_before_tools", complex_ui_task_returns_plan_before_tools),
        ("plan_approval_grants_first_computer_control_step", plan_approval_grants_first_computer_control_step),
        ("url_platform_classifies_douyin_and_common_sites", url_platform_classifies_douyin_and_common_sites),
        ("url_metadata_parses_open_graph", url_metadata_parses_open_graph),
        ("douyin_html_metadata_extracts_description_author_and_cover", douyin_html_metadata_extracts_description_author_and_cover),
        ("url_context_cache_reuses_metadata", url_context_cache_reuses_metadata),
        ("short_context_resolves_reference_to_recent_url", short_context_resolves_reference_to_recent_url),
        ("url_tools_return_structured_results", url_tools_return_structured_results),
        ("url_preview_uses_real_chinese_markers", url_preview_uses_real_chinese_markers),
        ("read_webpage_social_url_degrades_to_url_context", read_webpage_social_url_degrades_to_url_context),
        ("task_graph_updates_planned_step_with_tool_result", task_graph_updates_planned_step_with_tool_result),
        ("worker_result_assimilation_updates_task_graph_only_from_main_thread", worker_result_assimilation_updates_task_graph_only_from_main_thread),
        ("observe_needed_stays_awaiting_validation", observe_needed_stays_awaiting_validation),
        ("workflow_replay_records_blocked_graph", workflow_replay_records_blocked_graph),
        ("live_eval_counts_workflow_metrics", live_eval_counts_workflow_metrics),
        ("live_eval_counts_verified_waiting_workflow_as_healthy", live_eval_counts_verified_waiting_workflow_as_healthy),
        ("worker_queue_submits_and_runs_success_job", worker_queue_submits_and_runs_success_job),
        ("worker_records_failed_command_evidence", worker_records_failed_command_evidence),
        ("worker_timeout_is_structured", worker_timeout_is_structured),
        ("worker_rejects_unallowed_verifier_command", worker_rejects_unallowed_verifier_command),
        ("verifier_subagent_can_submit_background_job", verifier_subagent_can_submit_background_job),
        ("live_eval_counts_worker_metrics", live_eval_counts_worker_metrics),
        ("live_eval_counts_planner_context_subagent_and_assimilation", live_eval_counts_planner_context_subagent_and_assimilation),
        ("presence_shadow_mode_records_candidate_without_send", presence_shadow_mode_records_candidate_without_send),
        ("presence_cooldown_prevents_spam", presence_cooldown_prevents_spam),
        ("presence_suppresses_during_active_task", presence_suppresses_during_active_task),
        ("live_eval_reports_presence_health", live_eval_reports_presence_health),
        ("presence_quiet_hours_are_soft_when_owner_recently_active", presence_quiet_hours_are_soft_when_owner_recently_active),
        ("presence_stale_permission_does_not_block_forever", presence_stale_permission_does_not_block_forever),
        ("presence_notify_tick_sends_once_and_respects_daily_limit", presence_notify_tick_sends_once_and_respects_daily_limit),
        ("presence_shadow_tick_never_sends", presence_shadow_tick_never_sends),
        ("presence_composer_quality_message_sends", presence_composer_quality_message_sends),
        ("presence_composer_rejects_generic_checkin", presence_composer_rejects_generic_checkin),
        ("presence_composer_error_does_not_crash_or_send", presence_composer_error_does_not_crash_or_send),
        ("presence_default_quota_is_adaptive_not_tiny_daily_cap", presence_default_quota_is_adaptive_not_tiny_daily_cap),
        ("presence_long_silence_becomes_icebreak_opportunity", presence_long_silence_becomes_icebreak_opportunity),
        ("presence_debug_records_suppression_reason", presence_debug_records_suppression_reason),
        ("failure_replay_persists_minimal_case", failure_replay_persists_minimal_case),
        ("subagent_lite_returns_isolated_summary", subagent_lite_returns_isolated_summary),
        ("verifier_subagent_runs_safe_command", verifier_subagent_runs_safe_command),
        ("subagent_boundaries_reject_disallowed_tools_and_commands", subagent_boundaries_reject_disallowed_tools_and_commands),
        ("session_brain_verification_pass_clears_pending", session_brain_verification_pass_clears_pending),
        ("session_brain_verification_failure_keeps_validation", session_brain_verification_failure_keeps_validation),
        ("verification_planner_recommends_runtime_gates", verification_planner_recommends_runtime_gates),
        ("verification_planner_handles_docs_only", verification_planner_handles_docs_only),
        ("session_brain_validation_includes_plan_and_clears_it", session_brain_validation_includes_plan_and_clears_it),
        ("latency_policy_classifies_modes", latency_policy_classifies_modes),
        ("computer_action_text_routes_to_tool_task", computer_action_text_routes_to_tool_task),
        ("observe_only_media_status_does_not_require_plan_approval", observe_only_media_status_does_not_require_plan_approval),
        ("observe_only_media_status_overrides_stale_validation_state", observe_only_media_status_overrides_stale_validation_state),
        ("observe_only_media_status_starts_fresh_graph_over_stale_active_graph", observe_only_media_status_starts_fresh_graph_over_stale_active_graph),
        ("chat_policy_blocks_vision_tool", chat_policy_blocks_vision_tool),
        ("user_visible_tool_blocks_hide_internal_route_terms", user_visible_tool_blocks_hide_internal_route_terms),
        ("user_voice_strings_are_unicode_safe", user_voice_strings_are_unicode_safe),
        ("user_facing_source_files_are_unicode_safe", user_facing_source_files_are_unicode_safe),
        ("runtime_context_uses_owner_facing_labels", runtime_context_uses_owner_facing_labels),
        ("semantic_intent_upgrades_chat_policy_without_user_modes", semantic_intent_upgrades_chat_policy_without_user_modes),
        ("safe_verifier_command_runs_in_screen_observe_route", safe_verifier_command_runs_in_screen_observe_route),
        ("arbitrary_command_still_requires_permission", arbitrary_command_still_requires_permission),
        ("workspace_media_send_is_low_friction_but_external_media_is_guarded", workspace_media_send_is_low_friction_but_external_media_is_guarded),
        ("media_cache_hits_second_analysis", media_cache_hits_second_analysis),
        ("dynamic_media_skips_image_vision", dynamic_media_skips_image_vision),
        ("quick_ack_exists_for_slow_modes", quick_ack_exists_for_slow_modes),
        ("repeated_tool_call_stops_before_timeout", repeated_tool_call_stops_before_timeout),
        ("interleaved_ui_navigation_is_not_treated_as_repeated_loop", interleaved_ui_navigation_is_not_treated_as_repeated_loop),
        ("screen_observe_policy_blocks_unrelated_vision_tool", screen_observe_policy_blocks_unrelated_vision_tool),
        ("prompt_mode_routes_screen_observe_persona", prompt_mode_routes_screen_observe_persona),
        ("dsml_cleaner_handles_spaced_tags", dsml_cleaner_handles_spaced_tags),
        ("fail_safe_returns_without_retry", fail_safe_returns_without_retry),
        ("turn_coalesces_text_and_sticker", turn_coalesces_text_and_sticker),
        ("telegram_gateway_retries_transient_send_errors", telegram_gateway_retries_transient_send_errors),
        ("turn_coalesces_sticker_then_text_with_text_primary", turn_coalesces_sticker_then_text_with_text_primary),
        ("turn_sticker_only_is_social_sticker", turn_sticker_only_is_social_sticker),
        ("turn_explicit_vision_request_uses_vision_task", turn_explicit_vision_request_uses_vision_task),
        ("turn_debounce_default_is_55_seconds", turn_debounce_default_is_55_seconds),
        ("turn_debounce_env_override_is_used", turn_debounce_env_override_is_used),
        ("turn_debounce_invalid_env_falls_back", turn_debounce_invalid_env_falls_back),
        ("turn_coalescer_records_trace_events", turn_coalescer_records_trace_events),
        ("turn_parts_after_window_split_into_two_turns", turn_parts_after_window_split_into_two_turns),
        ("gateway_sticker_fuzzy_match", gateway_sticker_fuzzy_match),
        ("gateway_autonomous_sticker_send", gateway_autonomous_sticker_send),
        ("gateway_ascii_sticker_alias", gateway_ascii_sticker_alias),
        ("gateway_dedupes_screenshot_markers", gateway_dedupes_screenshot_markers),
        ("gateway_auto_attaches_social_sticker_for_battle_reply", gateway_auto_attaches_social_sticker_for_battle_reply),
        ("gateway_does_not_duplicate_existing_social_sticker", gateway_does_not_duplicate_existing_social_sticker),
        ("gateway_records_sent_sticker_in_social_session", gateway_records_sent_sticker_in_social_session),
        ("social_sticker_tag_inference", social_sticker_tag_inference),
        ("social_session_infers_modes", social_session_infers_modes),
        ("social_reply_policy_guides_social_rhythm", social_reply_policy_guides_social_rhythm),
        ("social_session_suggests_and_avoids_recent_stickers", social_session_suggests_and_avoids_recent_stickers),
        ("social_sticker_index_rebuild_and_choose", social_sticker_index_rebuild_and_choose),
        ("social_sticker_catalog_incoming", social_sticker_catalog_incoming),
        ("social_sticker_metadata_tags_and_dedup", social_sticker_metadata_tags_and_dedup),
        ("social_sticker_filters_mature_content", social_sticker_filters_mature_content),
        ("social_sticker_index_migrates_unsafe_old_entries", social_sticker_index_migrates_unsafe_old_entries),
        ("social_sticker_candidate_approval_copies_and_selects", social_sticker_candidate_approval_copies_and_selects),
        ("social_sticker_safe_affection_and_teasing_allowed", social_sticker_safe_affection_and_teasing_allowed),
        ("social_sticker_candidate_reject_blocks_selection", social_sticker_candidate_reject_blocks_selection),
        ("social_sticker_unsafe_candidate_cannot_be_approved", social_sticker_unsafe_candidate_cannot_be_approved),
        ("sticker_curation_command_payload_parses_quotes", sticker_curation_command_payload_parses_quotes),
        ("social_sticker_batch_approval_and_summary", social_sticker_batch_approval_and_summary),
        ("social_curation_reminder_is_low_noise", social_curation_reminder_is_low_noise),
        ("gateway_batch_approves_recent_sticker_candidates", gateway_batch_approves_recent_sticker_candidates),
        ("gateway_approves_latest_sticker_candidate", gateway_approves_latest_sticker_candidate),
        ("gateway_rejects_latest_sticker_candidate", gateway_rejects_latest_sticker_candidate),
        ("search_sticker_uses_social_index", search_sticker_uses_social_index),
        ("search_sticker_blocks_mature_query_results", search_sticker_blocks_mature_query_results),
        ("search_sticker_supports_safe_battle_query", search_sticker_supports_safe_battle_query),
        ("live_telegram_smoke", live_telegram_smoke),
    ]

    backup_task_plan()
    backup_memory()
    backup_session_brain()
    backup_reliability_state()
    try:
        passed = sum(1 for name, fn in checks if check(name, fn))
        failed = len(checks) - passed
        print(f"\nSUMMARY {passed} passed, {failed} failed")
        raise SystemExit(1 if failed else 0)
    finally:
        restore_task_plan()
        restore_memory()
        restore_session_brain()
        restore_reliability_state()
        cleanup_self_test_files()
        release_self_test_lock()


if __name__ == "__main__":
    main()

