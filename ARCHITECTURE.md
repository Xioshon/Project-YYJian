# YueYue Agent Architecture

This project should be treated as a small agent runtime, not as a pile of prompt tricks.

## Design Principles

1. Explicit state beats inferred dialogue.
   Permission, Telegram context, tool results, and failures must be stored as data.

2. Tools are capabilities, not conversation.
   Every tool has a schema, a confirmation policy, and a structured `ToolResult`.

3. The gateway renders replies.
   Telegram text, stickers, screenshots, and reactions are transport concerns. The model may choose the Chinese sticker marker or `[sticker: filename]`, but the gateway sends it.

4. Real risk needs scoped approval.
   Low-risk local/read-only tools stay convenient. Single approval retries only the exact pending tool call, and turn approval is bundle-scoped for destructive, privacy-sensitive, or system-control actions.

5. Failures must be inspectable.
   Runtime events are appended to `workspace/project_cache/agent_trace.jsonl`.

6. Harness before feature sprawl.
   Protocols, hooks, skills, replay cases, and verification gates are the control plane that keeps the agent reliable.

## Runtime Layers

- `SiliconFlowAdapter`
  Converts local message/tool objects into OpenAI-compatible SiliconFlow requests.

- `CompanionAgent`
  Owns conversation memory, model loop, session-brain updates, tool-call handling, history saving, and fail-safe termination.

- `SessionBrain`
  Tracks whether the agent is idle, doing an active task, waiting for permission, waiting for validation, or blocked. It summarizes current task state into context without bypassing permission rules.

- `PermissionManager`
  Tracks pending approvals, exact replay actions, and bundle-scoped turn grants. Risk-tiering keeps low-risk local tools free while preserving approval for destructive or privacy-sensitive actions.

- `ToolRegistry`
  Stores the registered tools and exposes stable names to the model.

- `ToolExecutor`
  Applies confirmation policy, invokes tools, catches exceptions, emits trace events, and normalizes results.

- `ExecutionRecovery`
  Lets the agent recover from clear cwd-related `execute_command` failures by retrying once from the project root and recording recovery events.

- `TaskTransactionManager`
  Stores local JSON task transactions with objective, current step, created files, cleanup intent, tool results, and verification status.

- `TaskGraphManager`
  Stores durable workflow graphs with objective, planned steps, step state, current step, created files, cleanup intent, worker evidence, and verification state. It can summarize unfinished work after restart without granting any tool permission.

- `PlannerV1`
  Creates conservative persistent steps for non-chat tasks before tools run. It is deterministic in this phase so the runtime can replay and test task state without depending on a planner model.

- `ActionVerification`
  Verifies deterministic tool postconditions after execution and marks UI actions as needing observation rather than assuming success.

- `TelegramGateway`
  Handles Telegram updates, message context, idempotent reply rendering, sticker sending, screenshot marker dedupe, and low-noise tool status updates.

- `agent_protocol`
  Owns approval phrases, reply markers, status labels, and fail-safe text with Unicode-safe constants.

- `HookManager`
  Emits lifecycle events, writes trace JSONL, and lets hooks allow, block, annotate, or transform runtime actions.

- `SkillRegistry`
  Discovers `workspace/skills/*/SKILL.md`, selects relevant procedures, and keeps long instructions out of context until needed.

- `ContextPackBuilder`
  Builds bounded startup context from compiled memory, selected skills, and task-only context. It writes `context_budget_report.json` and preserves high-priority memory sections instead of blindly stuffing every memory file into every prompt.

- `MemoryCompiler`
  Compiles personality, rules, owner profile, long-term memory, rolling chat summary, SessionBrain, and bounded engineering context into mode-specific prompt memory. Persona mode is explicit for chat, social sticker, task, vision, and screen observation turns.

- `MemoryHealth`
  Checks missing files, mojibake-like text, oversized sections, and persona warnings. Health output is written to `workspace/project_cache/memory_health.json`; persona-specific health is written to `workspace/project_cache/persona_health.json`.

- `PermissionHealth`
  Written by `agent_eval.py` to `workspace/project_cache/permission_health.json`. It lists free tools, guarded tools, bundles, and safe verifier command patterns so the permission model stays observable.

