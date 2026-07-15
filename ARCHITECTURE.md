# YueYue Agent Architecture

This project should be treated as a small agent runtime, not as a pile of prompt tricks.

## Runtime v3 (the only runtime)

Runtime v3 is implemented under `yueyue_v3/`. It is the sole runtime - the earlier `CompanionAgent` control layer and its entire support cluster (`core_agent.py` and ~19 dependent `agent_*.py` modules) were removed once confirmed dead: `main.py` always builds `YueYueRuntimeV3`.

- `YueYueRuntimeV3` is the only workflow/session/permission state writer.
- `RuntimeEvent` serializes Telegram turns, tool results, permission replay, and worker evidence.
- `WorkflowState` contains `GoalContract`, `StepContract`, requested outputs, evidence, and verification.
- `WorkflowEngine` separates action success, step satisfaction, and goal satisfaction.
- `ObservationService` stores revision-bound `UiSnapshot` and `UiElement` records. Clicks are rejected when a snapshot expires or the active window changes.
- `PermissionController` directly replays the original pending action. Computer-control approval is workflow scoped; high-risk execution remains single-action.
- `ContextCompiler` isolates chat/social/task/vision/presence context and bounds execution transcripts. It shares its text-sanitizing helpers (greeting detection, prompt-leak stripping, workflow-meta stripping, benign-testing-note detection) with `agent_short_context.py` via `chat_text_sanitizers.py` - both consumers used to keep independent copies of this logic, which is why it is now a single shared module.
- `RenderLedger` gives Telegram text/media at-most-once rendering per inbound event.
- `AtomicJsonStore` uses UTF-8, `fsync`, backup, and `os.replace`; v3 runtime files carry schema version 3.

The execution invariant is `Plan -> Observe -> Decide -> Act -> Verify Step -> Verify Goal -> Reply`. A successful screenshot, click, or command is evidence only; it is never equal to task completion.

Two execution-loop guarantees added 2026-07-15 (gap-battery findings):
- The evidence tail (`_evidence_note`) is rendered into every execution-loop conversation, so tool results survive permission round-trips (the in-loop transcript resets there). Command stdout/stderr/returncode are captured into evidence facts, and requested-output binding prefers stdout over generic tool status phrases ("Command completed.").
- `report_result` values must be grounded: `_report_value_grounded` rejects any reported value that does not appear in actual tool evidence, so the model cannot fabricate an answer to finish a task.

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
   Runtime events are appended to `workspace/project_cache/agent_trace.jsonl` (pre-v3 pipeline) and `workspace/project_cache/v3/runtime_events.jsonl` (`YueYueRuntimeV3`).

6. Harness before feature sprawl.
   Protocols, hooks, replay cases, and verification gates are the control plane that keeps the agent reliable. Prefer deleting an unused subsystem over letting it rot alongside its replacement.

## Runtime Layers (live components)

- `SiliconFlowAdapter` (`agent_llm.py`)
  Converts local message/tool objects into OpenAI-compatible SiliconFlow requests. Used by `PresenceEngine`'s background check-in generation.

- `SiliconFlowProvider` (`yueyue_v3/providers.py`)
  The provider `YueYueRuntimeV3` actually talks to for every real conversation turn.

- `agent_protocol`
  Owns approval phrases, reply markers, status labels, and fail-safe text with Unicode-safe constants.

- `agent_hooks` (`HookManager`)
  Emits lifecycle events and writes trace JSONL.

- `TelegramGateway` (`main.py`)
  Handles Telegram updates, message context, idempotent reply rendering, sticker sending, screenshot marker dedupe, and low-noise tool status updates. Constructs a `YueYueRuntimeV3` via `build_agent()` and drives every real turn through it. Reply text is split into bubbles on the model's own newlines (sentence-splitting only rescues overlong runs, soft limit 60 chars, max 4 bubbles), and a short trailing afterthought line (不過…/對了…) is sent a beat later like a real follow-up text. During tasks the gateway narrates significant tools to the owner (`TOOL_PROGRESS_LABELS`, deduped and rate-limited, max 6 lines/turn) and reports non-retryable tool failures honestly instead of staying silent.

