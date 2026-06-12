# YueYue Agent

YueYue is a private Telegram cyber-catgirl companion agent runtime with tools, permissions, memory, social sticker behavior, replay tests, and local observability.

## Setup

1. Install Python 3.
2. Copy `.env.example` to `.env`.
3. Fill in your local API and Telegram values in `.env`.
4. Keep `.env` private.

## Run

```powershell
cd C:\Agent
powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1
```

Check startup without launching Telegram:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1 -CheckOnly
```

Run the regression suite:

```powershell
python self_test.py
```

Generate the live evaluation gate report:

```powershell
python agent_eval.py
```

Rebuild the local engineering knowledge index:

```powershell
python -c "import agent_knowledge; print(agent_knowledge.reindex_workspace())"
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

## Knowledge Index

YueYue keeps a lightweight local RAG-style index in `workspace/project_cache/`.

It indexes only low-noise engineering sources such as `ARCHITECTURE.md`, `RUNBOOK.md`, `workspace/brain/*.md`, `workspace/memory/chat_summary/rolling_summary.md`, task transactions, and failure replay cases.

It does not index `.env`, Telegram chat ids, full `workspace/chat_history/`, screenshots, downloaded Telegram media, or generated cache files. The runtime tools are `search_knowledge`, `read_knowledge`, and `reindex_workspace`.

## Live Evaluation

`agent_eval.py` reads the local trace and writes `workspace/project_cache/eval_report.json`.

It reports tool success rate, permission replay success, permission policy health, planner coverage, workflow success rate, observe-needed counts, background worker success/timeout/assimilation rate, subagent health, persona health, render dedupe, context budget, repeated failure cases, latency buckets, knowledge search hit rate, recent errors, and Git hygiene status. Use it after significant runtime changes before moving to larger workflow or social-layer work.

## Permission Model

YueYue uses risk-tiered permission. Low-risk local/read-only tools, safe verifier commands, workspace media sending, and memory/profile updates with quality checks should feel smooth. Destructive actions, arbitrary commands, external file paths, downloads, and UI control still require explicit approval.

## Persona and Screen Observe

YueYue's personality files live in `workspace/brain/`. The current SOUL core is cyber catgirl: playful, tsundere, clingy, high-energy cute, and loyal to Xioshon. Chat and sticker turns should carry visible 喵~ / kaomoji flavor; task turns stay reliable without becoming a cold work assistant.

Screen-observe requests such as "截圖" or "幫我看看畫面" use a short route: observe once, summarize once, then stop. The normal tool loop will block unrelated tools in this route and stop repeated same-tool retries before they can spam Telegram. Screenshot and sticker markers are deduped by the Telegram renderer.

## Durable Workflow

YueYue records durable task graphs in `workspace/project_cache/task_graphs.json` and blocked workflow replay cases in `workspace/project_cache/workflow_replay_cases.jsonl`.

Non-chat tasks are first turned into conservative planned steps by Planner v1. Tool results, observe-needed states, and verifier evidence attach back to those steps. Workflow summaries can be restored after restart, but they do not auto-run protected tools or bypass permission.

## Hybrid Worker

YueYue keeps one main decision thread for chat, permission, TaskGraph, memory, and Telegram replies.

Background verifier workers only run allowlisted checks such as `py_compile`, `self_test`, `agent_eval`, and trace summary. Results are written to `workspace/project_cache/worker_results.jsonl` as evidence; workers do not modify permission, TaskGraph, memory, or Telegram directly.

The main agent thread later assimilates worker results into the active TaskGraph. This keeps verification faster without letting background jobs change state on their own.

## Context Budget

`ContextPackBuilder` writes `workspace/project_cache/context_budget_report.json` when it builds prompt context.

Chat and social turns stay light. Task/tool/vision turns can add SessionBrain, TaskGraph, selected skills, and bounded engineering knowledge without stuffing the whole workspace into the model.

## Command Execution

`execute_command` runs from the project root by default, so verification commands such as this work:

```powershell
python -m py_compile core_tools.py
```

When a command must run inside the workspace, pass `cwd="workspace"`. Other cwd values are rejected.

If a command fails because it cannot find a file, the tool result includes cwd metadata and a retry hint. The agent runtime may retry once from the project root when the failure is clearly cwd-related.
