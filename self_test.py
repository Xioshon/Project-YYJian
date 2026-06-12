import inspect
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))

import core_tools
import main as main_module
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
from agent_knowledge import (
    KNOWLEDGE_CHUNKS_FILE,
    KNOWLEDGE_INDEX_FILE,
    KNOWLEDGE_MANIFEST_FILE,
    read_knowledge,
    reindex_workspace,
    search_knowledge,
)
from agent_eval import EVAL_REPORT_FILE, PERMISSION_HEALTH_FILE, build_live_eval_report, check_repo_hygiene, write_eval_report
from agent_observability import summarize_trace
from agent_protocol import STICKER_MARKER_LABEL, classify_approval, screenshot_marker, sticker_marker, sticker_pattern
from agent_runtime_context import build_runtime_context, should_include_task_context
from agent_action_verification import verify_action
from agent_replay import FAILURE_REPLAY_FILE, ReplayCase, ReplayHarness, record_failure_replay
from agent_self_recovery import SelfRecoveryController
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
from agent_verification import DEFAULT_VERIFICATION_PLANNER
from agent_transactions import TASK_TRANSACTIONS_FILE, TaskTransactionManager
from agent_task_graph import TASK_GRAPHS_FILE, WORKFLOW_REPLAY_FILE, TaskGraphManager
from agent_worker import ALLOWED_VERIFIER_COMMANDS, WORKER_JOBS_FILE, WORKER_RESULTS_FILE, WorkerJob, WorkerQueue, VerifierWorker
from core_agent import CompanionAgent, SiliconFlowAdapter, TRACE_LOG_FILE, clean_assistant_output
import agent_llm
from agent_llm import RoutedLLMAdapter, infer_route_from_messages
from main import TelegramGateway, _dedupe_preserve_order, _prompt_mode_for_seed, _split_sticker_command_payload, build_system_prompt, find_sticker_file


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
_task_graphs_backup = None
_workflow_replay_backup = None
_worker_jobs_backup = None
_worker_results_backup = None
_context_budget_report_backup = None
_subagent_runs_backup = None


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
    if "可以嗎" not in first["content"]:
        raise AssertionError(first)
    if os.path.exists(target):
        raise AssertionError("file was written before permission")
    second = agent.chat("可以")
    if "write_file" not in second["content"]:
        raise AssertionError(second)
    if not os.path.exists(target):
        raise AssertionError("file was not written after permission")
    if agent.llm.calls != 2:
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


def permission_replay_bypasses_chat_route_policy():
    agent = CompanionAgent(PermissionReplayPythonAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "permission_python_route_test.json"))
    agent.interactive_mode = False
    for tool in core_tools.ALL_TOOLS:
        agent.add_tool(tool)
    first = agent.chat("run python", response_policy=response_policy_for(InteractionMode.TOOL_TASK))
    if "可以嗎" not in first["content"] and "requires approval" not in first["content"]:
        raise AssertionError(first)
    second = agent.chat("好", response_policy=response_policy_for(InteractionMode.CHAT))
    if "execute_python" not in second["content"] or "Python completed" not in second["content"]:
        raise AssertionError(second)
    if "approved python replay" not in second["content"]:
        raise AssertionError("permission replay did not surface stdout")
    if agent.llm.calls != 2:
        raise AssertionError(f"approval should replay without replanning; calls={agent.llm.calls}")
    return "approved pending python replay bypassed chat route policy"


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
    forbidden = ["llm.chat_with_tools", "tool_call_counts", "Repeated tool call stopped", "failsafe"]
    leaked = [marker for marker in forbidden if marker in chat_source]
    if leaked:
        raise AssertionError(f"tool loop leaked back into CompanionAgent.chat: {leaked}")
    required = ["llm.chat_with_tools", "tool_call_counts", "Repeated tool call stopped", "failsafe"]
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
    messages = [{"role": "user", "content": "hi\n\n[SessionBrain]\nstate\nturn_intent: task_continuation"}]
    if infer_route_from_messages(messages) != "task_continuation":
        raise AssertionError("turn_intent route not inferred")
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
    agent = CompanionAgent(NoReplanAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, f"{filename}.json"))
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
    if "發給你" not in result["content"] or "send_me_artifact.png" not in result["content"]:
        raise AssertionError(result)
    if agent.llm.calls:
        raise AssertionError("send artifact should not replan")
    return "stored artifact sent without replanning"


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
    if "沒有找到可用的產物" not in result["content"]:
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
    if "py_compile" not in result["content"] or "job:" not in result["content"]:
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
    if "目前沒有明確下一步" not in result["content"]:
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
    if agent.llm.calls != 2:
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
            return {"role": "assistant", "content": "需要權限，可以嗎？"}
        if self.calls == 3:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_write2", "name": "write_file", "arguments": {"filename": "project_cache/turn.txt", "content": "turn"}, "raw_arguments": '{"filename":"project_cache/turn.txt","content":"turn"}'},
                    {"id": "call_py", "name": "execute_python", "arguments": {"code": "print('turn ok')"}, "raw_arguments": json.dumps({"code": "print('turn ok')"})},
                ],
            }
        return {"role": "assistant", "content": "turn approval handled"}


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
    if result["content"] != "turn approval handled":
        raise AssertionError(result)
    if not os.path.exists(target):
        raise AssertionError("turn write_file did not execute")
    new_tool_messages = [m for m in agent.memory if m.get("role") == "tool"][before:]
    if not any('"status": "blocked"' in m.get("content", "") and "execute_python" in m.get("content", "") for m in new_tool_messages):
        raise AssertionError("turn bundle should not allow unrelated high-risk execute_python")
    return "turn approval allowed file bundle but blocked high-risk command"


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
    try:
        os.remove(SESSION_BRAIN_FILE)
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
    forbidden = ["[SessionBrain]", "[TaskGraph]", "turn_intent:"]
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
    required = ["[SessionBrain]", "[TaskGraph]", "turn_intent:"]
    missing = [item for item in required if item not in last_user]
    if missing:
        raise AssertionError(f"task context missing: {missing}\n{last_user}")
    if infer_route_from_messages(adapter.last_messages) == "chat":
        raise AssertionError("task turn should not route to chat model")
    return "task runtime context preserved"