- `GatewayWatchdog` (`agent_watchdog.py`)
  Detects the "process alive but nothing happens" failure mode. Heartbeats piggyback on every `get_updates` call (a healthy long-polling loop beats at least every ~25s even during network flaps, because the beat fires on call, not on success); each aggregated turn registers in-flight and deregisters in a `finally`. A turn stuck past 3 minutes alerts the owner once; past 10 minutes - or a wedged polling thread past 150s - it dumps every thread's stack to `workspace/logs/watchdog/` (forensics survive the restart), alerts the owner through a direct HTTPS call with its own timeout (never the possibly-hung TeleBot session), and exits with code 21 so `start_yueyue.bat`'s restart loop self-heals in ~5 seconds. Deterministic coverage lives in `scripts/watchdog_check.py`.

- `agent_latency`
  Classifies interactions as `chat`, `social_sticker`, `vision_task`, `screen_observe`, or `tool_task`, applies route-specific tool budgets, and caches media analysis. Bare, everyday action words (打開/關閉/播放/點擊/etc.) require either a named controllable target (瀏覽器/設定/程式/etc.) or an explicit request phrase (幫我/請/可以...) to route to `tool_task` - a bare mention in ordinary chat ("我打開音樂在聽") must not hijack routing. The same co-occurrence discipline applies to screen-observation triggers ("是不是"/"現在"/"狀態" only mean "check the screen" alongside an actual screen/device word).

- Screen control primitives
  `capture_screen` creates an internal short-lived screenshot id; `list_windows` and `focus_window` locate the intended application; `click_screen` accepts coordinates only from the latest fresh screenshot and always requires a follow-up observation. Intermediate screenshots are evidence and are not sent to Telegram by default.

