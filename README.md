# YueYue (月月) — a companion agent that never fakes it

YueYue is a Telegram companion agent runtime: real task execution with hard honesty guarantees,
plus conversation quality engineered to feel like a person, not an assistant.

**中文速覽**:這是一個私人 Telegram 陪伴 agent 運行時。它的兩個立身之本:①任務執行「不准假成功」——
每個結果都要有真實工具證據背書,模型不能編造答案交差;②聊天語感工程——人設完全檔案化、
字感有單一契約與輸出閘、質量有可跑的 eval 套件把關。人設/記憶/貼圖全部私有不入庫,
你可以照 `docs/PERSONA_GUIDE.md` 打造自己的角色。

## What makes it different

**No fake success.** The workflow engine treats a screenshot, click, or command exit code as
*evidence*, never as completion. Results the model reports must be grounded in actual tool output
(a filename it never observed gets rejected); mutation steps the owner asked for hold the goal
open; generic tool status lines ("Command completed.") can never masquerade as answers. Workflow
state persists across restarts without auto-running protected tools.

**Conversation quality as an engineering discipline.** A single voice contract
(`voice_contract.py`) defines the lexical register; every prompt site and output gate imports it.
Rejected generations retry with a *named critique* before any canned fallback — phrase blocklists
are frozen ratchets, not a growing whack-a-mole. A free, deterministic eval suite
(`scripts/eval_suite.py`) pins the persona gate with curated good/bad exemplars, so trimming rules
or changing models has a regression net.

**Long-term memory with an honesty gate.** Conversations are distilled into episode summaries and
facts — but a fact is only stored if it quotes the owner's actual words. Recall is dated and
hedged; zero recall injects nothing. The agent says "I don't remember" instead of inventing a past.

**Proactive, grounded care.** Commitments the owner voiced ("exam tomorrow") are extracted with
due dates and followed up exactly once. Yesterday's episode surfaces when today opens with a
greeting — memory, not keyword rules.

**Model-routed, keyword-fast.** Certain signals (media, explicit tool verbs) route instantly by
keyword; grey-zone messages are re-judged by a cheap LLM call that can only refine, never break,
the keyword decision.

## Architecture (one paragraph)

`yueyue_v3/` is the only runtime: a single-writer event loop over durable atomic state, a
goal-contract workflow engine (plan → observe → act → verify step → verify goal → reply), a
risk-tiered permission controller with workflow-scoped grants, and a context compiler that builds
bounded, mode-isolated prompts (CHAT/SOCIAL/TASK/VISION/PRESENCE). Chat and task use different
models (cheap chat voice / strong task planner); vision has its own fallback chain. Details in
`ARCHITECTURE.md`.

## Setup

1. Python 3.11+, `python -m pip install -r requirements.txt`
2. Copy `.env.example` → `.env`; fill in your SiliconFlow API key and Telegram bot token. Keep
   `.env` private — it is gitignored and the regression gate runs a secret scan.
3. Write your persona: see **`docs/PERSONA_GUIDE.md`** — the repo ships no personality;
   `workspace/brain/` and `workspace/memory/` are yours and never committed.
4. Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1
```

Useful variants:

```powershell
# Verify startup without launching Telegram (includes scripts/system_audit.py)
powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1 -CheckOnly
# Clean restart when a previous launcher is still running
powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1 -Restart
# Full regression gate: pytest suite, response-quality checks, Ruff, secret scan
powershell -ExecutionPolicy Bypass -File .\start_yueyue.ps1 -CheckOnly -SelfTest
# Runtime health
python -m yueyue_v3.health --root .
# Persona quality evals (Tier 1 free/deterministic; --live scores real generation)
python scripts/eval_suite.py
```

## Configuration

Model routing lives in `.env` (see `.env.example` for the documented full set):

```env
YUEYUE_CHAT_MODEL=Pro/MiniMaxAI/MiniMax-M2.5     # owner-facing voice
YUEYUE_STRONG_MODEL=deepseek-ai/DeepSeek-V4-Pro  # task planning/execution
YUEYUE_LLM_ROUTING=1     # grey-zone LLM routing
YUEYUE_LTM=1             # long-term memory distillation + recall
YUEYUE_TASK_POSTCHECK=1  # post-completion sanity check
```

## Permission model

Risk-tiered: read-only/local tools are free; workspace file writes take one approval per workflow;
arbitrary commands, deletes, downloads, and UI control require explicit per-call approval.
Reply 可以 to approve, 不要 to deny, 全部可以 for turn-wide grants — grammar details in
`RUNBOOK.md`.

## Operations & plan

`RUNBOOK.md` — restart, permission grammar, observability files, troubleshooting.
`docs/ROADMAP.md` — the engineering plan and its current state.

## License

MIT — see `LICENSE`.