- `ReplayHarness`
  Runs deterministic regression scenarios for historical bugs and emits structured replay results.

- `FailureReplay`
  Converts repeated tool failures into minimal JSONL replay cases so failure loops become future regression tests.

- `WorkflowReplay`
  Converts blocked durable workflows into minimal replay cases with task id, failed step, tool, arguments, and result evidence.

- `SubagentLite`
  Provides isolated Explorer, Verifier, and Reviewer role interfaces. Explorer and Reviewer are read-only. Verifier can run only allowlisted local verification commands synchronously or submit background verifier jobs.

- `agent_worker.py`
  Runs allowlisted verifier checks in background threads and writes job/result JSONL evidence. Workers do not mutate permission, TaskGraph, memory, or Telegram directly; the main agent assimilates completed worker results back into TaskGraph steps.

- `agent_latency`
  Classifies interactions as `chat`, `social_sticker`, `vision_task`, `screen_observe`, or `tool_task`, applies route-specific tool budgets, and caches media analysis.

- `VerificationPlanner`
  Chooses conservative deterministic verification commands from objective, changed files, and evidence. SessionBrain stores the selected plan while validation is pending.

- `TraceSummary` / `agent_observability.py`
  Reads `agent_trace.jsonl` and produces a compact health snapshot with event counts, tool success rate, permission replay metrics, bundle metrics, action verification status, repeated-failure replay counts, interaction modes, latency buckets, social events, and recent errors.

- `agent_eval.py`
  Builds the live evaluation gate from trace events: tool success/failure, permission replay success, permission policy health, planner coverage, workflow success, observe-needed counts, worker health, worker assimilation, subagent health, persona health, render dedupe, context budget, recovery count, repeated failures, latency buckets, knowledge hit rate, recent errors, Telegram activity, and Git hygiene.

- `SocialStickerIndex`
  Builds a local emotion index for stickers, catalogs incoming Telegram stickers as metadata, and supports deterministic sticker selection before LLM fallback.
  Incoming candidates store Telegram emoji, sticker set name, file unique id, media type, and content hash for tagging and duplicate detection. They remain unapproved until curated.
  Candidate curation supports single-item and recent-batch approve/reject operations so Telegram commands and future UI surfaces share the same behavior.
- `SocialCurationReminder`
  Provides throttled in-memory reminders when pending sticker candidates accumulate, keeping curation discoverable without turning every sticker into a separate notification.
- `SocialSessionManager`
  Keeps short-lived per-chat social rhythm such as sticker battle, affection, or teasing. This is in-memory runtime state only; it does not update profile, memory, or task plans.
  TelegramGateway may attach one suggested local sticker for clear sticker-battle turns when the model did not already emit a sticker marker.
- `SocialReplyPolicy`
  Converts short-lived social rhythm into concise reply guidance: sticker battle stays playful and quick, affection stays soft, teasing stays warm, and social turns avoid tool use.

## Latency Policy

- Plain chat and social stickers use a quiet policy: no proactive vision, at most one tool attempt plus one final response.
- Stickers are treated as social/emotional signals unless the owner explicitly asks for analysis.
- Photos only trigger vision when the caption/message asks to look, analyze, identify, or describe.
- Dynamic sticker/video formats are recorded as media metadata and are not sent to image-only vision.
- Vision results are cached by file hash and summarized before returning to the main agent loop.
- Slow vision/tool tasks may send a quick acknowledgement before the heavy work completes.
- Sticker battles and mood stickers use local sticker search/indexing first; incoming stickers are cataloged as social metadata and are not analyzed unless requested.
- Sticker auto-selection uses quiet eligibility checks and keeps incoming stickers as unapproved candidates until curated. Affection/teasing is allowed, and intense wording should be handled by warm pivoting rather than abrupt interruption.
- SocialSession prompt notes may provide candidate sticker filenames and recent rhythm, but must remain separate from durable memory and SessionBrain task state.

## Memory Policy