- Health gate (`yueyue_v3/health.py`)
  A blocked workflow (owner hasn't said "繼續" yet after a "no fake success" block) is reported in `active_workflow_status` but is deliberately NOT a gate blocker - `start_yueyue.ps1` runs this health check on every startup with `$ErrorActionPreference = "Stop"`, so gating on a normal, resumable blocked state would mean the bot could never restart again after any legitimately blocked task, including via the watchdog's own self-heal restart. Only structural problems (wrong public tool count, state schema mismatch, replay failures) fail the gate.

- Screen-observe route (`YueYueRuntimeV3._screen_observe_turn`)
  "What's on my screen" questions never go through the planner. The runtime deterministically chains `capture_screen` -> `analyze_media` and the reply must carry the actual visual content (guarded by `_reply_reflects_content`: a persona reply that fails to reflect the observed summary is replaced by a fallback that does). Capture or analysis failures are reported honestly, including where the screenshot ended up. A reply of "captured it" with no content is a structural impossibility on this route, not a tuning hope.

- `SocialStickerIndex` / `SocialSessionManager` / `SocialReplyPolicy` / `SocialCurationReminder` (`agent_social.py`)
  Builds a local emotion index for stickers, catalogs incoming Telegram stickers as metadata, and supports deterministic sticker selection before LLM fallback. Keeps short-lived per-chat social rhythm (sticker battle, affection, teasing) as in-memory runtime state only - it does not update profile, memory, or workflow state.

- `ShortContextBuffer` (`agent_short_context.py`)
  Keeps the last 20 logical chat turns per chat for lightweight social grounding: recent text, URL summaries, media metadata, mood/topic hints, and the last assistant reply summary. This runs in `main.py`'s pre-processing layer, separate from (and in addition to) `YueYueRuntimeV3`'s own `ShortContextStore`.

- `URLContextCache` (`agent_url_context.py`)
  Classifies and caches URL metadata/preview context for YouTube, Bilibili, Douyin, TikTok, Instagram, X/Twitter, direct images, and ordinary websites. Extraction is layered and bounded: metadata first, optional preview only on demand, hard timeouts, no login cookies, no platform bypassing, and clear failure reasons.

- `PresenceEngine` (`agent_presence.py`)
  Evaluates whether YueYue would naturally want to check in after recent chat context. Quiet hours are a soft rule, recent owner activity can mark the owner as likely awake, stale task states stop blocking forever, and decisions are written to `presence_debug.jsonl`. In `notify` mode it can send one low-frequency Telegram text check-in through the gateway scheduler; it never runs tools, mutates memory, or changes workflow state.

## Latency Policy

- Plain chat and social stickers use a quiet policy: no proactive vision, at most one tool attempt plus one final response.
- Stickers are treated as social/emotional signals unless the owner explicitly asks for analysis.
- Photos only trigger vision when the caption/message asks to look, analyze, identify, or describe.
- Dynamic sticker/video formats are recorded as media metadata and are not sent to image-only vision.
- Vision results are cached by file hash and summarized before returning to the main agent loop.
- Slow vision/tool tasks may send a quick acknowledgement before the heavy work completes.
- Sticker battles and mood stickers use local sticker search/indexing first; incoming stickers are cataloged as social metadata and are not analyzed unless requested.
- Sticker auto-selection uses quiet eligibility checks and keeps incoming stickers as unapproved candidates until curated.
- URL handling follows the same latency principle: cheap metadata/cache is allowed in chat, heavier preview is only triggered when the owner asks to look at or comment on the link. Douyin is treated as Chinese Douyin, not TikTok; app-only, login-gated, or region-restricted pages must degrade honestly instead of inventing content.
- Presence handling is low-cost. Post-turn evaluation records candidates and debug evidence; a background scheduler performs bounded ticks every configured interval. Quiet hours suppress checks unless recent Telegram context indicates the owner is likely awake. Fresh active tasks, permission waits, validation waits, cooldown, and daily limits still suppress proactive messages.

## Persona and Chat Quality

- Personality is stable SOUL behavior guidance: YueYue is Xioshon's cyber catgirl, not a generic assistant. Personality files live in `workspace/brain/` (`personality.md`, `rules.md`, `personality_samples.md`).
- CHAT/SOCIAL replies default to one short, natural bubble; splitting into multiple bubbles is the exception for a genuinely heavy moment, not the default. The model controls bubble breaks with newlines (the gateway honors them verbatim), so "hold this line for the next bubble" is a model decision, not a regex accident. `scripts/blocklist_growth_check.py` guards the reject/marker lists in `yueyue_v3/runtime.py`, `yueyue_v3/context.py`, and `response_composer.py` against silent unreviewed growth.
- Lexical register is Hong Kong **written** Traditional Chinese with mainland-internet chat rhythm (屏幕/網絡/軟件 over 螢幕/網路/軟體; no habitual 喔/喲/耶 endings; written register also means NO spoken-Cantonese characters like 嘅/喺/㗎/咗/唔/冇; 笑死/絕了/？？？-style expressions allowed sparingly; 語氣詞 like 啦/咯 welcome but at most one per line). The single source of truth is **`voice_contract.py`**: `VOICE_REGISTER_ZH` / `VOICE_REGISTER_EN` are the prompt-side instruction (imported by the CHAT mode contract in `yueyue_v3/context.py`, the owner-voice prompt in `yueyue_v3/runtime.py`, `PRESENCE_COMPOSER_SYSTEM_PROMPT` in `agent_presence.py`, and all five composer prompts in `response_composer.py`), and `voice_register_violation()` is the output-side gate (wired into `_chat_reply_violates_social_policy`, `_compose_owner_voice`, the presence quality gate, and the fast-reply validators). `workspace/brain/personality_samples.md` keeps a human-readable copy of the same standard as few-shot calibration.

  When adding ANY new prompt that produces an owner-facing line: import the register constant from `voice_contract.py` (never hand-copy the wording) and run the output through `voice_register_violation()` with a retry or clean-Traditional fallback. Tuning the register means editing `voice_contract.py` + `personality_samples.md` - two files, nothing else.
- The reply script (Simplified/Traditional Chinese) mirrors the owner's own current message via `yueyue_v3.context.owner_script_is_simplified_with_history` + `to_simplified_script` - a deterministic character-table conversion, not a prompt instruction, because the model's own Traditional-Chinese baseline and an already-Traditional conversation history reliably outweigh a same-turn prompt note.
- Full chat history remains out of the default prompt; only the last few short-context turns are included.

## Repository Policy

- Source code, docs, safe examples, and curated non-private assets belong in Git.
- Runtime logs, traces, chat history, Telegram chat ids, downloaded Telegram media, screenshots, project cache, pycache, and `.env` stay local.
- Private repository status is useful but not a substitute for `.gitignore` and clean tracking.

## Permission Contract

- Chinese single-approval phrases such as "can/ok/approve/agree" plus `ok`, `yes`
  Approve the previously blocked exact tool call once. The runtime replays the saved pending action directly instead of asking the model to generate a new tool call.

- Chinese turn-approval phrases such as "allow this turn / all ok this time / full authority" plus `global allow`, `allow all`
  Approve protected tools for the current task turn, limited to the relevant bundle inferred from the pending action.

- Current bundles:
  `computer_control_bundle`, `file_workspace_bundle`, `telegram_media_bundle`, and `screenshot_bundle`.

- Free low-risk tools include local read/search, screen observation, knowledge search, sticker search, media analysis, message reactions, and memory/profile updates with quality checks.

- High-risk tools:
  Arbitrary `execute_command`, arbitrary `execute_python`, `execute_async_command`, destructive file operations, external downloads, external-path media sending, and UI control require approval and are not smuggled through ordinary bundles.

- Any unrelated protected tool after a single approval is blocked again.

## Reply Markers

Preferred:

- Chinese sticker marker: `[表情包: filename]`
- Chinese screenshot marker: `[系統截圖: filename]`

ASCII fallbacks:

- `[sticker: filename]`
- `[screenshot: filename]`

## Regression Gates

Before trusting a change:

```powershell
cd C:\Agent
python -m py_compile main.py reply_context.py sticker_flow.py sticker_assets.py telegram_input.py intent_router.py response_composer.py temporal_context.py telegram_outbox.py

python scripts\reply_context_check.py
python scripts\sticker_flow_check.py
python scripts\sticker_assets_check.py
python scripts\sticker_resend_check.py
python scripts\telegram_input_check.py
python scripts\intent_router_check.py
python scripts\response_composer_check.py
python scripts\temporal_context_check.py
python scripts\stability_check.py
python scripts\blocklist_growth_check.py
python scripts\watchdog_check.py

powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1 -CheckOnly
```

For a full regression pass including the pytest suite and Ruff:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1 -CheckOnly -SelfTest
```

`tests_v3/` (pytest) covers protocol encoding, scoped approval, permission bundles, workflow verification, replay, provider resilience, and Telegram rendering for the v3 runtime.

Live Telegram verification is manual (see RUNBOOK.md's Live Social Smoke Checklist) - there is
no automated pytest smoke test against the real Telegram API.

## What Not To Do

- Do not add new tool names without adding schema tests.
- Do not parse approval by reading the last assistant message.
- Do not let model-chosen stickers go through `send_telegram_media` approval.
- Do not silently retry code with another LLM inside `execute_python`.
- Do not hide tool failures in natural-language replies only; preserve structured tool results and trace events.
- Do not bypass hooks when adding new tool execution paths.
- Do not copy-paste a text sanitizer/classifier into a second file "just for this one caller" - it will quietly diverge from the original the next time either copy gets tuned. Add the caller to the shared module's import list instead.
