# YueYue Agent Runbook

## What Changed

- The runtime is now split into clear layers: `ToolRegistry`, `ToolExecutor`, `PermissionManager`, `CompanionAgent`, and `TelegramGateway`.
- The architecture contract is documented in `C:\Agent\ARCHITECTURE.md`.
- Harness Phase 1 adds protocol constants, hooks, skills, bounded context packs, replay evals, and subagent-lite interfaces.
- Latency optimization adds interaction modes, response policies, quick acknowledgements, and media analysis cache.
- Message coalescing groups quick Telegram text/photo/sticker bursts into one human turn before the agent replies.
- SessionBrain records whether YueYue is chatting, doing a task, waiting for permission, waiting for validation, or blocked.
- Social sticker indexing maps local stickers to coarse emotions and catalogs incoming stickers as metadata for future social behavior.
- Reliability Phase adds permission bundles, task transactions, action verification, repeated-failure replay cases, and live evaluation metrics.
- Durable Workflow adds task graphs, workflow replay cases, and workflow metrics in the live evaluation gate.
- Hybrid Worker adds a background verifier queue while keeping the main agent thread as the only decision/state writer.
- Mainstream Gap Closure adds deterministic Planner v1, worker-result assimilation, observe-needed workflow state, context budget reporting, stricter subagent boundaries, and expanded live evaluation metrics.
- Memory + Personality Core adds compiled memory, memory health checks, rolling chat summary, and the SOUL cyber-catgirl personality core.
- Phase 5 restores YueYue's SOUL direction, adds persona health reporting, routes screen-observe tasks through a short observe-once flow, and dedupes Telegram render artifacts.
- Repo hygiene and execution recovery keep private runtime files out of Git and let cwd-related command failures retry once from project root.
- LLM routing separates the runtime from the provider adapter and can send casual chat/social turns to a fast model while keeping task, tool, and vision turns on the stronger model.
- Tools return structured `ToolResult` data instead of loose strings.
- Approval is explicit state, not guessed from the last assistant message.
- Telegram sticker rendering through `[表情包: filename]` or `[sticker: filename]` is autonomous and does not require tool approval.
- Explicit `send_telegram_media` still requires approval because it can transmit arbitrary local files.
- `react_to_message` is available and uses the current Telegram message context by default.

## Approval Rules

- `可以`, `好`, `允許`, `同意`, `ok`, `yes` approve only the previously blocked exact tool call one time.
- `本輪允許`, `這次全部可以`, `全部可以`, `全權交給你`, `allow all` approve protected tools for the current task turn.
- If the model tries an unrelated protected tool after a single approval, it is blocked again.
- YueYue uses risk-tiered permission. Low-risk local/read-only tools should stay convenient; destructive, exfiltrating, or system-control actions stay guarded.
- Low-risk free tools include local read/search, screen observation, knowledge search, sticker search, media analysis, message reaction, and memory/profile updates with quality checks.
- Safe verifier commands such as `python -m py_compile ...`, `python self_test.py`, `python agent_eval.py`, and `python agent_observability.py` may run without extra approval.
- Workspace-local generated media, screenshots, stickers, and Telegram media cache files may be sent through `send_telegram_media` without extra approval. External absolute paths or suspicious private files still require approval.
- Turn approval is bundle-scoped:
  - `computer_control_bundle`: `click_ui_element`, `type_keyboard`, `press_hotkey`
  - `file_workspace_bundle`: `write_file`, `delete_file`, `download_file`
  - `telegram_media_bundle`: `send_telegram_media`
  - `screenshot_bundle`: `get_screen_ui`, `send_telegram_media`, `delete_file`
- High-risk tools such as arbitrary `execute_command`, arbitrary `execute_python`, and `execute_async_command` are not included in ordinary bundles.

## Debug Trace

## Model Routing

`agent_llm.RoutedLLMAdapter` is the default adapter used by `main.build_agent()`.

Optional `.env` values:

```env
YUEYUE_CHAT_MODEL=
YUEYUE_STRONG_MODEL=deepseek-ai/DeepSeek-V4-Pro
YUEYUE_TASK_MODEL=
YUEYUE_VISION_MODEL=
```

Leave `YUEYUE_CHAT_MODEL` blank to use the strong model for every route. Set it when ordinary chat feels too slow.

