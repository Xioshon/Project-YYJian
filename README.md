# YueYue Agent

YueYue is a private Telegram cyber-catgirl companion agent runtime with tools, permissions, memory, social sticker behavior, replay tests, and local observability.

## Setup

1. Install Python 3.11 or newer.
2. Run `python -m pip install -r requirements.txt`.
3. Copy `.env.example` to `.env`.
4. Fill in your local API and Telegram values in `.env`.
5. Keep `.env` private.

Runtime v3 (`yueyue_v3/`) is the only runtime. It keeps the 30 public tool names and Telegram behavior on one event loop, one workflow state, structured observations, goal verification, and atomic state files.

## Run

```powershell
cd C:\Agent
powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1
```

Check startup without launching Telegram:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1 -CheckOnly
```

`CheckOnly` also runs `scripts/system_audit.py`, which verifies UTF-8 source files, static image/WebM assets,
sticker index references, runtime dependencies, and all 30 public tool schemas. The URL preview dependencies
(`yt-dlp`, Playwright, and Pillow) are installed from `requirements.txt`; Playwright uses the existing Chrome
installation before considering its own Chromium runtime.

Restart the Telegram service cleanly if a previous launcher is still running:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1 -Restart
```

Run the full regression gate:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1 -CheckOnly -SelfTest
```

This runs the v3 pytest suite (`tests_v3/`), the response-quality script checks (`scripts/*_check.py`), the Ruff lint gate, and a tracked-file secret scan.

Check the live runtime health directly:

```powershell
python -m yueyue_v3.health --root C:\Agent
```

Generated v3 state lives under `workspace/project_cache/v3/` and is never committed.

## Model Configuration

YueYue talks to the model through `yueyue_v3.providers.SiliconFlowProvider`. Configure the model in `.env`:

```env
YUEYUE_TASK_MODEL=
YUEYUE_STRONG_MODEL=deepseek-ai/DeepSeek-V4-Pro
```

## Repository Hygiene

This repository should contain source code, docs, safe configuration examples, and curated assets only.

Do not commit:

- `.env` or real API keys
- Telegram chat id files
- private chat history
- runtime traces and logs
- screenshots and downloaded Telegram media
- `__pycache__` or `.pyc` files
- generated project cache files

The local workspace may still contain those files; `.gitignore` prevents new runtime/private files from entering Git.

## Permission Model

YueYue uses risk-tiered permission. Low-risk local/read-only tools, safe verifier commands, workspace media sending, and memory/profile updates with quality checks should feel smooth. Destructive actions, arbitrary commands, external file paths, downloads, and UI control still require explicit approval.

## Persona and Screen Observe

YueYue's personality files live in `workspace/brain/`. The current SOUL core is cyber catgirl: playful, tsundere, clingy, high-energy cute, and loyal to Xioshon. Chat and sticker turns should carry visible 喵~ / kaomoji flavor; task turns stay reliable without becoming a cold work assistant.

Screen-observe requests such as "截圖" or "幫我看看畫面" use a short route: observe once, summarize once, then stop. The normal tool loop will block unrelated tools in this route and stop repeated same-tool retries before they can spam Telegram. Screenshot and sticker markers are deduped by the Telegram renderer.

## Durable Workflow

`yueyue_v3.workflow.WorkflowEngine` converts non-chat requests into a structured `GoalContract` with requested outputs and success criteria, then executes one evidence-backed step at a time. Tool success is only action evidence; the workflow completes only after the goal is verified against actual evidence. Workflow state persists across restarts in `workspace/project_cache/v3/runtime_state.json`, but it does not auto-run protected tools or bypass permission.

## Context Budget

`yueyue_v3.context.ContextCompiler` builds a bounded, mode-isolated prompt per turn (CHAT/SOCIAL/TASK/VISION/PRESENCE). Chat and social turns stay light; task/vision turns add the current workflow contract without stuffing the whole workspace into the model.

## Command Execution

`execute_command` runs from the project root by default, so verification commands such as this work:

```powershell
python -m py_compile core_tools.py
```

When a command must run inside the workspace, pass `cwd="workspace"`. Other cwd values are rejected.

If a command fails because it cannot find a file, the tool result includes cwd metadata and a retry hint. The agent runtime may retry once from the project root when the failure is clearly cwd-related.
