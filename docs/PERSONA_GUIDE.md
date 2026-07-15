# Writing Your Own Persona / 打造你自己的人設

YueYue's personality is **entirely file-driven** — the repo ships no personality, and the private
`workspace/brain/` + `workspace/memory/` + sticker collection are never committed. To run your own
companion, create these files:

## Required files

### `workspace/brain/personality.md`
Who the agent IS. Identity, temperament, speech register, relationship to the owner.
Loaded into every system prompt (budget ~6500 chars). Write it in the language you want replies in.

### `workspace/brain/rules.md`
Stable behavioral rules (budget ~3200 chars): what she never does, honesty requirements,
tone boundaries. Keep it short — mechanical rules (bubble counts, registers) are enforced by
code gates, so this file only needs the *semantic* rules a gate cannot check.

### `workspace/brain/personality_samples.md`
Style calibration samples (budget ~4200 chars), loaded for CHAT/SOCIAL turns only. 8-15 short
exchanges showing HOW she talks in different moods (teased, praised, comforted, provoked).
The model calibrates feeling and rhythm from these — it is told not to copy them verbatim.
**This file matters more than any instruction list.** Show, don't tell.

### `workspace/memory/profile.json`
Owner profile facts as JSON (name, preferences, context the agent should always know).
Start with `{}` — the long-term memory layer grows real knowledge over time.

## Optional

### Stickers: `workspace/assets/stickers/` + `workspace/assets/stickers_index.json`
Drop image files in the folder, then index them with coarse emotion tags so the social layer can
pick by feeling. Index shape (`stickers_index.json` maps filename -> metadata; the social index
`social_sticker_index.json` is maintained automatically):

```json
{
  "happy_bounce.gif": {"filename": "happy_bounce.gif", "tags": ["happy"], "safe_for_minor": true,
                        "approved_for_autouse": true, "rejected": false}
}
```

Only indexed, approved stickers are ever auto-sent — unindexed files are ignored by selection.

## Voice register (the hard part)

`voice_contract.py` is the single source of truth for the lexical register (which script/particles
are allowed). The default enforces Hong Kong written Traditional Chinese; if your persona speaks
differently, edit the contract THERE — every prompt site and output gate imports from it, so one
edit moves the whole system. Run `python scripts/eval_suite.py` after changing it: the Tier-1
persona eval tells you immediately if your good examples now get rejected.

## Quality workflow

1. Write persona files → restart the bot.
2. Chat. When a reply feels off, don't add a banned-phrase rule — add the *ideal* reply you wanted
   to `evals/cases.py` as a good exemplar (and optionally the bad one as a failure case).
3. `python scripts/eval_suite.py` (free, deterministic) keeps every future change honest.