## Debug Trace

Runtime events are written as JSON lines to:

`C:\Agent\workspace\project_cache\agent_trace.jsonl`

Important event names:

- `SessionStart`
- `UserMessage`
- `PermissionRequest`
- `PermissionGranted`
- `PermissionConsumed`
- `PermissionReplay`
- `PermissionReplayResult`
- `PermissionBundleGranted`
- `PermissionBundleConsumed`
- `PermissionBundleDenied`
- `ActionVerification`
- `planner.result`
- `planner.plan_created`
- `planner.plan_reused`
- `task_transaction.started`
- `task_transaction.step_recorded`
- `workflow.step_recorded`
- `workflow.completed`
- `workflow.blocked`
- `worker.result_assimilated`
- `context.budget`
- `subagent.run`
- `FailureReplayCreated`
- `llm.response`
- `tool.blocked`
- `tool.start`
- `tool.end`
- `PostToolUse`
- `ToolError`
- `BeforeReply`
- `Stop`
- `StopFailure`
- `turn.part`
- `turn.flush`
- `turn.config_warning`
- `session_brain.classified`
- `session_brain.state_changed`
- `session_brain.validation_needed`
- `session_brain.blocked`
- `session_brain.verification_result`
- `replay.case`
- `subagent.verifier`
- `render.dedupe`
- `ToolSkippedByPolicy`

Session state is stored at:

`C:\Agent\workspace\project_cache\session_brain.json`

Task transaction state is stored at:

`C:\Agent\workspace\project_cache\task_transactions.json`

Durable workflow graph state is stored at:

`C:\Agent\workspace\project_cache\task_graphs.json`

Generated repeated-failure replay cases are appended to:

`C:\Agent\workspace\project_cache\failure_replay_cases.jsonl`

Generated blocked-workflow replay cases are appended to:

`C:\Agent\workspace\project_cache\workflow_replay_cases.jsonl`

Background verifier jobs and results are appended to:

`C:\Agent\workspace\project_cache\worker_jobs.jsonl`
`C:\Agent\workspace\project_cache\worker_results.jsonl`

Context budget and subagent run summaries are written to:

`C:\Agent\workspace\project_cache\context_budget_report.json`
`C:\Agent\workspace\project_cache\subagent_runs.jsonl`

## Skills

Built-in skills are materialized under:

`C:\Agent\workspace\skills`

Current built-ins:

`debug`, `vision`, `telegram`, `safe-computer-use`, `code-review-lite`.

The context builder loads only selected skills for a task instead of stuffing every procedure into every prompt.

## Memory + Personality Core

- `MemoryCompiler` is the single path for personality, profile, memory, chat summary, and SessionBrain context.
- Compiled memory is written to `C:\Agent\workspace\project_cache\memory_compiled.json`.
- Memory health is written to `C:\Agent\workspace\project_cache\memory_health.json`.
- Persona health is written to `C:\Agent\workspace\project_cache\persona_health.json`.
- Permission policy health is written to `C:\Agent\workspace\project_cache\permission_health.json` by `agent_eval.py`.
- Rolling chat summary is stored at `C:\Agent\workspace\memory\chat_summary\rolling_summary.md`.
- Personality style samples are stored at `C:\Agent\workspace\brain\personality_samples.md`.
- Chat mode injects personality, owner profile, preferences, and rolling summary.
- Task/tool/vision/screen-observe modes additionally inject SessionBrain and bounded engineering context.
- Social sticker mode does not inject engineering docs by default.
- Persona mode is explicit: chat/social turns keep vivid cyber-catgirl flavor with 喵~ and kaomoji; task turns stay reliable without becoming cold; screen-observe turns are warm, short, and practical.
- Full `workspace\chat_history` logs are not indexed or stuffed into prompt by default.
- Personality is written as SOUL behavior guidance, not a generic assistant profile. YueYue should feel like Xioshon's cyber catgirl, while the runtime still handles permission and safety.

## Screen Observe Route

