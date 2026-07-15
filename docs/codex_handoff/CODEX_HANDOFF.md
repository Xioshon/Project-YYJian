# YueYue / Project YYJian Codex Handoff

## Current Status

Project root:

```text
C:\Agent
```

Current baseline:

```text
Quality Baseline 2026-06-25 F
```

Current phase:

```text
Stability & Quality Freeze / Quality Consolidation
```

Do not treat this project as a broken emergency patch state.

The engineering layer is currently considered stable enough. The next phase should focus on persona quality, response quality, temporal consistency, and sticker emotional behavior.

## Current Priority

Preserve the current baseline first.

Do not start a large rewrite.

Do not start the new PromptCompiler framework yet.

Do not add new tool systems, GUI systems, memory systems, or large architecture changes before protecting the current baseline.

## Important Rules

* Make small, reversible changes.
* Back up files outside `C:\Agent`.
* Never create recursive backups inside the project folder.
* Use `C:\Agent_manual_backup` for manual backups.
* This environment is Windows PowerShell, not Bash.
* Do not use Bash heredoc syntax such as `python - <<'PY'`.
* Use PowerShell here-string syntax instead.

Example:

```powershell
@'
print("hello")
'@ | python
```

## Current Module Layout

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

Current understanding:

* `main.py` is now runtime orchestration, not a patch pile.
* Telegram input extraction has been split into `telegram_input.py`.
* Temporal handling has been split into `temporal_context.py`.
* Sticker selection and resend logic have been split into `sticker_assets.py` and `sticker_flow.py`.
* Reply context pollution protection is handled by `reply_context.py`.
* Send state tracking is handled by `telegram_outbox.py`.
* Owner intent routing is handled by `intent_router.py`.
* Response shaping is handled by `response_composer.py`.

## What Was Fixed

The previous patch-heavy state has been cleaned.

`main.py` should no longer contain temporary markers such as:

```text
_yyjian_
BEGIN YYJIAN
hotfix
patch
```

Sticker send reliability is now tracked through outbox states:

```text
send.pending
send.sent
send.failed
```

Reply context should not remember failed media sends as if they succeeded.

Telegram raw text should be extracted from message text or caption, not only from compiled prompts.

Temporal context now injects real current time, but should not use brittle output string replacement.

## Known Remaining Problems

This section was last updated 2026-06-25. It is now stale: `docs/codex_handoff/night_report_20260628.md`
in this same folder documents P1 as fixed and verified, and later work (recorded outside this handoff
doc, in the project's main session history) substantially addressed P2. Do not treat P1/P2 below as
open work without first reading the night report and checking the current code. P3/P4 have no recorded
verification and should still be treated as open.

### P1 — Temporal consistency — RESOLVED, verified 2026-07-01

Was: YueYue could follow the user's wrong greeting (e.g. answering with afternoon imagery when the
owner said "下午好" at night).

`night_report_20260628.md`'s "Final accepted status - 2026-07-01" records this as fixed and live-smoke
verified: "Day/night temporal route is working", with evidence such as `現在是晚上嗎` -> `現在是早上喔，
主人時間感沒壞。` and `外面是黑天還是白天` -> `按現在時間來看，現在是早上，外面應該算白天。` (both routed
through deterministic temporal context, not model guesswork). If a regression is suspected, re-run
`scripts\temporal_context_check.py` and re-test the exact phrases logged in that report before assuming
this is broken again.

### P2 — Persona replies can be too long — SUBSTANTIALLY ADDRESSED, keep monitoring

Was: YueYue sometimes over-explained jokes, stickers, or emotional intent.

`night_report_20260628.md` flagged this as an explicit "remaining risk" as of 2026-07-01 ("`早上好，剛醒`
produced several extra natural follow-up lines"). Since then, two changes landed that directly target
this: (1) CHAT/SOCIAL replies now default to one short, natural bubble by design (see "Persona and Chat
Quality" in `ARCHITECTURE.md`), with multi-bubble splitting reserved for genuinely heavy moments; (2) a
message-rhythm layer (split/hold/typing pacing) was built into `response_composer.py` /
`telegram_outbox.py` / `main.py` and is live. Preferred style is still: short, natural, cute, slightly
teasing, not over-explaining — that has not changed, only the mechanism enforcing it.

### P3 — Some metaphors are weird or too intimate — STILL OPEN

No verification found that this was specifically addressed. Keep the tone: playful, safe, light,
natural, not suggestive, not over-intimate.

### P4 — Stickers should become emotional support — STILL OPEN

Sticker send/resend mechanics work reliably and a sticker cooldown/anti-repeat mechanism was fixed
separately, which helps with "do not over-trigger stickers." But the deeper reframing - making stickers
feel like emotional support rather than a tool command - has no recorded verification and should still
be treated as an open design goal.

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

This list must stay in sync with the equivalent lists in `AGENTS.md` and `ARCHITECTURE.md` - all
three should name the same set of scripts. `AGENTS.md` is the current source of truth for this list.

Only start the Telegram bot after these pass.

## Do Not Do

* Do not reset the repo.
* Do not delete untracked modules.
* Do not run `git clean -fd`.
* Do not run `git reset --hard`.
* Do not treat untracked source files as garbage.
* Do not start a large prompt rewrite.
* Do not add automatic sticker spam.
* Do not use output-after-generation string replacement for temporal consistency.
* Do not commit `.env`, raw ChatGPT exports, private memory, logs, or runtime caches.

## Next Intended Phase

Phase 2:

```text
Persona Quality & Response Policy
```

Priority order:

1. Response length control.
2. Temporal contradiction handling.
3. Sticker-as-emotion layer.
4. Persona consistency tests.
5. Avoid over-explaining jokes or stickers.

## Current Goal

The goal is not just "it runs."

The goal is:

```text
stable
observable
not fake-success
not patchy
natural
emotionally consistent
maintainable
```