def runtime_context_builder_policy_is_explicit():
    chat = build_runtime_context("hi", turn_intent="chat", session_summary="state", task_summary="task", include_task_context=False)
    task = build_runtime_context("fix", turn_intent="task", session_summary="state", task_summary="task", include_task_context=True)
    if "[SessionBrain]" in chat or "[TaskGraph]" in chat:
        raise AssertionError(chat)
    if "[SessionBrain]" not in task or "turn_intent: task" not in task:
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
    written = write_eval_report(report, report_path)
    if not os.path.exists(written):
        raise AssertionError("report was not written")
    with open(written, "r", encoding="utf-8") as file:
        loaded = json.load(file)
    if loaded["knowledge"]["empty_count"] != 1:
        raise AssertionError(loaded["knowledge"])
    return report.to_text().splitlines()[0]


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
    graph = manager.plan_steps(plan.objective, plan.step_names(), "self_test", 1, plan.planner_version)
    if not graph.steps or not graph.steps[0].planned or graph.planner_version != "planner_v1":
        raise AssertionError(graph)
    reloaded = TaskGraphManager(path)
    if "steps:" not in reloaded.summary() or "workflow:" not in reloaded.summary():
        raise AssertionError(reloaded.summary())
    return graph.steps[0].name


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
        "metadata": {"step_id": graph.steps[0].step_id},
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
    if workflow["success_rate"] != 0.5 or workflow["recovery_count"] != 1:
        raise AssertionError(workflow)
    if workflow["top_failure_steps"][0]["tool"] != "execute_command":
        raise AssertionError(workflow)
    if report.next_stage_gate["status"] != "pass" or not report.next_stage_gate["warnings"]:
        raise AssertionError(report.next_stage_gate)
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