- Requests such as "截圖", "看螢幕", "幫我看看現在畫面" are classified as `screen_observe`.
- `screen_observe` uses a short observe-first route instead of the normal long tool loop.
- The route allows screen observation plus safe local verification commands, blocks unrelated vision/arbitrary command tools, and stops repeated same-tool/same-argument retries early.
- Tool-loop timeout no longer sends extra screenshots; it reports one clear stop message and records replay evidence.
- Telegram rendering is idempotent for screenshot and sticker markers. If the model repeats the same screenshot marker in one reply, the gateway sends it once and records `render.dedupe`.

## Latency Behavior

- Plain text chat should answer without proactive media analysis.
- Telegram stickers are treated as social signals by default.
- Image/sticker analysis runs only when the owner clearly asks to inspect or identify the media.
- Repeated image analysis uses `C:\Agent\workspace\project_cache\media_cache.json`.
- Dynamic stickers such as `.webm` and `.tgs` are recorded as dynamic media and are not passed into image-only vision.

## Turn Coalescing

- YueYue waits briefly before replying so a text message plus follow-up sticker/photo can be treated as one human turn.
- The default window is `5.5` seconds.
- Text is the primary intent. Stickers/photos inside the same window are treated as emotional or contextual supplements unless the owner explicitly asks to inspect the media.
- Configure the window with `.env` or environment variables:

```powershell
YUEYUE_TURN_DEBOUNCE_SECONDS=5.5
```

Common values:

- `3` for faster replies with less grouping.
- `5.5` for the current balanced default.
- `8` for slower but more forgiving multi-message grouping.

Invalid or non-positive values fall back to `5.5` and write a `turn.config_warning` trace event.

## Social Stickers

- Local sticker selection first checks `C:\Agent\workspace\assets\social_sticker_index.json`.
- The index maps filenames to coarse safe tags such as `happy`, `confused`, `angry`, `cute`, `cry`, `battle`, `agree`, and `affection`.
- `SocialSessionManager` keeps a short in-memory social rhythm per chat, such as sticker battle, affection, or teasing. It expires automatically and is not written to long-term memory.
- SocialSession may add a small prompt note with recent rhythm and good local sticker candidates, so YueYue can continue a meme/sticker exchange without using slow tools.
- `SocialReplyPolicy` keeps social turns short and natural: sticker battle is playful and quick, affection is soft and concise, teasing stays warm rather than mean. These modes do not use tools.
- In clear sticker-battle turns, TelegramGateway can attach one suggested local sticker when the model reply did not already include a sticker marker. This makes sticker battles responsive without adding slow tool calls.
- Incoming Telegram sticker candidates keep lightweight metadata such as emoji, sticker set name, file unique id, media type, and content hash. This improves emotion tags and prevents duplicate candidates, but still does not auto-approve the sticker for YueYue to use.
- YueYue may send a low-noise curation reminder when pending candidates reach 3 or more. The reminder is throttled with a cooldown so sticker intake does not spam the chat.
- Sticker auto-selection uses quiet eligibility checks so mood replies stay suitable without interrupting chat.
- Affection is allowed for cute, shy, heart, clingy, or gentle teasing stickers.
- If wording gets intense, YueYue should keep a warm tone and pivot inside the relationship rather than abruptly stop the conversation.
- Incoming Telegram stickers are cataloged as metadata only. They are not copied into the local sticker asset library and are not analyzed unless the owner asks.
- Incoming stickers are not approved for automatic use until explicitly curated later.
- `search_sticker` uses the social index before filename matching or LLM fallback.
- Telegram text commands for curation:
  - `list sticker candidates`
  - `approve latest sticker cute affection`
  - `reject latest sticker`
  - `approve recent 3 stickers cute affection`
  - `reject recent 3 stickers`
  - `列出貼圖候選`
  - `批准貼圖 "filename.webp" cute affection`
  - `拒絕貼圖 "filename.webp"`

## Session Brain

- `idle`: ordinary chat; no task is active.
- `active_task`: YueYue believes the owner is asking for work, debugging, testing, or implementation.
- `awaiting_permission`: a protected tool was blocked and is waiting for owner approval.
- `awaiting_validation`: tools ran successfully and results should be checked or reported.
- `blocked`: repeated tool failures need a clear explanation before continuing.

The brain is advisory state. It does not bypass `PermissionManager`; approvals still require the existing exact-tool or turn-scope rules.

Single-action approval replay:

