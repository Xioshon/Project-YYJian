# YueYue Agent Runbook

## Runtime v3 Operations

Runtime v3 (`yueyue_v3/`) is the only runtime - there is no rollback path anymore.

```powershell
powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1 -Restart
```

Full regression gate:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1 -CheckOnly -SelfTest
```

The startup gate includes `python scripts\system_audit.py`. It is read-only apart from its atomic report at
`workspace/project_cache/system_audit.json`, and checks source encoding, asset decoding, sticker index targets,
required imports, and the 30-tool public contract.

`-SelfTest` runs the v3 pytest suite (`tests_v3/`), the response-quality script checks (`scripts/*_check.py`),
Ruff, and `scripts/secret_scan.py`. Do not push or publish if the secret scan fails.

Quality eval suite (separate from the mechanical gate): `python scripts/eval_suite.py` runs the free,
deterministic Tier-1 persona eval (curated good/bad exemplars vs the chat quality gate — the regression
net for any blocklist trim; also runs in pytest as `test_persona_eval_baseline`). Add `--live` for real
generation scored on route/register, `--judge` for LLM persona-feel judging (both use the API). Cases live
in `evals/cases.py`; `known_gap=True` records a baseline the suite flags on drift. Runtime v3 state is under
`workspace/project_cache/v3/`.

## What Changed

- The v2 `CompanionAgent` control layer (`core_agent.py` + ~19 dependent `agent_*.py` modules, plus `self_test.py`/`agent_eval.py`/`agent_benchmark.py`/`agent_knowledge.py`/`agent_observability.py`) was deleted after confirming it was never instantiated on the live path (`main.py` always builds `YueYueRuntimeV3`). ~15,000 lines removed.
- The architecture contract is documented in `C:\Agent\ARCHITECTURE.md`.
- Message coalescing groups quick Telegram text/photo/sticker bursts into one human turn before the agent replies.
- Social sticker indexing maps local stickers to coarse emotions and catalogs incoming stickers as metadata for future social behavior.
- `YueYueRuntimeV3`'s `WorkflowEngine` records durable task graphs with goal/step verification; a successful tool call is action evidence only, never completion by itself.
- Three independently-maintained copies of the same chat-text sanitizer logic (greeting detection, prompt-leak stripping, benign-testing-note detection) were consolidated into `chat_text_sanitizers.py`, imported by both `agent_short_context.py` and `yueyue_v3/context.py`.
- The reply script (Simplified/Traditional) now deterministically mirrors the owner's own current message via character-table conversion, rather than relying on a prompt instruction the model unreliably followed.
- Tools return structured `ToolResult` data instead of loose strings.
- Approval is explicit state, not guessed from the last assistant message.
- Telegram sticker rendering through `[表情包: filename]` or `[sticker: filename]` is autonomous and does not require tool approval.
- Explicit `send_telegram_media` still requires approval because it can transmit arbitrary local files.
- `react_to_message` is available and uses the current Telegram message context by default.

## Approval Rules

- `可以`, `好`, `允許`, `同意`, `ok`, `yes` approve the previously blocked tool call. For high-risk
  tools (`execute_command`/`execute_python`/`delete_file`/`download_file`) this is one-time only; for
  `write_file` (workspace-guarded) and the computer-control bundle the grant covers the remaining
  actions of the SAME workflow, so a create-append-verify task needs one nod, not one per write.
- `本輪允許`, `這次全部可以`, `全部可以`, `全權交給你`, `allow all` approve protected tools for the current task turn.
- If the model tries an unrelated protected tool after a single approval, it is blocked again.
- YueYue uses risk-tiered permission. Low-risk local/read-only tools should stay convenient; destructive, exfiltrating, or system-control actions stay guarded.
- Low-risk free tools include local read/search, screen observation, knowledge search, sticker search, media analysis, message reaction, and memory/profile updates with quality checks.
- Safe verifier commands such as `python -m py_compile ...` and `python -m pytest tests_v3` may run without extra approval.
- Workspace-local generated media, screenshots, stickers, and Telegram media cache files may be sent through `send_telegram_media` without extra approval. External absolute paths or suspicious private files still require approval.
- Turn approval is bundle-scoped:
  - `computer_control_bundle`: `click_ui_element`, `type_keyboard`, `press_hotkey`
  - `file_workspace_bundle`: `write_file`, `delete_file`, `download_file`
  - `telegram_media_bundle`: `send_telegram_media`
  - `screenshot_bundle`: `get_screen_ui`, `capture_screen`, `analyze_media`, `send_telegram_media`, `delete_file`
- High-risk tools such as arbitrary `execute_command`, arbitrary `execute_python`, and `execute_async_command` are not included in ordinary bundles.

## Model Configuration

`main.build_agent()` builds a `yueyue_v3.providers.SiliconFlowProvider`. Owner-facing voice
(chat turns, owner-voice task replies, fast greetings/stickers, presence check-ins) follows
`YUEYUE_CHAT_MODEL`; planning/tool execution/verification follow `YUEYUE_TASK_MODEL` /
`YUEYUE_STRONG_MODEL`. All model/config values are read via `core_tools.env_value` (real OS env
first, then `.env`) - `main.py` does not load `.env` into the process environment generically.

Optional `.env` values:

```env
# Owner-facing voice model (chat/persona). Empty = use the strong/task default.
YUEYUE_CHAT_MODEL=Pro/MiniMaxAI/MiniMax-M2.5
YUEYUE_STRONG_MODEL=deepseek-ai/DeepSeek-V4-Pro
YUEYUE_TASK_MODEL=
# Reasoning depth for task execution/planning/verification only (never chat).
# SiliconFlow DeepSeek honors OpenAI-standard low/medium/high; high is the real ceiling.
YUEYUE_TASK_REASONING_EFFORT=high
```

## Debug Trace

Two trace streams exist:

- `C:\Agent\workspace\project_cache\agent_trace.jsonl` - `main.py`'s pre-processing layer (turn coalescing, short context, social session, presence). Event kinds: `turn.part`, `turn.flush`, `turn.config_warning`, `short_context.turn_recorded`, `social_session.observed`, `presence.candidate`, `presence.suppressed`.
- `C:\Agent\workspace\project_cache\v3\runtime_events.jsonl` - `YueYueRuntimeV3`'s own event log, one event per real workflow/permission/provider action. Event kinds: `turn.received`, `turn.replied`, `permission.requested`, `permission.granted`, `permission.denied`, `permission.replayed`, `workflow.started`, `workflow.resumed`, `workflow.verified`, `workflow.completed`, `workflow.blocked`, `workflow.cancelled`, `tool.result`, `provider.call`, `provider.error`, `planner.failed`, `worker.evidence`, `verifier.error`, `external.evidence`.

Durable workflow graph state is stored at:

`C:\Agent\workspace\project_cache\task_graphs.json`

Generated repeated-failure replay cases are appended to:

`C:\Agent\workspace\project_cache\failure_replay_cases.jsonl`

Generated blocked-workflow replay cases are appended to:

`C:\Agent\workspace\project_cache\workflow_replay_cases.jsonl`

Background verifier jobs and results are appended to:

`C:\Agent\workspace\project_cache\worker_jobs.jsonl`
`C:\Agent\workspace\project_cache\worker_results.jsonl`

## Personality and Context

Personality files live in `workspace\brain\` (`personality.md`, `rules.md`, `personality_samples.md`) and are compiled per-turn by `yueyue_v3.context.ContextCompiler.system_prompt()` - see [ARCHITECTURE.md](ARCHITECTURE.md#persona-and-chat-quality) for the current, accurate description. There is no separate compiled-memory cache file; the compiler reads the source files directly and bounds the result to 12000 characters.

## Screen Observe Route

- Requests such as "截圖", "看螢幕", "幫我看看現在畫面" are classified as `screen_observe`.
- `screen_observe` is now a deterministic runtime route (`_screen_observe_turn`): `capture_screen` -> `analyze_media` always run as a forced chain, and the owner reply must contain the actual observed content (window, main area, notable text). A "畫面已經捕捉下來了" reply with no content cannot happen on this route - if vision fails, YueYue says honestly that the screenshot exists but she could not read it, and where it is stored.
- Requests that name settings, menus, pages, or other navigation targets are promoted to a full tool task.
- Internal screenshots may be analyzed with vision. A screenshot path is evidence only and never counts as the requested answer by itself.
- `capture_screen`, `list_windows`, and `focus_window` avoid improvised Python/Alt+Tab loops. `click_screen` accepts only the latest screenshot id and requires a fresh observation after every click.
- Intermediate screenshots remain internal unless the owner asks for them, the requested result is an image, or a blocker needs visual evidence.
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

## Short Context and URL Vision

- Short context is stored at `C:\Agent\workspace\project_cache\short_context.json` and keeps only recent Telegram logical turns. It helps YueYue resolve phrases like `這個`, `剛剛那個`, and `你覺得呢`; it is not long-term memory.
- URL context is stored at `C:\Agent\workspace\project_cache\url_context_cache.json`; downloaded previews are stored under `C:\Agent\workspace\project_cache\url_previews`.
- `inspect_url(url, depth)` supports `metadata`, `auto`, `preview`, and `full`. It is low-risk but bounded by timeouts and cache.
- The URL pipeline tries cheap metadata first, then optional preview. It does not use login cookies, private account data, anti-bypass tricks, or full video downloads.
- Douyin links are classified as Chinese Douyin. Expect graceful degradation for app-only, login-gated, region-restricted, or anti-bot pages.
- If YueYue only sees a title, cover, or page screenshot, she should say so naturally instead of pretending to have watched the full video.
- Manual smoke tests:
  - Send a normal webpage URL and ask `你覺得呢`; expected: YueYue references title/description or says what she can see.
  - Send a Bilibili or YouTube URL; expected: metadata/preview when available, cache hit on second try.
  - Send a Douyin URL; expected: classified as Douyin, with clear fallback if restricted.
- Send `這個好抽象` after a URL; expected: YueYue binds `這個` to the recent URL.

## Presence Engine

- Phase B3 is the Presence Engine v1 close-out: `PresenceEngine` decides whether it is okay to reach out, then `PresenceComposer` uses the model to decide whether there is actually something worth saying.
- Default mode is `notify`: YueYue has a higher safety cap of eight short Telegram presence messages per day, with at least 75 minutes between candidates. This is not a quota; if the composer has no specific topic, YueYue stays quiet. Set `YUEYUE_PRESENCE_MODE=shadow` to record only.
- Runtime files:
  - `C:\Agent\workspace\project_cache\presence_state.json`
  - `C:\Agent\workspace\project_cache\presence_candidates.jsonl`
  - `C:\Agent\workspace\project_cache\presence_health.json`
  - `C:\Agent\workspace\project_cache\presence_debug.jsonl`
  - `C:\Agent\workspace\project_cache\presence_composer_debug.jsonl`
  - `C:\Agent\workspace\project_cache\presence_topic_history.jsonl`
- Telegram status prompts:
  - `月月剛剛有沒有想找我`
  - `月月刚刚有没有想找我`
  - `月月為什麼沒找我`
  - `月月为什么没找我`
  - `月月刚刚想说什么`
  - `presence status`
- Environment configuration:

```powershell
YUEYUE_PRESENCE_MODE=notify
YUEYUE_PRESENCE_DAILY_LIMIT=8
YUEYUE_PRESENCE_MIN_INTERVAL_MINUTES=75
YUEYUE_PRESENCE_QUIET_HOURS=23:30-08:00
YUEYUE_PRESENCE_OWNER_AWAKE_MINUTES=30
YUEYUE_PRESENCE_STALE_TASK_MINUTES=120
YUEYUE_PRESENCE_TICK_MINUTES=45
YUEYUE_PRESENCE_ICEBREAK_AFTER_MINUTES=360
YUEYUE_PRESENCE_COMPOSER_MODEL=deepseek-ai/DeepSeek-V4-Pro
```

Modes:

- `off`: no candidates.
- `shadow`: record candidates and suppression reasons only.
- `notify`: send model-composed Telegram presence messages through the gateway scheduler only after the quality gate passes.

Quality policy:

- Avoid generic check-ins such as `你還好嗎` or `今天怎麼樣` unless recent context clearly justifies care.
- Prefer recent jokes, links, stickers, light teasing, small shares, or a concrete follow-up that gives the owner something easy to reply to.
- If there is a recent URL, sticker exchange, mood signal, or joke, Presence treats it as a follow-up opportunity.
- If there has been no interaction for about six hours during non-quiet time, Presence may create an `icebreak` opportunity. The composer should open a small new topic, not ask why the owner disappeared.
- If the model output is repetitive, formal, task-like, too long, malformed, or not worth sending, YueYue records the reason and sends nothing.

Quiet hours are a soft rule. If the owner interacted recently, YueYue treats the owner as likely awake and may generate a candidate, but daily limit, cooldown, task state, and composer quality still apply. Fresh active tasks, permission waits, and validation waits suppress Presence. Stale task states older than the configured threshold stop blocking forever.

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

## Workflow State and Verification

`yueyue_v3.workflow.WorkflowEngine` is the only workflow/permission state writer (see [ARCHITECTURE.md](ARCHITECTURE.md#runtime-v3-the-only-runtime)):

- Non-chat turns get a structured `GoalContract` (requested outputs, success criteria, steps, allowed tools per step, risk). The model proposes the plan; `yueyue_v3.planning` validates it.
- The engine advances through Plan -> Act -> Observe -> Verify Step -> Verify Goal -> Reply. Tool exit code, screenshot creation, clicks, and Telegram delivery are action evidence only, never completion by itself - a workflow completes only once the goal is verified against actual evidence.
- `yueyue_v3.permissions.PermissionController` replays the exact pending action on single approval (`可以`/`ok`), instead of asking the model to regenerate the tool call.
- If the same tool fails repeatedly, the engine blocks the workflow and composes an owner-facing explanation instead of continuing to spin.
- Cancel/stop style owner messages mark the active workflow cancelled.
- Workflow state persists across restarts in `workspace\project_cache\v3\runtime_state.json`, but restoring it never grants permission or auto-runs protected tools.

Check current health directly:

```powershell
python -m yueyue_v3.health --root C:\Agent
```

## Stall Watchdog

- `GatewayWatchdog` (`agent_watchdog.py`) guards against the "process alive, messages ignored" failure mode.
- Heartbeat: every `get_updates` call beats; no beat for 150s means the polling thread is wedged (not just flaky Wi-Fi - beats fire on call, not on success).
- In-flight turns: stuck past 3 minutes -> one owner alert; past 10 minutes -> automatic restart.
- Before any restart it dumps every thread's stack to `C:\Agent\workspace\logs\watchdog\stall_*.log` - read that file to root-cause a stall after the fact.
- Restart path: process exits with code 21; `start_yueyue.bat` restarts it after 5 seconds. Owner alerts go through a direct HTTPS call with its own 10s timeout, never the possibly-hung TeleBot session.
- Deterministic checks: `python scripts\watchdog_check.py`.

## Execution Recovery

- `execute_command` defaults to `cwd="project"` (`C:\Agent`) instead of the workspace folder.
- `cwd="workspace"` is supported for commands that must run under `C:\Agent\workspace`.
- Other cwd values are rejected.
- Command results include `cwd`, `resolved_cwd`, `project_root`, `returncode`, and `retry_hint`.

## Tool Count

There are 30 registered tools (`from core_tools import ALL_TOOLS; len(ALL_TOOLS)`):

`get_screen_ui`, `capture_screen`, `list_windows`, `focus_window`, `click_screen`, `click_ui_element`, `type_keyboard`, `press_hotkey`, `create_plan`, `update_plan`, `list_files`, `search_in_files`, `execute_async_command`, `web_search`, `read_webpage`, `download_file`, `analyze_media`, `read_file`, `write_file`, `delete_file`, `send_telegram_media`, `react_to_message`, `update_profile`, `update_memory`, `execute_python`, `execute_command`, `search_sticker`, `inspect_url`, `read_url_context`, `reindex_url_cache`.

(`search_knowledge`, `read_knowledge`, `reindex_workspace` were removed — they called into `agent_knowledge.py`, a module deleted during the v2 cleanup; they always failed.)

## Tests

Run the default regression suite:

```powershell
cd C:\Agent
python -m pytest tests_v3 -q
```

Expected current baseline: `156 passed`. Adds tests_v3/test_report_result.py (the report_result derive-and-submit tool that lets compute-from-observation tasks complete) and expanded voice_contract Cantonese-phrase coverage. Key suites: `tests_v3/test_reasoning_scope.py` locks in
that task execution/planning/verification calls carry `reasoning_effort` (default high) while
chat/persona voice calls — which may run on a different provider like MiniMax — never do;
`tests_v3/test_voice_contract.py` covers the centralized `voice_contract.py` register gate;
`tests_v3/test_interaction_routing.py` locks the natural-task-verb / casual-mention routing.
(The earlier typo-injection humanization feature and its test were removed 2026-07-12 — it
relied on a fixed correction-phrase pool, which is exactly the canned-line pattern the owner
rejects.)

There is no automated live-Telegram pytest smoke test (an earlier reference to one was stale
documentation - it was removed along with the v2 cleanup and never re-added). Live verification
is manual: start the bot and work through the checklist below.

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
python -m yueyue_v3.health --root C:\Agent
```

Expected: `Gate: pass` with tool count 30 and 0 replay failures.

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
- Run `powershell -ExecutionPolicy Bypass -File C:\Agent\start_yueyue.ps1 -Restart` when you intentionally want to replace a running YueYue service.
- The launcher writes `workspace\project_cache\yueyue_launcher.pid` and refuses accidental duplicate starts. `-Restart` stops the recorded launcher process tree before starting a fresh service.
- Startup logs are written under `C:\Agent\workspace\logs`.

Direct non-interactive modes:

```powershell
cd C:\Agent
python main.py --health
python main.py --telegram
python main.py --terminal
```