def repeated_tool_call_stops_before_timeout():
    agent = CompanionAgent(RepeatToolAdapter(), "system self test", os.path.join(core_tools.HISTORY_DIR, "repeat_tool_test.json"))
    agent.add_tool(core_tools.AgentTool("fake_repeat", "fake repeat tool", lambda value=1: core_tools.ToolResult("ok", "repeat ok"), {"type": "object", "properties": {"value": {"type": "integer"}}}))
    result = agent.chat("repeat tool")
    if "重複" not in result["content"] or "重現" not in result["content"]:
        raise AssertionError(result)
    if _contains_internal_policy_leak(result["content"]):
        raise AssertionError(result)
    return result["content"]


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
    global _eval_report_backup
    global _task_graphs_backup, _workflow_replay_backup
    global _worker_jobs_backup, _worker_results_backup
    global _context_budget_report_backup, _subagent_runs_backup
    _transactions_backup = _read_optional_file(TASK_TRANSACTIONS_FILE)
    _failure_replay_backup = _read_optional_file(FAILURE_REPLAY_FILE)
    _rolling_summary_backup = _read_optional_file(ROLLING_SUMMARY_FILE)
    _memory_compiled_backup = _read_optional_file(MEMORY_COMPILED_FILE)
    _memory_health_backup = _read_optional_file(MEMORY_HEALTH_FILE)
    _knowledge_manifest_backup = _read_optional_file(KNOWLEDGE_MANIFEST_FILE)
    _knowledge_chunks_backup = _read_optional_file(KNOWLEDGE_CHUNKS_FILE)
    _knowledge_index_backup = _read_optional_file(KNOWLEDGE_INDEX_FILE)
    _eval_report_backup = _read_optional_file(EVAL_REPORT_FILE)
    _task_graphs_backup = _read_optional_file(TASK_GRAPHS_FILE)
    _workflow_replay_backup = _read_optional_file(WORKFLOW_REPLAY_FILE)
    _worker_jobs_backup = _read_optional_file(WORKER_JOBS_FILE)
    _worker_results_backup = _read_optional_file(WORKER_RESULTS_FILE)
    _context_budget_report_backup = _read_optional_file(CONTEXT_BUDGET_REPORT_FILE)
    _subagent_runs_backup = _read_optional_file(SUBAGENT_RUNS_FILE)


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
    _restore_optional_file(TASK_TRANSACTIONS_FILE, _transactions_backup)
    _restore_optional_file(FAILURE_REPLAY_FILE, _failure_replay_backup)
    _restore_optional_file(ROLLING_SUMMARY_FILE, _rolling_summary_backup)
    _restore_optional_file(MEMORY_COMPILED_FILE, _memory_compiled_backup)
    _restore_optional_file(MEMORY_HEALTH_FILE, _memory_health_backup)
    _restore_optional_file(KNOWLEDGE_MANIFEST_FILE, _knowledge_manifest_backup)
    _restore_optional_file(KNOWLEDGE_CHUNKS_FILE, _knowledge_chunks_backup)
    _restore_optional_file(KNOWLEDGE_INDEX_FILE, _knowledge_index_backup)
    _restore_optional_file(EVAL_REPORT_FILE, _eval_report_backup)
    _restore_optional_file(TASK_GRAPHS_FILE, _task_graphs_backup)
    _restore_optional_file(WORKFLOW_REPLAY_FILE, _workflow_replay_backup)
    _restore_optional_file(WORKER_JOBS_FILE, _worker_jobs_backup)
    _restore_optional_file(WORKER_RESULTS_FILE, _worker_results_backup)
    _restore_optional_file(CONTEXT_BUDGET_REPORT_FILE, _context_budget_report_backup)
    _restore_optional_file(SUBAGENT_RUNS_FILE, _subagent_runs_backup)


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
    for name in ("self_test.txt", "permission_test.txt", "wrong_tool.txt", "turn.txt", "delete_me_self_test.txt", "download_test.html", "dedupe_screen.png", "observability_trace_test.jsonl", "eval_trace_test.jsonl", "eval_missing_trace.jsonl", "eval_report_test.json", "workflow_eval_trace_test.jsonl", "worker_eval_trace_test.jsonl", "control_plane_eval_trace_test.jsonl", "worker_jobs_test.jsonl", "worker_results_test.jsonl", "worker_fail_jobs_test.jsonl", "worker_fail_results_test.jsonl", "worker_timeout_jobs_test.jsonl", "worker_timeout_results_test.jsonl", "worker_reject_jobs_test.jsonl", "worker_reject_results_test.jsonl", "worker_subagent_jobs_test.jsonl", "worker_subagent_results_test.jsonl", "continue_worker_jobs_test.jsonl", "continue_worker_results_test.jsonl", "continue_reject_jobs_test.jsonl", "continue_reject_results_test.jsonl", "action_verify.txt", "transaction_test.txt", "transaction_test.json", "task_graph_test.json", "task_graph_permission_test.json", "planner_graph_test.json", "planner_tool_graph_test.json", "worker_assim_graph_test.json", "observe_graph_test.json", "workflow_replay_graph_test.json", "workflow_replay_test.jsonl", "graph_file.txt", "failure_replay_test.jsonl"):
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
        ("permission_replay_lives_in_controller_not_core_loop", permission_replay_lives_in_controller_not_core_loop),
        ("tool_loop_lives_in_controller_not_core_loop", tool_loop_lives_in_controller_not_core_loop),
        ("tool_runtime_services_are_outside_core_agent", tool_runtime_services_are_outside_core_agent),
        ("llm_adapter_lives_outside_core_agent", llm_adapter_lives_outside_core_agent),
        ("routed_llm_adapter_selects_fast_chat_and_strong_task_models", routed_llm_adapter_selects_fast_chat_and_strong_task_models),
        ("main_build_agent_uses_routed_llm_adapter", main_build_agent_uses_routed_llm_adapter),
        ("task_result_followup_uses_last_outcome_without_replanning", task_result_followup_uses_last_outcome_without_replanning),
        ("outcome_send_artifact_uses_stored_artifact_without_replanning", outcome_send_artifact_uses_stored_artifact_without_replanning),
        ("outcome_analyze_artifact_uses_stored_artifact_without_replanning", outcome_analyze_artifact_uses_stored_artifact_without_replanning),
        ("outcome_action_without_artifact_is_clear", outcome_action_without_artifact_is_clear),
        ("outcome_intent_helpers_are_deterministic_and_bounded", outcome_intent_helpers_are_deterministic_and_bounded),
        ("outcome_continue_starts_allowlisted_verifier_worker", outcome_continue_starts_allowlisted_verifier_worker),
        ("outcome_continue_rejects_non_allowlisted_verifier_plan", outcome_continue_rejects_non_allowlisted_verifier_plan),
        ("single_approval_does_not_allow_unrelated_tool", single_approval_does_not_allow_unrelated_tool),
        ("turn_approval_allows_tool_chain", turn_approval_allows_tool_chain),
        ("command_cwd_failure_recovers_inside_agent_loop", command_cwd_failure_recovers_inside_agent_loop),
        ("transient_tool_error_recovers_before_user_followup", transient_tool_error_recovers_before_user_followup),
        ("self_recovery_does_not_retry_unsafe_python", self_recovery_does_not_retry_unsafe_python),
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
        ("observability_summarizes_trace_health", observability_summarizes_trace_health),
        ("live_eval_handles_missing_trace", live_eval_handles_missing_trace),
        ("live_eval_repo_hygiene_allows_env_example", live_eval_repo_hygiene_allows_env_example),
        ("live_eval_summarizes_fake_trace_and_writes_report", live_eval_summarizes_fake_trace_and_writes_report),
        ("live_eval_writes_permission_health", live_eval_writes_permission_health),
        ("action_verification_checks_file_write_and_delete", action_verification_checks_file_write_and_delete),
        ("task_transaction_records_tool_result", task_transaction_records_tool_result),
        ("task_graph_creates_persists_and_summarizes_steps", task_graph_creates_persists_and_summarizes_steps),
        ("task_graph_recovery_summary_does_not_grant_permission", task_graph_recovery_summary_does_not_grant_permission),
        ("planner_creates_persistent_steps", planner_creates_persistent_steps),
        ("task_graph_updates_planned_step_with_tool_result", task_graph_updates_planned_step_with_tool_result),
        ("worker_result_assimilation_updates_task_graph_only_from_main_thread", worker_result_assimilation_updates_task_graph_only_from_main_thread),
        ("observe_needed_stays_awaiting_validation", observe_needed_stays_awaiting_validation),
        ("workflow_replay_records_blocked_graph", workflow_replay_records_blocked_graph),
        ("live_eval_counts_workflow_metrics", live_eval_counts_workflow_metrics),
        ("worker_queue_submits_and_runs_success_job", worker_queue_submits_and_runs_success_job),
        ("worker_records_failed_command_evidence", worker_records_failed_command_evidence),
        ("worker_timeout_is_structured", worker_timeout_is_structured),
        ("worker_rejects_unallowed_verifier_command", worker_rejects_unallowed_verifier_command),
        ("verifier_subagent_can_submit_background_job", verifier_subagent_can_submit_background_job),
        ("live_eval_counts_worker_metrics", live_eval_counts_worker_metrics),
        ("live_eval_counts_planner_context_subagent_and_assimilation", live_eval_counts_planner_context_subagent_and_assimilation),
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
        ("chat_policy_blocks_vision_tool", chat_policy_blocks_vision_tool),
        ("user_visible_tool_blocks_hide_internal_route_terms", user_visible_tool_blocks_hide_internal_route_terms),
        ("semantic_intent_upgrades_chat_policy_without_user_modes", semantic_intent_upgrades_chat_policy_without_user_modes),
        ("safe_verifier_command_runs_in_screen_observe_route", safe_verifier_command_runs_in_screen_observe_route),
        ("arbitrary_command_still_requires_permission", arbitrary_command_still_requires_permission),
        ("workspace_media_send_is_low_friction_but_external_media_is_guarded", workspace_media_send_is_low_friction_but_external_media_is_guarded),
        ("media_cache_hits_second_analysis", media_cache_hits_second_analysis),
        ("dynamic_media_skips_image_vision", dynamic_media_skips_image_vision),
        ("quick_ack_exists_for_slow_modes", quick_ack_exists_for_slow_modes),
        ("repeated_tool_call_stops_before_timeout", repeated_tool_call_stops_before_timeout),
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


if __name__ == "__main__":
    main()

