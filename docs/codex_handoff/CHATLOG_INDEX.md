# Chatlog Index

This file indexes important ChatGPT discussions for Codex.

Raw exports should be placed under:

```text
docs/codex_handoff/chat_exports/raw/
```

Summaries should be placed under:

```text
docs/codex_handoff/chat_exports/summaries/
```

## Important Discussion Groups

### 1. Quality Baseline 2026-06-25 F

Topic:

* YueYue has moved from emergency bug repair into stability and quality consolidation.
* Engineering baseline is considered pass.
* Persona quality is not yet pass.

Decision:

* Do not treat the project as broken.
* Preserve the current baseline.
* Next work should focus on response quality, temporal consistency, and persona behavior.

### 2. Runtime Modularization

Topic:

* `main.py` was split into focused modules.

Important modules:

```text
telegram_input.py
temporal_context.py
sticker_assets.py
sticker_flow.py
reply_context.py
telegram_outbox.py
intent_router.py
response_composer.py
```

Decision:

* Do not merge everything back into `main.py`.
* Keep modules focused and testable.

### 3. Prompt Framework Discussion

Topic:

* Learning from ChatGPT / Claude client prompt structures.
* Considering a future PromptCompiler.

Decision:

* Do not copy leaked prompts directly.
* Do not build one giant mega prompt.
* Build modular prompt files later.
* Wait until the current baseline is protected.

### 4. GitHub / Codex Handoff Discussion

Topic:

* How to prepare files before letting Codex work.
* How to avoid uploading private data.

Decision:

* Use `AGENTS.md` for short Codex instructions.
* Use `docs/codex_handoff/CODEX_HANDOFF.md` for detailed handoff.
* Use ignored `chat_exports/raw/` for raw ChatGPT exports.