- When a protected tool is blocked, `PermissionManager` stores the full pending action: tool name, arguments, and timestamp.
- If the owner replies with a single approval such as `可以` or `ok`, the runtime executes that saved pending action directly.
- The model is not asked to regenerate the tool call, which prevents approval from drifting into a different tool or different arguments.

State separation:

- Personality/profile files define YueYue's stable voice and owner preferences.
- `memory.md` stores durable memories only when explicitly updated through the memory tool.
- `SessionBrain` stores task state such as active work, pending permission, validation, and blockers.
- `SocialSessionManager` stores only short-lived chat rhythm and recent sticker use; it does not bind or rewrite personality.

## Verification Layer

- `verify_action()` checks deterministic postconditions after tool execution: created files exist, deleted files are absent, commands return cleanly, Telegram media calls return ok, and UI actions are marked as observe-needed instead of blindly successful.
- `TaskTransactionManager` records task id, objective, current step, tool results, created files, cleanup intent, and verification status in local JSON.
- `TaskGraphManager` records durable workflow graphs with step status, current step, created files, cleanup intent, and verification details.
- Workflow summaries are injected into task context, but restoring a workflow never grants permission or auto-runs protected tools.
- If the same tool fails repeatedly, the runtime stops the loop and writes a minimal replay case instead of continuing to spin.
- Blocked workflows can write a minimal workflow replay case with task id, failed step, tool, arguments, and result evidence.
- `ReplayHarness.run_detailed()` returns structured replay results with status, message, expected events, and duration.
- `ReplayHarness.summary()` reports total, passed, failed, results, and failures.
- `Verifier` subagent can run a bounded local command and returns command evidence, return code, stdout, stderr, status, and duration.
- `Verifier` can also submit background verifier jobs. Workers run only allowlisted checks, save evidence, and never directly mutate SessionBrain, TaskGraph, PermissionManager, memory, or Telegram.
- Main-thread worker assimilation reads completed worker results, attaches evidence to TaskGraph steps, and records `worker.result_assimilated`.
- Passing verification clears SessionBrain pending validation; failing verification leaves validation pending or eventually moves to `blocked`.
- `VerificationPlanner` recommends deterministic checks from objective/evidence. Runtime Python changes get `py_compile` plus `python self_test.py`; docs-only changes get an optional self-test note.

## Planner / Workflow Control

- Non-chat task turns first pass through `PlannerV1`, which writes deterministic planned steps into `task_graphs.json`.
- Tool results update the current planned step instead of always appending unrelated steps.
- UI/computer actions can produce `observe_needed`; YueYue should observe and verify before claiming the action fully succeeded.
- Cancel/stop style owner messages mark the active workflow cancelled instead of letting stale steps keep driving behavior.
- `Explorer` and `Reviewer` are read-only subagent roles. `Verifier` is limited to the verifier allowlist.

## Observability

- Trace events are written to `C:\Agent\workspace\project_cache\agent_trace.jsonl`.
- Run `python agent_observability.py` for a compact health snapshot: event counts, tool success rate, permission replay results, bundle grants/blocks, action verification status, repeated-failure replay count, interaction modes, latency buckets, social events, and recent errors.
- The summary is read-only and does not expose long chat content beyond the structured trace fields already recorded.

## Live Evaluation Gate

- Run `python agent_eval.py` after substantial runtime changes and before starting a larger phase.
- The CLI prints a human-readable gate report and writes `C:\Agent\workspace\project_cache\eval_report.json`.
- The report includes tool success/failure rate, most failed tools, permission replay success, permission policy health, planner coverage, workflow success rate, observe-needed counts, background worker success/timeout/assimilation rate, subagent health, persona health, render dedupe, context budget, recovery count, repeated failure replay count, latency buckets, knowledge search hit/empty rate, Telegram media/reaction events, recent errors, and Git hygiene.
- Passing the next-stage gate means no private runtime files are tracked, tool success rate is at least 80%, and permission/repeated-failure issues are visible instead of hidden.
- Before a major phase, still run:

```powershell
cd C:\Agent
python self_test.py
powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1 -CheckOnly
python agent_eval.py
```

## Knowledge Index

