# Project YYJian / YueYue Agent Instructions

## Current Priority

Preserve the current baseline first.

Current baseline:

```text
Quality Baseline 2026-06-25 F
```

Current phase:

```text
Stability & Quality Freeze / Quality Consolidation
```

Do not treat this project as a broken emergency patch state.

The next intended phase is:

```text
Persona Quality & Response Policy
```

Focus on response quality, temporal consistency, persona consistency, and sticker emotional behavior.

## Read First

Before editing code, read:

1. `docs/codex_handoff/CODEX_HANDOFF.md`
2. `docs/codex_handoff/CHATLOG_INDEX.md`
3. `ARCHITECTURE.md`
4. `RUNBOOK.md`
5. `README.md`

## Working Rules

* Make small, reversible changes.
* Do not rewrite the whole runtime.
* Do not reset the repo.
* Do not run `git clean -fd`.
* Do not run `git reset --hard`.
* Do not delete untracked modules.
* Do not treat untracked source files as garbage.
* Do not start the PromptCompiler rewrite yet.
* Do not add new tool systems, GUI systems, memory systems, or large architecture changes.
* Do not add automatic sticker spam.
* Do not use final-output string replacement to fake temporal consistency.
* Do not commit `.env`, raw ChatGPT exports, private memory, logs, runtime caches, or local generated files.

## Backup Rules

* Back up files outside `C:\Agent`.
* Use `C:\Agent_manual_backup` for manual backups.
* Never create recursive backups inside the project folder.

## Windows PowerShell Rules

This environment is Windows PowerShell, not Bash.

Do not use Bash heredoc syntax:

```bash
python - <<'PY'
```

Use PowerShell here-string syntax instead:

```powershell
@'
print("hello")
'@ | python
```

## Current Module Understanding

Important current modules:

```text
main.py
telegram_input.py
temporal_context.py
sticker_assets.py
sticker_flow.py
reply_context.py
telegram_outbox.py
intent_router.py
response_composer.py
start_yueyue.bat
start_yueyue.ps1
```

Do not merge these modules back into `main.py`.

`main.py` is now runtime orchestration, not a patch pile.

## Required Test Gate

After any code change, run:

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

Only start the Telegram bot after these pass.

`scripts\blocklist_growth_check.py` checks that the response-quality reject/marker lists in
`yueyue_v3\runtime.py`, `yueyue_v3\context.py`, and `response_composer.py` have not silently
grown past their recorded baseline. If it fails, do not just add the new phrase and move on -
read the failure message: genuine leakage terms are fine to add (bump the baseline
deliberately), but repeated stylistic additions are a sign the *prompt* or
`workspace\brain\personality_samples.md` needs attention instead of a 40th banned phrase.

## Manual Test Phrases (Persona Quality)

Do not tune only against the same fixed handful of phrases every cycle - that produces a
persona that passes its own test script but drifts on anything phrased differently. Rotate
through a broader set for live Telegram smoke testing, for example:

```text
你好
hi你好月月
現在幾點
今天幾號
現在是晚上嗎
外面是黑天還是白天
早上好，剛醒
發個表情包
再發一次
這個表情包好得意
我最近一直在調你，真的有點累
我覺得你剛剛有點像機器人
陪我聊一下
那時候只是測試
下午好（實際是晚上時測試）
今天工作好煩
你今天心情怎樣
可以陪我打機嗎
不要發這個表情包
```

Pick a rotating subset each cycle instead of always the same 6-7 lines, and vary phrasing
(not just the topic) so tuning targets natural-language robustness, not memorized test
strings.

## Expected Response Format

When finishing a task, report:

1. Summary
2. Files changed
3. Commands run
4. Test results
5. Remaining risks or blockers
6. Recommended next step

## Collaboration Protocol

The project owner works with ChatGPT as the supervisor and Codex as the executor.

Codex should optimize reports for supervisor review:

- Be concise.
- Do not over-explain.
- State exactly what changed.
- State exact files touched.
- State exact commands run.
- State exact test results.
- State remaining risks.
- State the single recommended next action.
- If unsure, stop and ask before editing.
- Do not continue into a second task without owner confirmation.

Preferred report format:

1. Summary
2. Files changed
3. Commands run
4. Test results
5. Risks / blockers
6. Recommended next step

Avoid long background explanations unless explicitly requested.