- Personality is stable SOUL behavior guidance: YueYue is Xioshon's cyber catgirl, not a generic assistant.
- `profile.json` stores stable owner facts and preferences.
- `memory.md` stores sparse long-term notes only when explicitly updated.
- `chat_summary/rolling_summary.md` stores compact recent summaries, not full transcripts.
- Chat mode uses personality/profile/summary; task mode adds SessionBrain and engineering context; social sticker mode avoids engineering docs. Chat should feel like a cyber catgirl with 喵~ / kaomoji flavor; task mode should stay reliable without turning cold.
- Full chat history remains out of default prompt and out of the lightweight knowledge layer.

## Repository Policy

- Source code, docs, safe examples, and curated non-private assets belong in Git.
- Runtime logs, traces, chat history, Telegram chat ids, downloaded Telegram media, screenshots, project cache, pycache, and `.env` stay local.
- Private repository status is useful but not a substitute for `.gitignore` and clean tracking.

## Session State Policy

- Plain chat stays `idle` and should not create or mutate a task plan.
- Clear task intent enters `active_task`.
- A protected tool block enters `awaiting_permission`; owner approvals are still handled only by `PermissionManager`.
- Successful tool work enters `awaiting_validation` so the model can report what should be checked next.
- Repeated tool failures can enter `blocked` and must be surfaced through trace events and clear replies.
- Owner cancellation phrases such as "stop/cancel/算了" return the brain to `idle`.
- Verification success clears pending validation and returns to `idle`; verification failure keeps validation pending or becomes `blocked` after repeated failures.
- Runtime/Python changes should recommend `py_compile` plus `python self_test.py`; docs-only changes may mark self-test optional.

## Permission Contract

- Chinese single-approval phrases such as "can/ok/approve/agree" plus `ok`, `yes`
  Approve the previously blocked exact tool call once. The runtime replays the saved pending action directly instead of asking the model to generate a new tool call.

- Chinese turn-approval phrases such as "allow this turn / all ok this time / full authority" plus `global allow`, `allow all`
  Approve protected tools for the current task turn, limited to the relevant bundle inferred from the pending action.

- Current bundles:
  `computer_control_bundle`, `file_workspace_bundle`, `telegram_media_bundle`, and `screenshot_bundle`.

- Free low-risk tools include local read/search, screen observation, knowledge search, sticker search, media analysis, message reactions, and memory/profile updates with quality checks.

- Safe verifier commands such as `python -m py_compile ...`, `python self_test.py`, `python agent_eval.py`, and `python agent_observability.py` may run without extra approval.

- High-risk tools:
  Arbitrary `execute_command`, arbitrary `execute_python`, `execute_async_command`, destructive file operations, external downloads, external-path media sending, and UI control require approval and are not smuggled through ordinary bundles.

- Any unrelated protected tool after a single approval is blocked again.

## Reply Markers

Preferred:

- Chinese sticker marker: `[\u8868\u60c5\u5305: filename]`
- Chinese screenshot marker: `[\u7cfb\u7d71\u622a\u5716: filename]`

ASCII fallbacks:

- `[sticker: filename]`
- `[screenshot: filename]`

## Regression Gates

Before trusting a change:

```powershell
cd C:\Agent
python -m py_compile core_tools.py core_agent.py main.py self_test.py agent_turns.py agent_session.py agent_context.py agent_memory.py agent_knowledge.py agent_eval.py
python -m py_compile agent_task_graph.py
python self_test.py
python agent_eval.py
```

The default suite covers protocol encoding, scoped approval, permission bundles, task transactions, action verification, repeated-failure replay, observability metrics, memory compiler, personality core, session brain, hooks, skills, context pack, replay harness, subagent-lite, Telegram fake gateway, and tool execution.
It also covers latency routing, media cache hits, dynamic media skipping, and response policy blocking for unwanted vision calls.

Optional live Telegram smoke:

```powershell
$env:RUN_LIVE_TELEGRAM_SMOKE='1'
python self_test.py
```

The live smoke sends one message, reacts to it, and sends one local sticker/media file.

## What Not To Do

- Do not add new tool names without adding schema tests.
- Do not parse approval by reading the last assistant message.
- Do not let model-chosen stickers go through `send_telegram_media` approval.
- Do not silently retry code with another LLM inside `execute_python`.
- Do not hide tool failures in natural-language replies only; preserve structured tool results and trace events.
- Do not add a workflow directly into the model prompt when it belongs in a skill.
- Do not bypass hooks when adding new tool execution paths.