- `agent_knowledge.py` builds the local engineering knowledge index in `C:\Agent\workspace\project_cache`.
- Runtime files: `knowledge_manifest.json`, `knowledge_chunks.jsonl`, and `knowledge_index.jsonl`.
- Whitelisted sources: `ARCHITECTURE.md`, `RUNBOOK.md`, `workspace\brain\*.md`, `workspace\memory\chat_summary\rolling_summary.md`, task transactions, and failure replay cases.
- Excluded sources: `.env`, Telegram chat id, full chat history, screenshots, downloaded Telegram media, and noisy/generated project cache content.
- Tools:
  `search_knowledge(query, limit=5)`, `read_knowledge(chunk_id)`, and `reindex_workspace()`.
- `MemoryCompiler` uses knowledge search for task/tool/vision context only; normal chat and social sticker turns do not inject engineering docs.
- Rebuild manually:

```powershell
cd C:\Agent
python -c "import agent_knowledge; print(agent_knowledge.reindex_workspace())"
```

## Execution Recovery

- `execute_command` defaults to `cwd="project"` (`C:\Agent`) instead of the workspace folder.
- `cwd="workspace"` is supported for commands that must run under `C:\Agent\workspace`.
- Other cwd values are rejected.
- Command results include `cwd`, `resolved_cwd`, `project_root`, `returncode`, and `retry_hint`.
- If `execute_command` fails from `cwd="workspace"` because a project-root file is missing, `CompanionAgent` retries once with `cwd="project"` and emits `ToolRecoveryAttempt` / `ToolRecoveryResult`.

## Tool Count

There are 26 registered tools:

`get_screen_ui`, `click_ui_element`, `type_keyboard`, `press_hotkey`, `create_plan`, `update_plan`, `list_files`, `search_in_files`, `execute_async_command`, `web_search`, `read_webpage`, `download_file`, `analyze_media`, `read_file`, `write_file`, `delete_file`, `send_telegram_media`, `react_to_message`, `update_profile`, `update_memory`, `execute_python`, `execute_command`, `search_sticker`, `search_knowledge`, `read_knowledge`, `reindex_workspace`.

## Tests

Run the default regression suite:

```powershell
cd C:\Agent
python self_test.py
```

Expected current baseline:

`SUMMARY 142 passed, 0 failed`

Run a small live Telegram smoke test:

```powershell
cd C:\Agent
$env:RUN_LIVE_TELEGRAM_SMOKE='1'
python self_test.py
```

The live smoke sends one Telegram message, reacts to it, and sends one local sticker/media file.

## Live Social Smoke Checklist

Use these manual Telegram checks after starting bot mode:

1. Send plain text only.
   Expected: one natural reply after the debounce window; no vision/tool progress spam.

2. Send text, then a sticker within `YUEYUE_TURN_DEBOUNCE_SECONDS`.
   Expected: one combined reply that treats text as primary intent and sticker as mood.

3. Send a sticker only.
   Expected: quick social reply, no `analyze_media`, and YueYue may attach one local sticker.

4. Send two or three stickers.
   Expected: candidates are cataloged, duplicates are ignored, and curation reminder appears only when the threshold/cooldown allows it.

5. Send `list sticker candidates`.
   Expected: pending candidates show filename, tags, and any emoji/set/id metadata.

6. Send `approve recent 3 stickers cute`.
   Expected: recent candidates move into the approved local sticker index.

7. Send a sticker-battle prompt such as `鬥圖`.
   Expected: short playful reply, at most one local sticker, no duplicate sticker if the model already picked one.

8. Run:

```powershell
cd C:\Agent
python agent_observability.py
```

Expected: readable event summary and healthy tool success rate.

## Start

```powershell
cd C:\Agent
python main.py
```

Choose `1` for terminal chat or `2` for Telegram bot mode.

One-click Windows launcher:

- Double-click `C:\Agent\start_yueyue.bat` to check resources and start Telegram bot mode directly.
- Run `C:\Agent\start_yueyue.bat -SelfTest` to run the full regression suite before starting.
- Run `C:\Agent\start_yueyue.bat -CheckOnly` to verify Python, required files, workspace folders, health, and compilation without starting the bot.
- Startup logs are written under `C:\Agent\workspace\logs`.

Direct non-interactive modes:

```powershell
cd C:\Agent
python main.py --health
python main.py --telegram
python main.py --terminal
```

